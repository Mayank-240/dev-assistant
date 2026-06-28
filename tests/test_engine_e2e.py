"""Offline end-to-end test of the whole pipeline with a fake LLM provider.

Exercises: orchestrator plan -> parallel scheduler -> session pool -> agent run
(dispatching a tool, incl. an agent-to-agent message) -> reviewer verdict -> summary ->
docs on disk. No network / API key / Claude CLI required.
"""

from __future__ import annotations

import asyncio

from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.llm.schemas import BriefDoc, Plan, SubTask, Verdict


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
                        effort=None, max_tokens=8000, max_iterations=8, workdir=None, on_step=None):
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


async def test_budget_stops_run(tmp_path):
    import dataclasses
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
