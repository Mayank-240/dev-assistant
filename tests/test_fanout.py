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


# ---- cross-project task dependencies (deps kwarg) ----

def _spy_factory(seen_prompts: dict[str, str], order: list[tuple[str, str]] | None = None,
                 exploding: set[str] | None = None):
    """Engine factory that records the exact prompt each child engine received."""

    def factory(child_settings: Settings) -> Engine:
        slug = child_settings.project
        engine = Engine(child_settings)
        if exploding and slug in exploding:
            class Exploder(ChildProvider):
                async def structured(self, **kw):
                    raise RuntimeError("planner exploded")
            provider = Exploder()
        else:
            provider = ChildProvider()
        engine.provider = provider
        engine.orchestrator._provider = provider
        engine.reviewer._provider = provider
        orig_run = engine.run

        async def spy_run(p, **kw):
            seen_prompts[slug] = p
            if order is not None:
                order.append(("start", slug))
            try:
                return await orig_run(p, **kw)
            finally:
                if order is not None:
                    order.append(("end", slug))

        engine.run = spy_run
        return engine

    return factory


async def test_no_deps_prompt_is_byte_identical(tmp_path):
    """deps=None (and deps={}) must hand every child EXACTLY the caller's prompt."""
    settings = _settings(tmp_path, max_concurrent_runs=2)
    slugs = []
    for name in ("plain-a", "plain-b"):
        p = projects.create_project(settings, name)
        _seed(projects.project_checkout(settings, p["slug"]), name.replace("-", "_"))
        slugs.append(p["slug"])

    prompt = "add a note file"
    seen: dict[str, str] = {}
    events = []
    result = await run_cross_project(settings, prompt, slugs, on_event=events.append,
                                     engine_factory=_spy_factory(seen))
    assert result["status"] == "completed"
    for s in slugs:
        assert seen[s] == prompt  # byte-identical, no appended context
    # no wave marker on the no-deps path
    for e in events:
        if e.type in ("child_start", "child_done"):
            assert "wave" not in e.data


async def test_deps_two_waves_order_and_injected_context(tmp_path):
    settings = _settings(tmp_path, budget_usd=10.0, max_concurrent_runs=2)
    a = projects.create_project(settings, "up-api")["slug"]
    b = projects.create_project(settings, "down-web")["slug"]
    _seed(projects.project_checkout(settings, a), "up_api")
    _seed(projects.project_checkout(settings, b), "down_web")

    seen: dict[str, str] = {}
    order: list[tuple[str, str]] = []
    seen_budgets: dict[str, float] = {}
    base_factory = _spy_factory(seen, order)

    def factory(child_settings: Settings) -> Engine:
        seen_budgets[child_settings.project] = child_settings.budget_usd
        return base_factory(child_settings)

    prompt = "wire the endpoint"
    events = []
    result = await run_cross_project(
        settings, prompt, [a, b], deps={b: [a]},
        on_event=events.append, engine_factory=factory)

    assert result["status"] == "completed"
    # wave ordering: a fully finished before b started
    assert order.index(("end", a)) < order.index(("start", b))
    # upstream (wave-1) prompt untouched; downstream prompt gets the section
    assert seen[a] == prompt
    assert seen[b].startswith(prompt + "\n\n--- Upstream results ---\n")
    assert f"[{a}] status: completed" in seen[b]
    assert "Done." in seen[b]  # upstream summary (brief tldr) injected
    # budget still split equally across all children
    assert all(abs(v - 5.0) < 1e-9 for v in seen_budgets.values())
    # wave marker on child events
    waves = {e.data["slug"]: e.data["wave"] for e in events if e.type == "child_start"}
    assert waves == {a: 1, b: 2}
    done_waves = {e.data["slug"]: e.data["wave"] for e in events if e.type == "child_done"}
    assert done_waves == {a: 1, b: 2}
    # rollup records the dependency order
    rollup = (settings.docs_dir / result["parent_id"] / "rollup.md").read_text()
    assert "Dependency order" in rollup and f"[{a}] -> [{b}]" in rollup


async def test_deps_failed_upstream_still_runs_downstream(tmp_path):
    settings = _settings(tmp_path, max_concurrent_runs=2)
    bad = projects.create_project(settings, "dep-bad")["slug"]
    good = projects.create_project(settings, "dep-good")["slug"]
    _seed(projects.project_checkout(settings, bad), "dep_bad")
    _seed(projects.project_checkout(settings, good), "dep_good")

    seen: dict[str, str] = {}
    events = []
    result = await run_cross_project(
        settings, "do the thing", [bad, good], deps={good: [bad]},
        on_event=events.append, engine_factory=_spy_factory(seen, exploding={bad}))

    by_slug = {c["slug"]: c for c in result["children"]}
    assert by_slug[bad]["status"] == "failed"
    assert by_slug[good]["status"] == "completed"  # downstream ran anyway
    # injected section states the failure, never silently skips it
    assert f"[{bad}] status: failed" in seen[good]
    assert "planner exploded" in seen[good]
    # a status event flags the failed upstream
    assert any(e.type == "status" and bad in e.message and "upstream" in e.message
               for e in events)


async def test_deps_validation_rejects_cycles_and_unknowns_before_running(tmp_path):
    import pytest

    settings = _settings(tmp_path, max_concurrent_runs=2)
    a = projects.create_project(settings, "val-a")["slug"]
    b = projects.create_project(settings, "val-b")["slug"]

    events = []
    with pytest.raises(ValueError, match="cycle"):
        await run_cross_project(settings, "x", [a, b], deps={a: [b], b: [a]},
                                on_event=events.append,
                                engine_factory=lambda s: (_ for _ in ()).throw(
                                    AssertionError("no child may start")))
    with pytest.raises(ValueError, match="unknown upstream project"):
        await run_cross_project(settings, "x", [a, b], deps={a: ["nope"]},
                                on_event=events.append,
                                engine_factory=lambda s: (_ for _ in ()).throw(
                                    AssertionError("no child may start")))
    with pytest.raises(ValueError, match="unknown project in dependencies"):
        await run_cross_project(settings, "x", [a, b], deps={"nope": [a]},
                                on_event=events.append,
                                engine_factory=lambda s: (_ for _ in ()).throw(
                                    AssertionError("no child may start")))
    with pytest.raises(ValueError, match="cannot depend on itself"):
        await run_cross_project(settings, "x", [a, b], deps={a: [a]},
                                on_event=events.append,
                                engine_factory=lambda s: (_ for _ in ()).throw(
                                    AssertionError("no child may start")))
    assert events == []  # validation fired before any run started
    store = RunStore(settings.data_dir / "runs.db")
    try:
        assert store.list(10) == []  # no parent row was ever created
    finally:
        store.close()
