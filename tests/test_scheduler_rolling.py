"""Rolling-dispatch scheduler tests (A2): dependents start as soon as their deps finish,
without waiting for slower siblings, while all batch-era semantics are preserved."""

from __future__ import annotations

import asyncio
import time

import pytest

from ai_dev_assistant.llm.schemas import Plan, SubTask, Verdict
from ai_dev_assistant.orchestration.scheduler import BudgetExceeded, Scheduler
from ai_dev_assistant.orchestration.session_pool import SessionPool
from ai_dev_assistant.orchestration.task import RunStatus, SubTaskState, TaskRun


def _sub(sid: str, agent: str = "coder", deps: list[str] | None = None) -> SubTask:
    return SubTask(id=sid, title=sid, description="d", agent=agent, rationale="r",
                   depends_on=deps or [], acceptance_criteria=["ok"])


def _run(subtasks: list[SubTask]) -> TaskRun:
    return TaskRun.from_plan("t", Plan(summary="s", subtasks=subtasks))


def _pool(n: int = 4) -> SessionPool:
    pool = SessionPool(max_concurrent=n, idle_ttl=5, reaper_interval=5,
                       agent_provider=lambda name: object())
    pool.start()
    return pool


async def _pass_verify(state, result):
    return Verdict(passed=True, score=90)


# ---- rolling dispatch: a dependent must not wait on a slow sibling ----
async def test_dependent_starts_before_slow_sibling_finishes():
    # a (fast) -> c depends on a; b (slow) is independent. Under the old batch barrier,
    # c could not start until b finished; rolling dispatch starts c as soon as a passes.
    run = _run([_sub("a"), _sub("b"), _sub("c", deps=["a"])])
    pool = _pool()
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}

    async def execute(state, deps, session):
        starts[state.id] = time.monotonic()
        await asyncio.sleep(0.3 if state.id == "b" else 0.01)
        ends[state.id] = time.monotonic()
        return f"output for {state.id}"

    sched = Scheduler(pool=pool, execute=execute, verify=_pass_verify, max_retries=0)
    await sched.run(run)
    await pool.stop()

    assert all(run.subtasks[i].status is RunStatus.PASSED for i in ("a", "b", "c"))
    assert "c" in starts, "c never started"
    assert starts["c"] < ends["b"], (
        f"c started at {starts['c']:.3f} only after slow sibling b ended at {ends['b']:.3f} "
        "— batch barrier is back"
    )
    assert run.subtasks["c"].result == "output for c"


# ---- BudgetExceeded mid-flight cancels the still-running sibling ----
async def test_budget_exceeded_cancels_in_flight_sibling():
    run = _run([_sub("a"), _sub("b")])
    pool = _pool()
    b_started = asyncio.Event()
    b_finished = False
    b_cancelled = False

    async def execute(state, deps, session):
        nonlocal b_finished, b_cancelled
        if state.id == "a":
            await b_started.wait()  # ensure b is genuinely mid-flight
            raise BudgetExceeded()
        b_started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            b_cancelled = True
            raise
        b_finished = True
        return "output for b"

    sched = Scheduler(pool=pool, execute=execute, verify=_pass_verify, max_retries=0)
    with pytest.raises(BudgetExceeded):
        await sched.run(run)
    await pool.stop()

    assert b_cancelled and not b_finished
    # every subtask is terminal with an end time — nothing left RUNNING/PENDING
    for state in run.subtasks.values():
        assert state.status in (RunStatus.FAILED, RunStatus.BLOCKED), state.status
        assert state.ended_at is not None


# ---- cancelling scheduler.run() itself must not leak in-flight tasks ----
async def test_external_cancellation_settles_states_and_cancels_children():
    run = _run([_sub("a")])
    pool = _pool()
    a_started = asyncio.Event()
    a_cancelled = False

    async def execute(state, deps, session):
        nonlocal a_cancelled
        a_started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            a_cancelled = True
            raise
        return "out"

    sched = Scheduler(pool=pool, execute=execute, verify=_pass_verify, max_retries=0)
    runner = asyncio.ensure_future(sched.run(run))
    await a_started.wait()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    await pool.stop()

    assert a_cancelled
    assert run.subtasks["a"].status is RunStatus.BLOCKED
    assert run.subtasks["a"].ended_at is not None


# ---- replan still fires when the run stalls, and its repair subtask runs ----
async def test_replan_fires_on_terminal_failure_and_repair_runs():
    run = _run([_sub("a"), _sub("b", deps=["a"])])
    pool = _pool()
    replan_calls = 0

    async def execute(state, deps, session):
        if state.id == "a":
            raise RuntimeError("boom")  # non-transient -> FAILED, no review retry left
        return f"output for {state.id}"

    async def replan(r: TaskRun) -> int:
        nonlocal replan_calls
        replan_calls += 1
        if replan_calls > 1:
            return 0  # amend the DAG exactly once (contract: return number added)
        fix = _sub("a-fix")
        r.plan.subtasks.append(fix)
        r.subtasks["a-fix"] = SubTaskState(spec=fix)
        return 1

    sched = Scheduler(pool=pool, execute=execute, verify=_pass_verify, max_retries=0,
                      degrade_on_partial=False, replan=replan)
    await sched.run(run)
    await pool.stop()

    assert replan_calls >= 1
    assert run.subtasks["a"].status is RunStatus.FAILED
    assert run.subtasks["a-fix"].status is RunStatus.PASSED
    assert run.subtasks["b"].status is RunStatus.BLOCKED  # its dep truly failed


# ---- on_retry hook fires when a failed review is about to be re-run ----
async def test_on_retry_hook_fires_on_review_retry():
    run = _run([_sub("a")])
    pool = _pool()
    retries: list[tuple[str, int, int, str]] = []
    verdicts = iter([Verdict(passed=False, score=40, reasons=["missing tests"]),
                     Verdict(passed=True, score=90)])

    async def execute(state, deps, session):
        return f"output for {state.id}"

    async def verify(state, result):
        return next(verdicts)

    def on_retry(state, attempt, max_retries, verdict):
        retries.append((state.id, attempt, max_retries, verdict.reasons[0]))

    sched = Scheduler(pool=pool, execute=execute, verify=verify, max_retries=1,
                      on_retry=on_retry)
    await sched.run(run)
    await pool.stop()

    assert retries == [("a", 1, 1, "missing tests")]
    assert run.subtasks["a"].status is RunStatus.PASSED


# ---- degrade-on-partial unchanged under rolling dispatch ----
async def test_degrade_on_partial_unchanged():
    run = _run([_sub("a"), _sub("b", agent="documenter", deps=["a"])])
    pool = _pool()

    async def execute(state, deps, session):
        return f"output for {state.id}"

    async def verify(state, result):
        # a never passes review; it produced output, so it degrades instead of cascading
        return Verdict(passed=state.id != "a", score=50)

    sched = Scheduler(pool=pool, execute=execute, verify=verify, max_retries=1,
                      degrade_on_partial=True)
    await sched.run(run)
    await pool.stop()

    assert run.subtasks["a"].status is RunStatus.PASSED_WITH_CAVEATS
    assert run.subtasks["a"].attempts == 2  # max_retries=1 -> two review attempts
    assert run.subtasks["b"].status is RunStatus.PASSED
    # the dependent saw the caveat-tagged dep result path (task.dependency_results)
    assert run.rollup_status() == "partial"


# ---- the session pool's semaphore remains the single concurrency bound ----
async def test_pool_cap_bounds_rolling_concurrency():
    run = _run([_sub("a"), _sub("b"), _sub("c")])
    pool = _pool(1)
    running = 0
    max_running = 0

    async def execute(state, deps, session):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.02)
        running -= 1
        return f"output for {state.id}"

    sched = Scheduler(pool=pool, execute=execute, verify=_pass_verify, max_retries=0)
    await sched.run(run)
    await pool.stop()

    assert max_running == 1  # pool cap respected even though all three were dispatched
    assert all(s.status is RunStatus.PASSED for s in run.subtasks.values())


# ---- gate is awaited before each dispatch ----
async def test_gate_checked_before_each_dispatch():
    run = _run([_sub("a"), _sub("b", deps=["a"])])
    pool = _pool()
    gate_calls = 0

    async def gate():
        nonlocal gate_calls
        gate_calls += 1

    async def execute(state, deps, session):
        return f"output for {state.id}"

    sched = Scheduler(pool=pool, execute=execute, verify=_pass_verify, max_retries=0,
                      gate=gate)
    await sched.run(run)
    await pool.stop()

    assert gate_calls == 2  # once per dispatched subtask
    assert all(s.status is RunStatus.PASSED for s in run.subtasks.values())
