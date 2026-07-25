"""Smoke tests for the web surface, including the Tier 4/5 endpoints (no LLM needed)."""

from __future__ import annotations

import subprocess

import pytest

from ai_dev_assistant import projects
from ai_dev_assistant.config import Settings
from ai_dev_assistant.web.server import RunRequest, create_app

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

# projects.py is being rewritten in a parallel change; these mark the endpoints that
# depend on its new surface. They flip on automatically once the functions land.
needs_import = pytest.mark.skipif(
    not hasattr(projects, "import_project"), reason="projects.import_project not landed yet")
needs_status = pytest.mark.skipif(
    not hasattr(projects, "project_status"), reason="projects.project_status not landed yet")
needs_delete = pytest.mark.skipif(
    not hasattr(projects, "delete_project"), reason="projects.delete_project not landed yet")


def _client(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    # api_token="" forces auth off regardless of a developer's ADA_API_TOKEN env
    # (auth behavior itself is covered in test_web_auth.py)
    return TestClient(create_app(settings, api_token=""))


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


# ---- F1/F6: project lifecycle endpoints ----

def _seed_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    def _git(*args):
        subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                        *args], check=True, capture_output=True)
    _git("init")
    (path / "README.md").write_text("hello\n")
    _git("add", "-A")
    _git("commit", "-m", "init")
    return path


@needs_import
def test_project_import_happy_path(tmp_path):
    src = _seed_git_repo(tmp_path / "srcrepo")
    c = _client(tmp_path)
    r = c.post("/api/projects/import", json={"source": str(src), "name": "Imported One"})
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry.get("slug")
    slugs = [p["slug"] for p in c.get("/api/projects").json()]
    assert entry["slug"] in slugs


@needs_import
def test_project_import_bad_source_is_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/projects/import",
               json={"source": str(tmp_path / "definitely-not-a-repo")})
    assert r.status_code == 400
    assert r.json()["error"]


def test_project_import_blank_source_is_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/projects/import", json={"source": "   "})
    assert r.status_code == 400
    assert "source" in r.json()["error"]


@needs_import
@needs_status
def test_project_status_shape(tmp_path):
    src = _seed_git_repo(tmp_path / "srcrepo")
    c = _client(tmp_path)
    slug = c.post("/api/projects/import", json={"source": str(src)}).json()["slug"]
    st = c.get(f"/api/projects/{slug}/status")
    assert st.status_code == 200, st.text
    body = st.json()
    assert {"slug", "branch", "head", "dirty", "origin", "root",
            "last_indexed_commit", "archived"} <= set(body)
    assert body["slug"] == slug


def test_project_status_unknown_project_is_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/projects/nope/status").status_code == 404


def test_project_activity_running_queued_recent(tmp_path):
    c = _client(tmp_path)
    st = c.app.state.runs
    # running: run row + membership in the live running set
    st.start("run-live", "implement the parser", title="Parser work", project="default")
    c.app.state.running.add("run-live")
    # queued: a pending queue entry whose payload targets the default project
    st.enqueue("run-waiting", "write docs", "Docs task",
               {"prompt": "write docs", "project": None})
    # recent: a finished run
    st.start("run-old", "old task", title="Old task", project="default")
    st.finish("run-old", status="completed", cost_usd=0.5, quality_score=90)
    act = c.get("/api/projects/default/activity")
    assert act.status_code == 200
    body = act.json()
    assert body["slug"] == "default"
    assert [r["id"] for r in body["running"]] == ["run-live"]
    assert body["running"][0]["title"] == "Parser work"
    assert [q["id"] for q in body["queued"]] == ["run-waiting"]
    recent_ids = [r["id"] for r in body["recent"]]
    assert "run-old" in recent_ids and len(body["recent"]) <= 5
    done = next(r for r in body["recent"] if r["id"] == "run-old")
    assert done["status"] == "completed" and done["quality_score"] == 90


def test_project_activity_unknown_project_is_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/projects/nope/activity").status_code == 404


def test_delete_default_project_is_409(tmp_path):
    c = _client(tmp_path)
    r = c.delete("/api/projects/default")
    assert r.status_code == 409
    assert "default" in r.json()["error"]


def test_delete_project_refuses_while_running(tmp_path):
    c = _client(tmp_path)
    slug = c.post("/api/projects", json={"name": "Busy Project"}).json()["slug"]
    c.app.state.runs.start("run-busy", "work", project=slug)
    c.app.state.running.add("run-busy")
    r = c.delete(f"/api/projects/{slug}")
    assert r.status_code == 409
    assert "running" in r.json()["error"]


@needs_delete
def test_delete_idle_project_succeeds(tmp_path):
    c = _client(tmp_path)
    slug = c.post("/api/projects", json={"name": "Ephemeral"}).json()["slug"]
    r = c.delete(f"/api/projects/{slug}")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert slug not in [p["slug"] for p in c.get("/api/projects").json()]


# ---- Clean break: RunRequest no longer carries per-run repo binding ----

def test_run_request_has_no_repo_fields(tmp_path):
    for gone in ("repo_path", "repo_url", "repo_ref"):
        assert gone not in RunRequest.model_fields
    assert "git_finalize" in RunRequest.model_fields  # kept
    assert "project" in RunRequest.model_fields       # how a run targets a repo now
    # posting a stray repo_path is simply ignored (pydantic drops unknown fields)
    c = _client(tmp_path)
    c.app.state.paused = True  # keep the pump from launching a real engine
    r = c.post("/api/run", json={"prompt": "x", "repo_path": "/anywhere"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"


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
