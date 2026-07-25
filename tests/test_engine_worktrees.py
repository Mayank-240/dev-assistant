"""Per-subtask git worktree isolation: parallel subtasks merge cleanly into the workspace."""

from __future__ import annotations

import asyncio
import subprocess

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


class WriterProvider:
    """Two parallel coder subtasks, each writing its own file through the toolbox."""

    def __init__(self) -> None:
        self.usage = _Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(
                summary="Write two files in parallel.",
                subtasks=[
                    SubTask(id="s1", title="Write alpha", description="Create alpha.txt.",
                            agent="coder", rationale="code", depends_on=[],
                            acceptance_criteria=["alpha.txt exists"]),
                    SubTask(id="s2", title="Write beta", description="Create beta.txt.",
                            agent="coder", rationale="code", depends_on=[],
                            acceptance_criteria=["beta.txt exists"]),
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
        await asyncio.sleep(0.02)  # let the two subtasks overlap
        name = "alpha.txt" if "alpha" in prompt else "beta.txt"
        toolbox.dispatch("write_file", {"path": name, "content": f"content of {name}\n"})
        return f"Wrote {name}."

    async def aclose(self):
        pass


def _git(ws, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=ws, check=True, capture_output=True)


async def test_parallel_subtasks_merge_via_worktrees(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace",
        session_idle_ttl=0.05, reaper_interval=0.02, max_concurrent_sessions=4,
        max_retries=0, verify_run_tests=False, worktree_per_subtask=True,
    )
    tid = "20260725-000000-wtreee"
    ws = settings.run_workspace(tid)
    ws.mkdir(parents=True)
    (ws / "base.txt").write_text("base\n")
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "base")

    engine = Engine(settings)
    provider = WriterProvider()
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider
    try:
        run, _brief, _out = await engine.run("write two files", task_id=tid)
    finally:
        await engine.aclose()

    assert run.all_passed
    # both subtasks' files landed in the SHARED workspace via worktree merges
    assert (ws / "alpha.txt").read_text() == "content of alpha.txt\n"
    assert (ws / "beta.txt").read_text() == "content of beta.txt\n"
    # merge commits exist and no worktree debris remains
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws, capture_output=True,
                         text=True).stdout
    assert "ada: subtask s1" in log and "ada: subtask s2" in log
    assert not (ws / ".ada_worktrees" / "s1").exists()
    assert not (ws / ".ada_worktrees" / "s2").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True,
                            text=True).stdout
    assert status.strip() == ""


async def test_worktrees_disabled_without_git(tmp_path):
    """A non-git workspace silently falls back to the shared-workspace path."""
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace",
        session_idle_ttl=0.05, reaper_interval=0.02, max_retries=0,
        verify_run_tests=False, worktree_per_subtask=True,
    )
    engine = Engine(settings)
    provider = WriterProvider()
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider
    try:
        run, _b, _o = await engine.run("write two files")
    finally:
        await engine.aclose()
    assert run.all_passed
    ws = settings.run_workspace(run.id)
    assert (ws / "alpha.txt").exists() and (ws / "beta.txt").exists()
