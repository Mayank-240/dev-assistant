"""In-run control (Tier 5): pause / resume / steer / answer a live run.

RunControl lets the UI pause a run between batches, inject steer notes that feed the
next subtask's context, and ANSWER attention requests — clarifying questions (`ask`)
and permission requests (`permission`) that agents raise mid-run and await. Answers
arrive through the same steer channel as notes shaped:

    [answer <id>] <free text or chosen option>
    [permission <id>] ALLOW ONCE: … | ALLOW FOR THIS RUN: … | DENIED: …

Such notes resolve the matching pending request instead of joining the steer queue.
"""

from __future__ import annotations

import asyncio
import itertools
import re

_ANSWER_RE = re.compile(r"^\[(answer|permission)\s+([A-Za-z0-9_\-]+)\]\s*(.*)$", re.S)


class RunControl:
    def __init__(self) -> None:
        self._resume = asyncio.Event()
        self._resume.set()  # start un-paused
        self._steer: list[str] = []
        self.paused = False
        self._req_seq = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}

    def pause(self) -> None:
        self.paused = True
        self._resume.clear()

    def resume(self) -> None:
        self.paused = False
        self._resume.set()

    # ---- attention requests (ask / permission) ----
    def open_request(self, kind: str) -> str:
        """Register a pending attention request; returns its id (e.g. 'ask-3')."""
        rid = f"{kind}-{next(self._req_seq)}"
        self._pending[rid] = asyncio.get_running_loop().create_future()
        return rid

    async def wait_answer(self, rid: str, timeout: float | None) -> str | None:
        """Await the operator's answer for ``rid``; None on timeout/cancel."""
        fut = self._pending.get(rid)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(rid, None)

    def _try_answer(self, note: str) -> bool:
        m = _ANSWER_RE.match(note)
        if not m:
            return False
        rid, body = m.group(2), m.group(3).strip()
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            return False
        fut.get_loop().call_soon_threadsafe(
            lambda: None if fut.done() else fut.set_result(body))
        return True

    def steer(self, note: str) -> None:
        note = (note or "").strip()
        if not note:
            return
        if self._try_answer(note):
            return  # consumed by a pending attention request — not a context note
        self._steer.append(note)

    def drain_steer(self) -> list[str]:
        notes, self._steer = self._steer, []
        return notes

    async def gate(self) -> None:
        """Awaited by the scheduler before each batch; blocks while paused."""
        await self._resume.wait()
