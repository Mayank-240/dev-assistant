"""Cross-project fan-out (F3): children per project, budget split, stagger, rollup."""

from __future__ import annotations

import asyncio
import subprocess
import time

from ai_dev_assistant import projects
from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.llm.schemas import BriefDoc, Plan, SubTask, Verdict
from ai_dev_assistant.orchestration.fanout import run_cross_project
from ai_dev_assistant.orchestration.run_store import RunStore


class _Usage:
    def __init__(self):
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def to_dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": self.cost_usd}


class ChildProvider:
    def __init__(self, marks: list[tuple[str, float]] | None = None) -> None:
        self.usage = _Usage()
        self.marks = marks if marks is not None else []

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(summary="One change.", subtasks=[
                SubTask(id="s1", title="Write note", description="note.txt",
                        agent="coder", rationale="c", depends_on=[],
                        acceptance_criteria=["note.txt exists"])])
        if schema is Verdict:
            return Verdict(passed=True, score=90, reasons=["ok"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Done.", key_points=["done"], status="completed")
        raise AssertionError(schema)

    async def run_agent(self, *, prompt, toolbox, workdir=None, on_step=None, **_kw):
        self.marks.append(("start", time.monotonic()))
        await asyncio.sleep(0.05)
        toolbox.dispatch("write_file", {"path": "note.txt", "content": "note\n"})
        self.marks.append(("end", time.monotonic()))
        return "Wrote note.txt."

    async def aclose(self):
        pass


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


def _seed(checkout, name):
    (checkout / f"{name}.py").write_text(f"{name.upper()} = 1\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "seed")


def _settings(tmp_path, **kw) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace",
        session_idle_ttl=0.05, reaper_interval=0.02, max_retries=0,
        verify_run_tests=False, **kw,
    )


async def test_fanout_runs_children_and_rolls_up(tmp_path):
    settings = _settings(tmp_path, budget_usd=10.0, max_concurrent_runs=2)
    slugs = []
    for name in ("svc-a", "svc-b"):
        p = projects.create_project(settings, name)
        _seed(projects.project_checkout(settings, p["slug"]), name.replace("-", "_"))
        slugs.append(p["slug"])

    seen_budgets: dict[str, float] = {}
    marks: list[tuple[str, float]] = []

    def factory(child_settings: Settings) -> Engine:
        seen_budgets[child_settings.project] = child_settings.budget_usd
        engine = Engine(child_settings)
        provider = ChildProvider(marks)
        engine.provider = provider
        engine.orchestrator._provider = provider
        engine.reviewer._provider = provider
        return engine

    events = []
    result = await run_cross_project(
        settings, "add a note file", slugs, title="Notes everywhere",
        on_event=events.append, engine_factory=factory)

    assert result["status"] == "completed"
    assert {c["slug"] for c in result["children"]} == set(slugs)
    # budget split across children (10 / 2)
    assert all(abs(b - 5.0) < 1e-9 for b in seen_budgets.values())
    store = RunStore(settings.data_dir / "runs.db")
    try:
        parent = store.get(result["parent_id"]) or {}
        assert parent.get("project") == "multi"
        assert parent.get("status") == "completed"
        kids = store.children_of(result["parent_id"])
        assert {k["project"] for k in kids} == set(slugs)
        for k in kids:
            assert k["parent_id"] == result["parent_id"]
    finally:
        store.close()
    # each project got its own task branch with the change
    for c in result["children"]:
        checkout = projects.project_checkout(settings, c["slug"])
        log = subprocess.run(["git", "log", "--oneline", c["branch"]], cwd=checkout,
                             capture_output=True, text=True).stdout
        assert "ada: subtask s1" in log
        assert not (checkout / "note.txt").exists()  # checkout tree untouched
    # rollup doc + events
    rollup = (settings.docs_dir / result["parent_id"] / "rollup.md").read_text()
    assert all(s in rollup for s in slugs)
    types = [e.type for e in events]
    assert types.count("child_start") == 2 and types.count("child_done") == 2
    assert "brief" in types and "done" in types


async def test_fanout_failure_isolation_and_stagger(tmp_path):
    settings = _settings(tmp_path, max_concurrent_runs=2)
    good = projects.create_project(settings, "good-one")
    _seed(projects.project_checkout(settings, good["slug"]), "good")
    bad = projects.create_project(settings, "bad-one")
    _seed(projects.project_checkout(settings, bad["slug"]), "bad")
    order: list[str] = []

    def factory(child_settings: Settings) -> Engine:
        engine = Engine(child_settings)
        if child_settings.project == bad["slug"]:
            class Exploder(ChildProvider):
                async def structured(self, **kw):
                    raise RuntimeError("planner exploded")
            provider = Exploder()
        else:
            provider = ChildProvider()
        orig = provider.run_agent

        async def tracked(**kw):
            order.append(child_settings.project)
            return await orig(**kw)
        provider.run_agent = tracked
        engine.provider = provider
        engine.orchestrator._provider = provider
        engine.reviewer._provider = provider
        return engine

    result = await run_cross_project(
        settings, "do the thing", [bad["slug"], good["slug"]],
        stagger=True, engine_factory=factory)

    assert result["status"] == "partial"  # bad failed, good completed
    by_slug = {c["slug"]: c for c in result["children"]}
    assert by_slug[bad["slug"]]["status"] == "failed"
    assert "planner exploded" in by_slug[bad["slug"]]["error"]
    assert by_slug[good["slug"]]["status"] == "completed"
    assert order == [good["slug"]]  # bad never reached an agent; good ran after stagger head
