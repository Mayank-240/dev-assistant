"""Session pool: bounds how many agent sessions run at once and terminates idle ones.

A "session" wraps a live agent instance plus a bit of bookkeeping. The pool:
  - caps concurrency with a semaphore (sessions never exceed the configured max in use),
  - reuses a warm idle session of the same agent type when one is free (cheap, keeps
    prompt cache warm),
  - runs a background reaper that closes sessions idle longer than the TTL, so spawned
    agents are never left lying around.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("ada.pool")

_ids = itertools.count(1)


@dataclass
class Session:
    agent_name: str
    agent: object
    id: int = field(default_factory=lambda: next(_ids))
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    closed: bool = False

    def close(self) -> None:
        # Terminating a session drops its live context; the shared LLM client/agent
        # registry is owned by the runtime, not the session, so this is intentionally cheap.
        self.closed = True
        self.agent = None  # type: ignore[assignment]


class SessionPool:
    def __init__(
        self,
        *,
        max_concurrent: int,
        idle_ttl: float,
        reaper_interval: float,
        agent_provider: Callable[[str], object],
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._idle_ttl = idle_ttl
        self._reaper_interval = reaper_interval
        self._provider = agent_provider
        self._idle: list[Session] = []
        self._leased: dict[int, Session] = {}
        self._reaper: asyncio.Task | None = None
        self.created_total = 0
        self.reaped_total = 0

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for sess in [*self._idle, *self._leased.values()]:
            sess.close()
        self._idle.clear()
        self._leased.clear()

    async def acquire(self, agent_name: str) -> Session:
        await self._sem.acquire()
        sess = self._take_idle(agent_name)
        if sess is None:
            sess = Session(agent_name=agent_name, agent=self._provider(agent_name))
            self.created_total += 1
            logger.info("spawned session #%d for agent '%s'", sess.id, agent_name)
        sess.last_used = time.time()
        self._leased[sess.id] = sess
        return sess

    def release(self, sess: Session) -> None:
        self._leased.pop(sess.id, None)
        if not sess.closed:
            sess.last_used = time.time()
            self._idle.append(sess)
        self._sem.release()

    @contextlib.asynccontextmanager
    async def lease(self, agent_name: str):
        sess = await self.acquire(agent_name)
        try:
            yield sess
        finally:
            self.release(sess)

    @property
    def active(self) -> int:
        return len(self._leased)

    @property
    def idle(self) -> int:
        return len(self._idle)

    def _take_idle(self, agent_name: str) -> Session | None:
        for i, sess in enumerate(self._idle):
            if sess.agent_name == agent_name and not sess.closed:
                return self._idle.pop(i)
        return None

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reaper_interval)
            self._reap_once()

    def _reap_once(self) -> None:
        now = time.time()
        survivors: list[Session] = []
        for sess in self._idle:
            if now - sess.last_used > self._idle_ttl:
                sess.close()
                self.reaped_total += 1
                logger.info(
                    "reaped idle session #%d (agent '%s', idle %.1fs)",
                    sess.id, sess.agent_name, now - sess.last_used,
                )
            else:
                survivors.append(sess)
        self._idle = survivors
