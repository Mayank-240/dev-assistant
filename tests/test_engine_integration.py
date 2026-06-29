"""Engine integration: real file writes through the per-run workspace, the objective
gate, telemetry files, and git delivery — all offline with a fake provider."""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.llm.schemas import BriefDoc, Plan, RunLessons, SubTask, Verdict
from ai_dev_assistant.orchestration.task import RunStatus


class _Usage:
    def __init__(self):
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def snapshot(self):
        return {"cost_usd": self.cost_usd, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "calls": 0}

    def to_dict(self):
        return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}


class FileWritingProvider:
    """run_agent writes a real file into the workspace; review degrades the coder subtask."""

    def __init__(self) -> None:
        self.usage = _Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(summary="code then document", subtasks=[
                SubTask(id="s1", title="Write add()", description="Implement add in add.py.",
                        agent="coder", rationale="r", acceptance_criteria=["add.py defines add"]),
                SubTask(id="s2", title="Document", description="Write a README.",
                        agent="documenter", rationale="r", depends_on=["s1"],
                        acceptance_criteria=["README exists"]),
            ])
        if schema is Verdict:
            # s1 fails review on purpose; the old engine would BLOCK s2 — degrade must save it.
            fail = "Write add()" in user
            return Verdict(passed=not fail, score=40 if fail else 90)
        if schema is BriefDoc:
            return BriefDoc(tldr="Built add() and documented it.", key_points=["add", "docs"],
                            status="partial")
        if schema is RunLessons:
            return RunLessons(summary="ok", what_worked=["wrote files"], what_to_avoid=[])
        raise AssertionError(f"unexpected schema {schema}")

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=None, workdir=None, on_step=None):
        # Only the coder writes a file; the documenter just reports.
        if "add.py" in prompt or "Write add" in prompt:
            Path(workdir, "add.py").write_text("def add(a, b):\n    return a + b\n")
        return "done"

    async def aclose(self):
        pass


def _settings(tmp_path, **over) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
        session_idle_ttl=0.05, reaper_interval=0.02, max_retries=0,
        verify_run_tests=False, objective_review=True, lint_check=False, **over,
    )


def _wire(engine: Engine, provider) -> None:
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider
    engine.reflector._provider = provider


async def test_files_land_in_workspace_and_degrade_keeps_dependent(tmp_path):
    settings = _settings(tmp_path)
    engine = Engine(settings)
    _wire(engine, FileWritingProvider())
    try:
        run, brief, out_dir = await engine.run("Build add() and document it")
    finally:
        await engine.aclose()

    # the coder's file landed in the PER-RUN workspace (base_dir/workdir fix)
    assert (settings.workspace_dir / run.id / "add.py").is_file()
    # s1 failed review but produced output → degraded, so s2 (documenter) still ran
    assert run.subtasks["s1"].status is RunStatus.PASSED_WITH_CAVEATS
    assert run.subtasks["s2"].status is RunStatus.PASSED
    assert run.rollup_status() == "partial"
    # telemetry + docs were written
    assert (out_dir / "events.jsonl").is_file()
    assert (out_dir / "trace.jsonl").is_file()
    assert (out_dir / "report.md").is_file()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
async def test_git_finalize_creates_a_branch(tmp_path):
    settings = _settings(tmp_path, git_finalize=True)
    engine = Engine(settings)
    _wire(engine, FileWritingProvider())
    events = []
    try:
        run, _b, _o = await engine.run("Build add() and document it", on_event=events.append)
    finally:
        await engine.aclose()
    assert any(e.type == "git" for e in events)
    assert (settings.workspace_dir / run.id / ".git").is_dir()
