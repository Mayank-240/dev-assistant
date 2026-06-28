"""DAG scheduler: runs independent subtasks in parallel, verifies each, retries failures.

Generic over how a subtask is executed and verified — the runtime injects those two
async callables, so the scheduler only owns ordering, parallelism, and retry policy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from ..llm.schemas import Verdict
from .session_pool import Session, SessionPool
from .task import RunStatus, SubTaskState, TaskRun

logger = logging.getLogger("ada.scheduler")


class BudgetExceeded(Exception):
    """Raised by the execute fn to hard-stop scheduling when the cost cap is hit."""


ExecuteFn = Callable[[SubTaskState, dict[str, str], Session], Awaitable[str]]
VerifyFn = Callable[[SubTaskState, str], Awaitable[Verdict]]


class Scheduler:
    def __init__(
        self,
        *,
        pool: SessionPool,
        execute: ExecuteFn,
        verify: VerifyFn,
        max_retries: int,
    ) -> None:
        self._pool = pool
        self._execute = execute
        self._verify = verify
        self._max_retries = max_retries

    async def run(self, run: TaskRun) -> TaskRun:
        while True:
            run.block_unreachable()
            ready = run.ready()
            if not ready:
                break
            logger.info(
                "scheduling %d subtask(s) in parallel: %s",
                len(ready), ", ".join(s.id for s in ready),
            )
            await asyncio.gather(*(self._run_one(run, state) for state in ready))

        # Anything still pending after we run dry is part of a cycle / unsatisfiable.
        for state in run.subtasks.values():
            if state.status is RunStatus.PENDING:
                state.status = RunStatus.BLOCKED
                state.error = state.error or "unresolved dependencies (possible cycle)"
        return run

    async def _run_one(self, run: TaskRun, state: SubTaskState) -> None:
        async with self._pool.lease(state.agent) as session:
            state.status = RunStatus.RUNNING
            state.started_at = time.time()
            deps = run.dependency_results(state)

            for _ in range(self._max_retries + 1):
                state.attempts += 1
                try:
                    result = await self._execute(state, deps, session)
                except BudgetExceeded:
                    raise  # propagate — do not retry; abort the whole run
                except Exception as exc:  # an agent failure shouldn't kill the whole run
                    state.error = str(exc)
                    logger.warning("subtask %s attempt %d errored: %s", state.id, state.attempts, exc)
                    continue

                try:
                    verdict = await self._verify(state, result)
                except Exception as exc:  # a flaky verify shouldn't abort the whole run
                    logger.warning("subtask %s verification errored: %s", state.id, exc)
                    verdict = Verdict(passed=False, score=0, reasons=[f"verification error: {exc}"])
                state.result = result
                state.verdict = verdict
                if verdict.passed:
                    state.status = RunStatus.PASSED
                    state.ended_at = time.time()
                    logger.info("subtask %s passed (score %d)", state.id, verdict.score)
                    return
                logger.info("subtask %s failed review (score %d); retrying", state.id, verdict.score)

            state.status = RunStatus.FAILED
            state.ended_at = time.time()
