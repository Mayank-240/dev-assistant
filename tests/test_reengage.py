"""Re-engage: continuing a completed task carries its workspace + context forward."""

from __future__ import annotations

from pathlib import Path

from ai_dev_assistant.cli import _build_parser
from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.llm.schemas import BriefDoc, Plan, RunLessons, SubTask, Verdict


class _Usage:
    cost_usd = 0.0
    def to_dict(self): return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    def snapshot(self): return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}


class WriterProvider:
    """Plans one coder subtask that writes a named file into the workspace."""

    def __init__(self, filename: str):
        self.usage = _Usage()
        self.filename = filename
        self.saw_continuation = False

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            if "CONTINUES a previously-completed task" in user:
                self.saw_continuation = True
            return Plan(title="T", summary="s", subtasks=[
                SubTask(id="s1", title="Work", description="d", agent="coder", rationale="r",
                        acceptance_criteria=["produced"])])
        if schema is Verdict:
            return Verdict(passed=True, score=90)
        if schema is BriefDoc:
            return BriefDoc(tldr="done", key_points=["k"], status="completed")
        if schema is RunLessons:
            return RunLessons(summary="ok")
        raise AssertionError(f"unexpected schema {schema}")

    async def run_agent(self, *, workdir, **kw):
        Path(workdir, self.filename).write_text(f"# {self.filename}\n")
        return "done"

    async def aclose(self): pass


def _settings(tmp_path):
    return Settings(llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
                    data_dir=tmp_path / "d", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
                    session_idle_ttl=0.05, reaper_interval=0.02, max_retries=0,
                    verify_run_tests=False, objective_review=True, lint_check=False)


def _wire(engine, provider):
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider
    engine.reflector._provider = provider


async def test_reengage_carries_workspace_and_links_parent(tmp_path):
    settings = _settings(tmp_path)

    e1 = Engine(settings)
    _wire(e1, WriterProvider("parent.py"))
    try:
        parent, _b, _o = await e1.run("Build the thing")
    finally:
        await e1.aclose()
    pid = parent.id
    assert (settings.workspace_dir / pid / "parent.py").is_file()

    e2 = Engine(settings)
    cont = WriterProvider("child.py")
    _wire(e2, cont)
    try:
        child, _b2, _o2 = await e2.run("Now add a feature", continue_from=pid)
    finally:
        await e2.aclose()
    cws = settings.workspace_dir / child.id

    assert cont.saw_continuation                    # the planner was told it's a continuation
    assert (cws / "parent.py").is_file()            # prior workspace carried forward
    assert (cws / "child.py").is_file()             # new work added on top
    # lineage recorded (read via a fresh store — the engines closed theirs)
    from ai_dev_assistant.orchestration.run_store import RunStore
    rs = RunStore(settings.data_dir / "runs.db")
    try:
        assert rs.get(child.id)["parent_id"] == pid
    finally:
        rs.close()


def test_cli_accepts_continue_flag():
    args = _build_parser().parse_args(["run", "do more", "--continue", "20260101-000000-abc123"])
    assert args.continue_from == "20260101-000000-abc123"
    assert _build_parser().parse_args(["run", "x"]).continue_from is None
