"""Checkpoint/resume (R1): an interrupted run resumes and skips completed subtasks."""

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


class FlakyProvider:
    """Deterministic plan; the 'Implement change' subtask fails until ``heal()`` is called."""

    def __init__(self) -> None:
        self.usage = _Usage()
        self.healed = False
        self.executed: list[str] = []  # which subtask titles ran

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(
                summary="Research, implement, document.",
                subtasks=[
                    SubTask(id="s1", title="Research approach", description="Investigate.",
                            agent="researcher", rationale="analysis", depends_on=[],
                            acceptance_criteria=["analysis provided"]),
                    SubTask(id="s2", title="Implement change", description="Write the code.",
                            agent="coder", rationale="code", depends_on=[],
                            acceptance_criteria=["code provided"]),
                    SubTask(id="s3", title="Document it", description="Write the docs.",
                            agent="documenter", rationale="docs", depends_on=["s1", "s2"],
                            acceptance_criteria=["docs written"]),
                ],
            )
        if schema is Verdict:
            return Verdict(passed=True, score=95, reasons=["ok"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Done.", key_points=["done"], status="completed")
        raise AssertionError(f"unexpected schema {schema}")

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=8, workdir=None,
                        on_step=None, **_kw):
        await asyncio.sleep(0.01)
        for title in ("Research approach", "Implement change", "Document it"):
            if title in prompt:
                self.executed.append(title)
        if "Implement change" in prompt and not self.healed:
            raise RuntimeError("transient tool crash")
        return "Done — satisfies the acceptance criteria."

    async def aclose(self):
        pass


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace",
        session_idle_ttl=0.05, reaper_interval=0.02, max_concurrent_sessions=4,
        max_retries=0, llm_max_retries=0, verify_run_tests=False,
    )


def _wire(engine: Engine, provider) -> None:
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider


async def test_resume_skips_completed_subtasks(tmp_path):
    settings = _settings(tmp_path)

    # First run: s2 crashes, s3 gets blocked; s1 completes and is checkpointed.
    engine1 = Engine(settings)
    flaky = FlakyProvider()
    _wire(engine1, flaky)
    try:
        run1, _brief, _out = await engine1.run("build the thing")
    finally:
        rid = run1.id if "run1" in dir() else None
        await engine1.aclose()
    assert run1.subtasks["s1"].status.value == "passed"
    assert run1.subtasks["s2"].status.value in ("failed", "blocked")
    rid = run1.id

    # Resume: the plan comes from the checkpoint store; s1 must NOT re-execute.
    engine2 = Engine(settings)
    healed = FlakyProvider()
    healed.healed = True
    _wire(engine2, healed)
    try:
        run2, _brief2, _out2 = await engine2.run("build the thing", task_id=rid, resume=True)
    finally:
        await engine2.aclose()

    assert run2.id == rid
    assert "Research approach" not in healed.executed  # restored from checkpoint, not re-run
    assert "Implement change" in healed.executed
    assert "Document it" in healed.executed
    assert run2.all_passed
    # the restored subtask kept its result
    assert run2.subtasks["s1"].result


async def test_resume_unknown_plan_replans(tmp_path):
    """Resuming an id with no stored plan falls back to planning fresh (no crash)."""
    settings = _settings(tmp_path)
    engine = Engine(settings)
    provider = FlakyProvider()
    provider.healed = True
    _wire(engine, provider)
    try:
        run, _b, _o = await engine.run("fresh thing", task_id="20990101-000000-none", resume=True)
    finally:
        await engine.aclose()
    assert run.all_passed
