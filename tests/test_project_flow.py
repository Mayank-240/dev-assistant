"""Slice-1 project flow: tasks run from a project's durable checkout (PLAN F1)."""

from __future__ import annotations

import asyncio
import dataclasses
import subprocess

from ai_dev_assistant import projects
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


class OneShotProvider:
    """Single coder subtask that reads the materialized repo and writes a file."""

    def __init__(self) -> None:
        self.usage = _Usage()
        self.saw_files: list[str] = []

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(summary="One change.", subtasks=[
                SubTask(id="s1", title="Add greeting", description="Create hello.txt.",
                        agent="coder", rationale="code", depends_on=[],
                        acceptance_criteria=["hello.txt exists"])])
        if schema is Verdict:
            return Verdict(passed=True, score=95, reasons=["ok"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Done.", key_points=["done"], status="completed")
        raise AssertionError(f"unexpected schema {schema}")

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=8, workdir=None,
                        on_step=None, **_kw):
        await asyncio.sleep(0.01)
        self.saw_files.append(toolbox.dispatch("list_dir", {"path": "."}))
        toolbox.dispatch("write_file", {"path": "hello.txt", "content": "hi\n"})
        return "Wrote hello.txt."

    async def aclose(self):
        pass


def _settings(tmp_path, project="default") -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace", project=project,
        session_idle_ttl=0.05, reaper_interval=0.02, max_retries=0,
        verify_run_tests=False,
    )


def _wire(engine, provider):
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


async def test_task_runs_from_greenfield_project_checkout(tmp_path):
    base = _settings(tmp_path)
    proj = projects.create_project(base, "My Service")
    settings = dataclasses.replace(base, project=proj["slug"])

    # seed the project checkout with a file so materialization is observable
    checkout = projects.project_checkout(settings, proj["slug"])
    (checkout / "seed.py").write_text("SEED = 1\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "seed")

    engine = Engine(settings)
    provider = OneShotProvider()
    _wire(engine, provider)
    events = []
    try:
        run, _b, _o = await engine.run("add a greeting file", on_event=events.append)
        row = engine.runs.get(run.id) or {}
    finally:
        await engine.aclose()

    assert run.all_passed
    # the run row is tagged with the project
    assert row.get("project") == proj["slug"]
    # workspace was materialized FROM the project checkout (seed file visible to the agent)
    assert any("seed.py" in s for s in provider.saw_files)
    ws = settings.run_workspace(run.id)
    assert (ws / "seed.py").is_file() and (ws / "hello.txt").is_file()
    # the project checkout itself was not mutated by the run
    assert not (checkout / "hello.txt").exists()
    assert any("project" in (e.message or "").lower() for e in events if e.type == "status")
    # indexing advanced
    assert (projects.get_project(settings, proj["slug"]) or {}).get("last_indexed_commit")


async def test_task_from_inplace_import_never_touches_user_repo(tmp_path):
    user_repo = tmp_path / "theirs"
    user_repo.mkdir()
    (user_repo / "app.py").write_text("APP = True\n")
    _git(user_repo, "init", "-q")
    _git(user_repo, "add", "-A")
    _git(user_repo, "commit", "-q", "-m", "user base")
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=user_repo,
                                 capture_output=True, text=True).stdout.strip()

    base = _settings(tmp_path)
    proj = projects.import_project(base, str(user_repo))
    settings = dataclasses.replace(base, project=proj["slug"])

    engine = Engine(settings)
    provider = OneShotProvider()
    _wire(engine, provider)
    try:
        run, _b, _o = await engine.run("add a greeting file")
    finally:
        await engine.aclose()

    assert run.all_passed
    # agent saw the user's code (materialized copy) and wrote only in the run workspace
    assert any("app.py" in s for s in provider.saw_files)
    ws = settings.run_workspace(run.id)
    assert (ws / "hello.txt").is_file()
    # the user's repo: same HEAD, clean, no new files
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=user_repo,
                                capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    assert not (user_repo / "hello.txt").exists()
    porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=user_repo,
                               capture_output=True, text=True).stdout
    assert porcelain.strip() == ""


def test_cli_project_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ADA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADA_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("ADA_DOCS_DIR", str(tmp_path / "docs"))
    from ai_dev_assistant.cli import main

    assert main(["project", "new", "Alpha Service"]) == 0
    assert main(["project", "list"]) == 0
    out = capsys.readouterr().out
    assert "alpha-service" in out
    assert main(["project", "show", "alpha-service"]) == 0
    out = capsys.readouterr().out
    assert "branch" in out
    assert main(["project", "delete", "alpha-service", "--yes"]) == 0
    assert main(["project", "delete", "default", "--yes"]) == 2  # refused
