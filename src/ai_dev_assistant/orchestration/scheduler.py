"""DAG scheduler: runs independent subtasks in parallel, verifies each, retries failures.

Generic over how a subtask is executed and verified — the runtime injects those two
async callables, so the scheduler only owns ordering, parallelism, and retry policy.

Reliability behavior (Tier 1):
  - A transient LLM error (timeout/429/5xx) is retried with backoff and does NOT consume
    the substantive review-retry budget.
  - When review retries are exhausted but the subtask produced real output, it ends as
    PASSED_WITH_CAVEATS (degrade-on-partial) so dependents still run, instead of FAILED
    cascading BLOCKED through the whole DAG.
  - On BudgetExceeded / cancellation, in-flight siblings are cancelled and every subtask
    is left in a terminal state with an end time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from ..llm.errors import TransientLLMError
from ..llm.schemas import Verdict
from .session_pool import Session, SessionPool
from .task import RunStatus, SubTaskState, TaskRun

logger = logging.getLogger("ada.scheduler")


class BudgetExceeded(Exception):
    """Raised by the execute fn to hard-stop scheduling when the cost cap is hit."""


ExecuteFn = Callable[[SubTaskState, dict[str, str], Session], Awaitable[str]]
VerifyFn = Callable[[SubTaskState, str], Awaitable[Verdict]]
# Optional hook: given the run after a batch, amend the DAG (add/repair subtasks). Returns
# the number of subtasks added (0 = no change). Injected by the engine when adaptive
# replanning is enabled.
ReplanFn = Callable[[TaskRun], Awaitable[int]]

_TRANSIENT_BACKOFF = (0.5, 2.0, 6.0)  # seconds between transient retries


class Scheduler:
    def __init__(
        self,
        *,
        pool: SessionPool,
        execute: ExecuteFn,
        verify: VerifyFn,
        max_retries: int,
        degrade_on_partial: bool = True,
        transient_retries: int = 3,
        replan: ReplanFn | None = None,
        gate: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._pool = pool
        self._execute = execute
        self._verify = verify
        self._max_retries = max_retries
        self._degrade = degrade_on_partial
        self._transient_retries = transient_retries
        self._replan = replan
        self._gate = gate  # awaited before each batch (pause/resume control)

    async def run(self, run: TaskRun) -> TaskRun:
        try:
            while True:
                if self._gate is not None:
                    await self._gate()  # block here while the run is paused
                run.block_unreachable()
                ready = run.ready()
                if not ready:
                    if self._replan is not None and await self._replan(run) > 0:
                        continue  # the orchestrator amended the DAG — re-evaluate
                    break
                logger.info(
                    "scheduling %d subtask(s) in parallel: %s",
                    len(ready), ", ".join(s.id for s in ready),
                )
                tasks = [asyncio.ensure_future(self._run_one(run, state)) for state in ready]
                try:
                    await asyncio.gather(*tasks)
                except (BudgetExceeded, asyncio.CancelledError):
                    # Cancel still-running siblings, then settle every subtask's state.
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self._finalize_nonterminal(run, "stopped: budget exceeded / cancelled")
                    raise
        finally:
            # Anything still pending after we run dry is part of a cycle / unsatisfiable.
            for state in run.subtasks.values():
                if state.status is RunStatus.PENDING:
                    state.status = RunStatus.BLOCKED
                    state.error = state.error or "unresolved dependencies (possible cycle)"
        return run

    def _finalize_nonterminal(self, run: TaskRun, reason: str) -> None:
        for state in run.subtasks.values():
            if state.status in (RunStatus.RUNNING, RunStatus.PENDING):
                state.status = RunStatus.BLOCKED
                state.error = state.error or reason
                state.ended_at = state.ended_at or time.time()

    async def _run_one(self, run: TaskRun, state: SubTaskState) -> None:
        async with self._pool.lease(state.agent) as session:
            state.status = RunStatus.RUNNING
            state.started_at = time.time()
            deps = run.dependency_results(state)
            try:
                await self._attempt_loop(run, state, deps, session)
            except asyncio.CancelledError:
                if state.status is RunStatus.RUNNING:
                    state.status = RunStatus.BLOCKED
                    state.error = state.error or "cancelled"
                    state.ended_at = time.time()
                raise

    async def _attempt_loop(self, run, state, deps, session) -> None:
        review_attempts = 0
        while review_attempts <= self._max_retries:
            # ---- execute (with transient-error backoff that doesn't cost a review attempt)
            result = await self._execute_with_backoff(state, deps, session)
            if result is None:  # exhausted transient retries — a real failure
                state.status = RunStatus.FAILED
                state.ended_at = time.time()
                return

            # ---- verify
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

            review_attempts += 1
            if review_attempts <= self._max_retries:
                logger.info("subtask %s failed review (score %d); retrying", state.id, verdict.score)
                deps = run.dependency_results(state)  # refresh in case deps advanced

        # Retries exhausted. Degrade to a soft-success if we have real output to build on.
        if self._degrade and (state.result or "").strip():
            state.status = RunStatus.PASSED_WITH_CAVEATS
            state.ended_at = time.time()
            logger.info("subtask %s degraded to passed_with_caveats (review unmet)", state.id)
        else:
            state.status = RunStatus.FAILED
            state.ended_at = time.time()

    async def _execute_with_backoff(self, state, deps, session) -> str | None:
        """Run execute, retrying transient errors with backoff. Returns None on hard failure."""
        for i in range(self._transient_retries + 1):
            state.attempts += 1
            try:
                return await self._execute(state, deps, session)
            except BudgetExceeded:
                raise  # propagate — abort the whole run, never retry
            except TransientLLMError as exc:
                state.error = str(exc)
                if i >= self._transient_retries:
                    logger.warning("subtask %s exhausted transient retries: %s", state.id, exc)
                    return None
                delay = _TRANSIENT_BACKOFF[min(i, len(_TRANSIENT_BACKOFF) - 1)]
                logger.info("subtask %s transient error (%s); backing off %.1fs", state.id, exc, delay)
                await asyncio.sleep(delay)
            except Exception as exc:  # an agent failure shouldn't kill the whole run
                state.error = str(exc)
                logger.warning("subtask %s attempt %d errored: %s", state.id, state.attempts, exc)
                return None
        return None
