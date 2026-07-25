"""Slice-2 flow: project tasks run in worktrees, land per-subtask commits on the
task branch, and per-subtask acceptance cherry-picks into the review target."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess

import pytest

from ai_dev_assistant import projects, vcs
from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.llm.schemas import BriefDoc, Plan, SubTask, Verdict

pytestmark = pytest.mark.skipif(
    not hasattr(vcs, "task_worktree_add"), reason="task worktree primitives not landed yet")


class _Usage:
    def __init__(self):
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def to_dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": self.cost_usd}


class TwoFileProvider:
    def __init__(self) -> None:
        self.usage = _Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(summary="Two files.", subtasks=[
                SubTask(id="s1", title="Write alpha", description="alpha.txt",
                        agent="coder", rationale="c", depends_on=[],
                        acceptance_criteria=["alpha.txt exists"]),
                SubTask(id="s2", title="Write beta", description="beta.txt",
                        agent="coder", rationale="c", depends_on=[],
                        acceptance_criteria=["beta.txt exists"]),
            ])
        if schema is Verdict:
            return Verdict(passed=True, score=95, reasons=["ok"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Done.", key_points=["done"], status="completed")
        raise AssertionError(schema)

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=8, workdir=None,
                        on_step=None, **_kw):
        await asyncio.sleep(0.02)
        name = "alpha.txt" if "alpha" in prompt.split("--- Context ---")[0] else "beta.txt"
        toolbox.dispatch("write_file", {"path": name, "content": f"{name} content\n"})
        return f"Wrote {name}."

    async def aclose(self):
        pass


def _git_out(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace",
        session_idle_ttl=0.05, reaper_interval=0.02, max_retries=0,
        verify_run_tests=False,
    )


async def test_project_task_branch_commits_and_acceptance(tmp_path):
    base = _settings(tmp_path)
    proj = projects.create_project(base, "Svc")
    slug = proj["slug"]
    checkout = projects.project_checkout(base, slug)
    (checkout / "seed.py").write_text("SEED = 1\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
                   cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "-m", "seed"], cwd=checkout, check=True, capture_output=True)
    head_before = _git_out(checkout, "rev-parse", "HEAD").strip()

    settings = projects.effective_settings(
        dataclasses.replace(base, project=slug))
    assert settings.worktree_per_subtask  # forced on for project tasks (decision #5)

    engine = Engine(settings)
    provider = TwoFileProvider()
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider
    events = []
    try:
        run, _b, _o = await engine.run("write two files", on_event=events.append)
        row = engine.runs.get(run.id) or {}
        states = engine.runs.get_subtask_states(run.id)
    finally:
        await engine.aclose()

    assert run.all_passed
    # workspace was a worktree outside the checkout, on the task branch
    ws = settings.workspace_dir / slug / "worktrees" / run.id
    assert ws.is_dir() and (ws / "alpha.txt").is_file() and (ws / "beta.txt").is_file()
    assert (ws / "seed.py").is_file()  # branched from project HEAD
    # checkout's own branch/working tree untouched
    assert _git_out(checkout, "rev-parse", "HEAD").strip() == head_before
    assert not (checkout / "alpha.txt").exists()
    # run row records branch + review target
    assert row.get("task_branch") == f"ada/{run.id}"
    assert row.get("review_target")
    # per-subtask merge commits recorded and reachable from the task branch
    for sid in ("s1", "s2"):
        sha = states[sid].get("merge_commit")
        assert sha, f"no merge commit for {sid}"
        assert json.loads(states[sid].get("changed") or "[]")
    branch_log = _git_out(checkout, "log", "--oneline", f"ada/{run.id}")
    assert "ada: subtask s1" in branch_log and "ada: subtask s2" in branch_log
    # policy event was emitted with tools + review metadata
    pol = next(e for e in events if e.type == "policy")
    assert pol.data["policy"]["worktree_per_subtask"] is True
    assert "coder" in pol.data["tools_by_agent"]
    assert pol.data["task_branch"] == f"ada/{run.id}"
    # branch mode (default): a git event announces the branch for review
    git_ev = next(e for e in events if e.type == "git")
    assert git_ev.data.get("branch") == f"ada/{run.id}"

    # ---- per-subtask acceptance: s1 accepted into the target, s2 not ----
    target = row["review_target"]
    res = projects.accept_commit(base, slug, states["s1"]["merge_commit"])
    assert res.get("merged"), res
    shown = _git_out(checkout, "show", f"{target}:alpha.txt")
    assert "alpha.txt content" in shown
    # beta was NOT accepted — absent from the target branch
    ls = _git_out(checkout, "ls-tree", "--name-only", target)
    assert "beta.txt" not in ls


async def test_protected_paths_policy_blocks_writes(tmp_path):
    base = _settings(tmp_path)
    proj = projects.create_project(base, "Locked")
    slug = proj["slug"]
    checkout = projects.project_checkout(base, slug)
    (checkout / "infra").mkdir()
    (checkout / "infra" / "x.tf").write_text("x\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
                   cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "-m", "seed"], cwd=checkout, check=True, capture_output=True)
    projects.set_policy(base, slug, {"protected_paths": ["infra/**"]})

    settings = projects.effective_settings(dataclasses.replace(base, project=slug))
    assert settings.protected_paths == ("infra/**",)

    class ProtectedWriter(TwoFileProvider):
        def __init__(self):
            super().__init__()
            self.denied = ""

        async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
            if schema is Plan:
                return Plan(summary="One.", subtasks=[
                    SubTask(id="s1", title="Touch infra", description="try infra",
                            agent="coder", rationale="c", depends_on=[],
                            acceptance_criteria=["attempted"])])
            return await super().structured(system=system, user=user, schema=schema,
                                            model=model, effort=effort, max_tokens=max_tokens)

        async def run_agent(self, *, prompt, toolbox, **kw):
            self.denied = toolbox.dispatch("write_file", {"path": "infra/x.tf", "content": "pwn\n"})
            toolbox.dispatch("write_file", {"path": "ok.txt", "content": "fine\n"})
            return "tried."

    engine = Engine(settings)
    provider = ProtectedWriter()
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider
    try:
        run, _b, _o = await engine.run("attempt a protected write")
    finally:
        await engine.aclose()

    assert provider.denied.startswith("DENIED: protected path")
    ws = settings.workspace_dir / slug / "worktrees" / run.id
    assert (ws / "ok.txt").is_file()
    assert (ws / "infra" / "x.tf").read_text() == "x\n"  # untouched
