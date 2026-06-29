"""Tests for the Tier 1-3 reliability/verification additions."""

from __future__ import annotations

import asyncio

import pytest

from ai_dev_assistant.context import assemble
from ai_dev_assistant.execution import ExecutionResult
from ai_dev_assistant.llm.jsonout import parse_model, repair_json
from ai_dev_assistant.llm.pricing import cost
from ai_dev_assistant.llm.schemas import Plan, SubTask, Verdict
from ai_dev_assistant.llm.usage import UsageTotals
from ai_dev_assistant.memory.store import MemoryStore
from ai_dev_assistant.orchestration.scheduler import Scheduler
from ai_dev_assistant.orchestration.session_pool import SessionPool
from ai_dev_assistant.orchestration.task import DAGError, RunStatus, TaskRun
from ai_dev_assistant.verification import apply_objective_gate


# ---- JSON repair (Tier 1) ----
def test_repair_json_handles_prose_fence_and_trailing_comma():
    messy = 'Sure! Here you go:\n```json\n{"passed": true, "score": 90, "reasons": ["ok",],}\n``` done'
    v = parse_model(messy, Verdict)
    assert v.passed and v.score == 90 and v.reasons == ["ok"]


def test_repair_json_smart_quotes():
    out = repair_json('{“a”: 1,}')
    assert out == '{"a": 1}'


# ---- DAG validation (Tier 3) ----
def _run(subtasks):
    return TaskRun.from_plan("t", Plan(summary="s", subtasks=subtasks))


def test_validate_detects_cycle():
    r = _run([
        SubTask(id="a", title="a", description="d", agent="coder", rationale="r", depends_on=["b"]),
        SubTask(id="b", title="b", description="d", agent="coder", rationale="r", depends_on=["a"]),
    ])
    with pytest.raises(DAGError):
        r.validate()


def test_validate_detects_dangling_and_dupes():
    with pytest.raises(DAGError):
        _run([SubTask(id="a", title="a", description="d", agent="coder", rationale="r",
                      depends_on=["ghost"])]).validate()
    with pytest.raises(DAGError):
        _run([
            SubTask(id="a", title="a", description="d", agent="coder", rationale="r"),
            SubTask(id="a", title="a2", description="d", agent="coder", rationale="r"),
        ]).validate()


# ---- soft-success rollup (Tier 1) ----
def test_rollup_status_and_caveats_satisfy_deps():
    r = _run([
        SubTask(id="s1", title="x", description="d", agent="coder", rationale="r"),
        SubTask(id="s2", title="y", description="d", agent="documenter", rationale="r", depends_on=["s1"]),
    ])
    r.subtasks["s1"].status = RunStatus.PASSED_WITH_CAVEATS
    r.subtasks["s1"].result = "did the thing"
    # a caveated dep does NOT block its dependent
    r.block_unreachable()
    assert r.subtasks["s2"].status is RunStatus.PENDING
    assert [s.id for s in r.ready()] == ["s2"]
    # and the dependent receives a caveat-tagged result
    assert "caveat" in r.dependency_results(r.subtasks["s2"])["s1"].lower()
    r.subtasks["s2"].status = RunStatus.PASSED
    assert r.rollup_status() == "partial"


# ---- scheduler degrade-on-partial (Tier 1) ----
async def test_scheduler_degrades_instead_of_cascading():
    run = _run([
        SubTask(id="s1", title="code", description="d", agent="coder", rationale="r",
                acceptance_criteria=["x"]),
        SubTask(id="s2", title="docs", description="d", agent="documenter", rationale="r",
                depends_on=["s1"], acceptance_criteria=["y"]),
    ])
    pool = SessionPool(max_concurrent=2, idle_ttl=1, reaper_interval=1,
                       agent_provider=lambda n: object())
    pool.start()
    ran: list[str] = []

    async def execute(state, deps, session):
        ran.append(state.id)
        return f"output for {state.id}"

    async def verify(state, result):
        # s1 always fails review; s2 passes. Old behavior would BLOCK s2 forever.
        return Verdict(passed=state.id != "s1", score=50)

    sched = Scheduler(pool=pool, execute=execute, verify=verify, max_retries=0,
                      degrade_on_partial=True)
    await sched.run(run)
    await pool.stop()
    assert run.subtasks["s1"].status is RunStatus.PASSED_WITH_CAVEATS
    assert "s2" in ran and run.subtasks["s2"].status is RunStatus.PASSED


# ---- objective gate (Tier 3) ----
def test_objective_gate_hard_fails_on_failing_tests():
    failing = ExecutionResult("pytest", 1, "1 failed", "", 0.1)
    out = apply_objective_gate(Verdict(passed=True, score=95), {"tests": failing})
    assert out.passed is False and "tests failed" in " ".join(out.reasons).lower()


def test_objective_gate_soft_passes_when_tests_green_but_llm_nitpicks():
    ok = ExecutionResult("pytest", 0, "2 passed", "", 0.1)
    out = apply_objective_gate(Verdict(passed=False, score=40), {"tests": ok})
    assert out.passed is True and out.score >= 70


# ---- vector dim guard + dedup (Tier 4) ----
def test_remember_unique_skips_duplicates():
    mem = MemoryStore.in_memory()
    a = mem.remember_unique("longterm", "use multi-stage docker builds")
    b = mem.remember_unique("longterm", "use multi-stage docker builds")
    assert a == b  # the near-duplicate was not appended


# ---- pricing + usage (Tier 5) ----
def test_pricing_and_usage_delta():
    assert cost("claude-opus-4-8", 1_000_000, 0) == pytest.approx(15.0)
    u = UsageTotals()
    before = u.snapshot()
    u.add(input_tokens=100, output_tokens=50, cost_usd=0.01)
    d = UsageTotals.delta(before, u.snapshot())
    assert d["input_tokens"] == 100 and d["cost_usd"] == pytest.approx(0.01)


# ---- context budgeting (Tier 3) ----
def test_assemble_respects_budget():
    big = "x" * 100_000
    out = assemble([("Task", "do it"), ("Dep", big)], budget_tokens=500)
    assert "do it" in out and len(out) < 10_000
