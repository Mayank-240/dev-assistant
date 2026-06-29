"""Interactive plan mode: propose → refine-via-instruction → approve."""

from __future__ import annotations

import pytest

from ai_dev_assistant.cli import _build_parser
from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.llm.schemas import Plan, SubTask


class _Usage:
    cost_usd = 0.0
    def to_dict(self): return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    def snapshot(self): return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}


class RefineProvider:
    """make_plan → one coder subtask; a 'security' refine adds a reviewer subtask."""

    def __init__(self): self.usage = _Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            subs = [SubTask(id="s1", title="Implement", description="d", agent="coder",
                            rationale="r", acceptance_criteria=["works"])]
            # only a *refine* call (distinct system prompt) carrying a security instruction adds a step
            if "refining" in system.lower() and "security" in user.lower():
                subs.append(SubTask(id="s2", title="Security review", description="d",
                                    agent="security_auditor", rationale="r", depends_on=["s1"],
                                    acceptance_criteria=["no vulns"]))
            return Plan(title="T", summary="s", subtasks=subs)
        raise AssertionError(f"unexpected schema {schema}")

    async def run_agent(self, **kw): return "done"
    async def aclose(self): pass


def _settings(tmp_path):
    return Settings(llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
                    data_dir=tmp_path / "d", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws")


async def test_engine_refine_applies_instruction(tmp_path):
    engine = Engine(_settings(tmp_path))
    fake = RefineProvider()
    engine.provider = fake
    engine.orchestrator._provider = fake
    try:
        plan = await engine.make_plan("Build a login endpoint")
        assert len(plan.subtasks) == 1
        revised = await engine.refine_plan("Build a login endpoint", plan, "add a security review step")
        assert len(revised.subtasks) == 2
        assert any(s.agent == "security_auditor" for s in revised.subtasks)
        # the refined plan is sanitized + structurally valid (no dangling/self deps, no cycle)
        from ai_dev_assistant.orchestration.task import TaskRun
        TaskRun.from_plan("Build a login endpoint", revised).validate()
    finally:
        await engine.aclose()


def test_cli_accepts_interactive_flag():
    args = _build_parser().parse_args(["run", "do a thing", "-i"])
    assert args.interactive is True
    args2 = _build_parser().parse_args(["run", "do a thing"])
    assert args2.interactive is False


def test_refine_endpoint_requires_instruction(tmp_path):
    tc = pytest.importorskip("fastapi.testclient")
    from ai_dev_assistant.web.server import create_app
    client = tc.TestClient(create_app(_settings(tmp_path)))
    r = client.post("/api/plan/refine", json={"prompt": "x", "plan": {"summary": "s", "subtasks": []}, "instruction": ""})
    assert r.status_code == 400
