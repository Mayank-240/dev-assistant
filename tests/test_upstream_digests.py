"""Upstream result digests: dependent subtasks inherit upstream REASONING in-plan.

A subtask with depends_on gets one "Upstream results" context part — each completed
upstream's result digest (capped per item and per section) prefixed
``[<upstream id> <title>]``; failed upstreams contribute a status line only.
CASSETTE-CRITICAL: dependency-free plans must assemble prompts byte-identical to a
control run with the digest hook disabled (technique from test_workspace_context).
"""

from __future__ import annotations

from unittest import mock

from test_engine_e2e import _settings
from test_workspace_context import _CaptureProvider, _engine

import ai_dev_assistant.engine as engine_mod
from ai_dev_assistant.engine import (
    _UPSTREAM_ITEM_CHARS,
    _UPSTREAM_SECTION_CHARS,
    upstream_results_part,
)
from ai_dev_assistant.llm.schemas import Plan, SubTask
from ai_dev_assistant.orchestration.task import RunStatus, TaskRun


def _diamond_run() -> TaskRun:
    plan = Plan(summary="s", subtasks=[
        SubTask(id="s1", title="Build core", description="d", agent="coder",
                rationale="r", acceptance_criteria=["c"]),
        SubTask(id="s2", title="Write tests", description="d", agent="test_engineer",
                rationale="r", acceptance_criteria=["c"]),
        SubTask(id="s3", title="Document", description="d", agent="documenter",
                rationale="r", depends_on=["s1", "s2"], acceptance_criteria=["c"]),
    ])
    return TaskRun.from_plan("p", plan, "t-digest")


# ---- unit: the part itself ----

def test_digest_prefix_item_and_section_caps():
    run = _diamond_run()
    run.subtasks["s1"].status = RunStatus.PASSED
    run.subtasks["s1"].result = "core " * 400          # ~2000 chars
    run.subtasks["s2"].status = RunStatus.PASSED
    run.subtasks["s2"].result = "tests " * 400
    part = upstream_results_part(run, run.subtasks["s3"])
    assert part is not None and part[0] == "Upstream results"
    lines = part[1].splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[s1 Build core] ")
    assert lines[1].startswith("[s2 Write tests] ")
    for line in lines:  # per-item: prefix + ~500-char digest + ellipsis
        assert len(line) <= len("[s2 Write tests] ") + _UPSTREAM_ITEM_CHARS + 1
        assert line.endswith("…")
    assert len(part[1]) <= _UPSTREAM_SECTION_CHARS


def test_failed_upstream_contributes_status_line_not_text():
    run = _diamond_run()
    run.subtasks["s1"].status = RunStatus.PASSED
    run.subtasks["s1"].result = "core built fine"
    run.subtasks["s2"].status = RunStatus.FAILED
    run.subtasks["s2"].result = "half-broken partial output"
    body = upstream_results_part(run, run.subtasks["s3"])[1]
    assert "[s1 Build core] core built fine" in body
    assert "[s2 Write tests] status: failed — no result inherited" in body
    assert "half-broken partial output" not in body   # failed text never inherited


def test_independent_subtask_has_no_part():
    run = _diamond_run()
    assert upstream_results_part(run, run.subtasks["s1"]) is None
    assert upstream_results_part(run, run.subtasks["s2"]) is None


# ---- engine wiring (offline, FakeProvider) ----

async def _run(settings, provider=None) -> list[str]:
    fake = provider or _CaptureProvider()
    engine = _engine(settings, fake)
    try:
        await engine.run("Add input validation and document it", on_event=lambda _e: None)
    finally:
        await engine.aclose()
    return fake.prompts


async def test_dependent_subtask_prompt_carries_upstream_digests(tmp_path):
    # FakeProvider's plan: s1/s2 independent, s3 (documenter) depends on both.
    prompts = await _run(_settings(tmp_path))
    dep_prompt = next(p for p in prompts if "Document it" in p)
    assert "Upstream results" in dep_prompt
    assert "[s1 Research approach]" in dep_prompt
    assert "[s2 Implement change]" in dep_prompt
    assert "Done — satisfies the acceptance criteria." in dep_prompt  # the digest text
    for p in prompts:  # independent subtasks never get the part
        if "Document it" not in p:
            assert "Upstream results" not in p


class _ParallelPlanProvider(_CaptureProvider):
    """A dependency-free (parallel-only) plan — the cassette-critical control shape."""

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(summary="Two independent subtasks.", subtasks=[
                SubTask(id="s1", title="Research approach", description="Investigate options.",
                        agent="researcher", rationale="r", depends_on=[],
                        acceptance_criteria=["analysis provided"]),
                SubTask(id="s2", title="Implement change", description="Write the code.",
                        agent="coder", rationale="r", depends_on=[],
                        acceptance_criteria=["code provided"]),
            ])
        return await super().structured(system=system, user=user, schema=schema,
                                        model=model, effort=effort, max_tokens=max_tokens)


async def test_dependency_free_plan_prompts_byte_identical_to_control(tmp_path):
    """Replay-cassette guarantee: with no depends_on anywhere, prompts equal a control
    run whose digest hook is disabled entirely (pre-feature behavior)."""
    with mock.patch.object(engine_mod, "upstream_results_part", lambda *_a, **_k: None):
        control = await _run(_settings(tmp_path / "control"), _ParallelPlanProvider())
    feature = await _run(_settings(tmp_path / "feature"), _ParallelPlanProvider())
    assert len(control) == len(feature) == 2
    assert sorted(control) == sorted(feature)  # byte-identical, order-independent
    assert all("Upstream results" not in p for p in feature)
