"""Offline end-to-end test of the whole pipeline with a fake LLM provider.

Exercises: orchestrator plan -> parallel scheduler -> session pool -> agent run
(dispatching a tool, incl. an agent-to-agent message) -> reviewer verdict -> summary ->
docs on disk. No network / API key / Claude CLI required.
"""

from __future__ import annotations

import asyncio

import dataclasses

from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.llm.schemas import BriefDoc, Plan, SubTask, Verdict
from ai_dev_assistant.orchestration.task import RunStatus


class _Usage:
    def __init__(self):
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def to_dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": self.cost_usd}


class FakeProvider:
    """Stands in for an LLMProvider: deterministic plan, tool dispatch, verdict, brief.

    Tracks concurrent run_agent calls so the test can prove the scheduler runs
    independent subtasks in parallel.
    """

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.usage = _Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(
                summary="Two independent subtasks run in parallel, then a documentation step.",
                subtasks=[
                    SubTask(id="s1", title="Research approach", description="Investigate options.",
                            agent="researcher", rationale="needs analysis", depends_on=[],
                            acceptance_criteria=["analysis provided"]),
                    SubTask(id="s2", title="Implement change", description="Write the code.",
                            agent="coder", rationale="needs code", depends_on=[],
                            acceptance_criteria=["code provided"]),
                    SubTask(id="s3", title="Document it", description="Write the docs.",
                            agent="documenter", rationale="needs docs", depends_on=["s1", "s2"],
                            acceptance_criteria=["docs written"]),
                ],
            )
        if schema is Verdict:
            return Verdict(passed=True, score=95, reasons=["meets all criteria"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Researched, implemented, and documented the change.",
                            key_points=["analysis done", "code written", "docs produced"],
                            status="completed")
        raise AssertionError(f"unexpected schema {schema}")

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=8, workdir=None,
                        on_step=None, **_kw):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)  # yield so concurrent subtasks actually interleave
            self.usage.cost_usd += 5.0
            if on_step:
                on_step({"kind": "text", "text": "working…"})
            if "send_message" in allowed_tools:  # demonstrate agent-to-agent messaging
                toolbox.dispatch("send_message", {"recipient": "reviewer", "content": "Result incoming."})
            else:
                toolbox.dispatch("recall", {"query": "prior context"})
            return "Done — satisfies the acceptance criteria."
        finally:
            self.active -= 1

    async def aclose(self):
        pass


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_backend="anthropic",  # avoids importing the Claude SDK; provider is overridden below
        anthropic_api_key="",
        embeddings_backend="hash",
        data_dir=tmp_path / "data",
        docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace",
        session_idle_ttl=0.05,
        reaper_interval=0.02,
        max_concurrent_sessions=4,
        max_retries=0,
        verify_run_tests=False,  # FakeProvider writes no files; keep the e2e offline + fast
    )


async def test_full_pipeline_offline(tmp_path):
    settings = _settings(tmp_path)
    engine = Engine(settings)
    fake = FakeProvider()
    engine.provider = fake
    engine.orchestrator._provider = fake
    engine.reviewer._provider = fake

    events = []
    try:
        run, brief, out_dir = await engine.run("Add input validation and document it", on_event=events.append)
    finally:
        await engine.aclose()

    assert run.all_passed
    assert len(run.subtasks) == 3

    # docs written, including the at-a-glance index
    assert (out_dir / "plan.md").is_file()
    assert (out_dir / "report.md").is_file()
    assert (out_dir / "brief.md").is_file()
    assert (settings.docs_dir / "INDEX.md").is_file()
    assert brief.tldr

    # independent subtasks actually ran concurrently, each with its own session
    assert fake.max_active >= 2
    assert engine.last_pool_stats["created"] >= 2

    # at least one agent-to-agent message flowed, and structured events were emitted
    assert any(m.sender in {"researcher", "coder"} for m in engine.bus.history)
    assert any(e.type == "subtask_review" for e in events)
    assert any(e.type == "agent_step" for e in events)  # live streaming
    assert any(e.type == "done" for e in events)

    # knowledge graph captured the task structure and was persisted
    assert settings.graph_path.is_file()
    rels = {(t.subject, t.relation, t.object) for t in engine.kg.facts_about("s1")}
    assert ("s1", "assigned_to", "researcher") in rels


async def test_run_binds_sandbox_settings(tmp_path):
    """Run start calls execution.configure_sandbox with this run's settings, so
    every command spawned below picks up the tier/image/network policy."""
    import ai_dev_assistant.execution as execution

    settings = dataclasses.replace(_settings(tmp_path), sandbox="container",
                                   sandbox_image="custom:img", sandbox_allow_network=True)
    engine = Engine(settings)
    fake = FakeProvider()
    engine.provider = fake
    engine.orchestrator._provider = fake
    engine.reviewer._provider = fake
    try:
        await engine.run("Bind the sandbox", on_event=lambda _e: None)
        assert execution._SANDBOX_CONFIG == {
            "backend": "container", "image": "custom:img", "allow_network": True}
    finally:
        await engine.aclose()
        execution._SANDBOX_CONFIG = None  # don't leak this run's policy into other tests


async def test_budget_stops_run(tmp_path):
    settings = dataclasses.replace(_settings(tmp_path), budget_usd=3.0)  # each agent call costs 5
    engine = Engine(settings)
    fake = FakeProvider()
    engine.provider = fake
    engine.orchestrator._provider = fake
    engine.reviewer._provider = fake

    events = []
    try:
        run, brief, out_dir = await engine.run("do a thing", on_event=events.append)
    finally:
        await engine.aclose()

    assert any(e.type == "budget" for e in events)
    done = next(e for e in events if e.type == "done")
    assert done.data["over_budget"] is True
    # s1/s2 (parallel, no deps) ran; s3 (depends on them) was stopped by the cap
    assert run.subtasks["s3"].status.value != "passed"


# ---- self-heal (adaptive replan on by default) + retry visibility ----

class _OneStepProvider(FakeProvider):
    """Single-subtask plan so the failure/repair/retry paths are deterministic."""

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(summary="One coding step.", subtasks=[
                SubTask(id="s1", title="Implement change", description="Write the code.",
                        agent="coder", rationale="needs code", depends_on=[],
                        acceptance_criteria=["code provided"])])
        return await super().structured(system=system, user=user, schema=schema,
                                        model=model, effort=effort, max_tokens=max_tokens)


class _TimeoutProvider(_OneStepProvider):
    """The original attempt sleeps past the wall-clock cap; the injected repair
    subtask (its task text starts with 'Repair:') completes normally."""

    async def run_agent(self, *, prompt, on_step=None, **_kw):
        if "Repair:" not in prompt:
            await asyncio.sleep(5)  # cancelled by the subtask wall-clock cap
        self.usage.cost_usd += 0.01
        return "Repaired — satisfies the acceptance criteria."


async def test_timeout_failed_subtask_gets_repair_subtask(tmp_path):
    # adaptive_replan is deliberately NOT set: self-heal must be the default
    settings = dataclasses.replace(_settings(tmp_path), subtask_max_seconds=0.2,
                                   objective_review=False)
    assert settings.adaptive_replan is True
    engine = Engine(settings)
    fake = _TimeoutProvider()
    engine.provider = fake
    engine.orchestrator._provider = fake
    engine.reviewer._provider = fake

    events = []
    try:
        run, brief, out_dir = await engine.run("do a thing", on_event=events.append)
    finally:
        await engine.aclose()

    # the wall-clock timeout left s1 FAILED (not stuck RUNNING) …
    assert run.subtasks["s1"].status is RunStatus.FAILED
    assert "wall-clock cap" in (run.subtasks["s1"].error or "")
    # … and the replan hook self-healed it with a bounded "-fix" repair subtask
    fix = run.subtasks.get("s1-fix")
    assert fix is not None and fix.status is RunStatus.PASSED
    assert any(e.type == "plan_update" and e.data.get("id") == "s1-fix" for e in events)


class _FailReviewOnceProvider(_OneStepProvider):
    """First verdict fails with a concrete reason; the retry passes."""

    def __init__(self) -> None:
        super().__init__()
        self.review_calls = 0

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Verdict:
            self.review_calls += 1
            if self.review_calls == 1:
                return Verdict(passed=False, score=40, reasons=["missing edge cases"],
                               suggestions=["handle empty input"])
            return Verdict(passed=True, score=95, reasons=["meets all criteria"], suggestions=[])
        return await super().structured(system=system, user=user, schema=schema,
                                        model=model, effort=effort, max_tokens=max_tokens)


async def test_review_retry_emits_subtask_retry_event(tmp_path):
    settings = dataclasses.replace(_settings(tmp_path), max_retries=1,
                                   objective_review=False)
    engine = Engine(settings)
    fake = _FailReviewOnceProvider()
    engine.provider = fake
    engine.orchestrator._provider = fake
    engine.reviewer._provider = fake

    events = []
    try:
        run, brief, out_dir = await engine.run("do a thing", on_event=events.append)
    finally:
        await engine.aclose()

    retry = next(e for e in events if e.type == "subtask_retry")
    assert retry.data == {"id": "s1", "agent": "coder", "attempt": 1, "max": 1}
    assert "missing edge cases" in retry.message
    assert run.subtasks["s1"].status is RunStatus.PASSED
    assert run.subtasks["s1"].attempts == 2  # the retry actually re-ran the agent
