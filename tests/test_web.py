"""Smoke tests for the web surface, including the Tier 4/5 endpoints (no LLM needed)."""

from __future__ import annotations

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.web.server import create_app

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


def _client(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    return TestClient(create_app(settings))


def test_health_and_ready(tmp_path):
    c = _client(tmp_path)
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/readyz").status_code == 200


def test_agents_and_stats(tmp_path):
    c = _client(tmp_path)
    agents = c.get("/api/agents").json()
    assert any(a["name"] == "coder" for a in agents)
    # new tools are exposed to full agents
    assert "write_file" in next(a for a in agents if a["name"] == "coder")["tools"]
    stats = c.get("/api/stats").json()
    assert "total_cost_usd" in stats and "by_status" in stats


def test_feedback_roundtrip(tmp_path):
    c = _client(tmp_path)
    c.app.state.runs.start("run-1", "a task")
    r = c.post("/api/run/run-1/feedback", json={"rating": 5, "accepted": True, "comment": "great"})
    assert r.json()["ok"] is True
    fb = c.get("/api/run/run-1/feedback").json()
    assert fb["rating"] == 5 and fb["accepted"] == 1 and fb["comment"] == "great"


def test_quality_and_events_endpoints(tmp_path):
    c = _client(tmp_path)
    assert "trend" in c.get("/api/quality").json()
    assert c.get("/api/tasks/does-not-exist/events").json() == []
    assert c.get("/api/tasks/does-not-exist/trace").json() == []


def test_effort_tiers_including_xhigh_and_max():
    from ai_dev_assistant.web.server import _EFFORT, _settings_for

    base = Settings()
    assert {"low", "medium", "high", "xhigh", "max"} <= set(_EFFORT)

    # high reproduces the env-default role mix (no surprise cost change)
    hi = _settings_for(base, "high", None)
    assert (hi.orchestrator_effort, hi.agent_effort, hi.reviewer_effort) == ("high", "medium", "high")

    # the new tiers raise reasoning effort, turns, and retries monotonically
    xh = _settings_for(base, "xhigh", None)
    mx = _settings_for(base, "max", None)
    assert xh.orchestrator_effort == "xhigh" and xh.reviewer_effort == "xhigh"
    assert mx.orchestrator_effort == "max" and mx.agent_effort == "max" and mx.reviewer_effort == "max"
    assert hi.agent_max_turns < xh.agent_max_turns < mx.agent_max_turns
    assert mx.max_retries >= 2
    # higher tiers keep Opus (no cheaper-model override)
    assert xh.sdk_model == base.sdk_model and mx.sdk_model == base.sdk_model
