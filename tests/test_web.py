"""Smoke tests for the web surface, including the Tier 4/5 endpoints (no LLM needed)."""

from __future__ import annotations

import json
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


# ---- F4 (decision #5): Reviews & Permissions panel + per-subtask accept/reject ----
# The run-store review columns/methods, the engine's policy event, and
# projects.accept_commit / vcs.cherry_pick_merge all land concurrently; the
# endpoints are guarded, so these tests accept 501 where a dependency may be
# missing and stub the contracts to pin the full happy/conflict paths.

_PLAN = {"title": "T", "summary": "s",
         "subtasks": [{"id": "s1", "title": "Do the thing", "agent": "coder"}]}


def _seed_terminal_run(c, tid="task-r", status="completed", project="default"):
    st = c.app.state.runs
    st.start(tid, "review me", title="Review me", project=project)
    st.save_plan(tid, json.dumps(_PLAN))
    st.checkpoint_subtask(tid, "s1", status="passed", attempts=1, result="done",
                          verdict_json=json.dumps({"passed": True, "score": 92,
                                                   "reasons": ["solid"],
                                                   "suggestions": ["more tests"]}))
    st.set_status(tid, status)
    return st


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_review_unknown_task_is_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/tasks/nope/review").status_code == 404


def test_review_endpoint_shape_or_501(tmp_path):
    c = _client(tmp_path)
    _seed_terminal_run(c)
    r = c.get("/api/tasks/task-r/review")
    assert r.status_code in (200, 501)
    if r.status_code == 501:
        return
    body = r.json()
    assert {"task_branch", "review_target", "subtasks"} <= set(body)
    assert body["task_branch"]  # falls back to ada/<task-id> pre-contract
    sub = next(s for s in body["subtasks"] if s["id"] == "s1")
    assert sub["title"] == "Do the thing" and sub["agent"] == "coder"
    assert sub["verdict"]["score"] == 92 and sub["verdict"]["passed"] is True
    assert isinstance(sub["changed"], list)
    assert sub["decision"] is None
    assert sub["diff"] == ""  # no merge commit / repo -> empty, never an error


def test_review_titles_from_plan_event(tmp_path):
    c = _client(tmp_path)
    st = c.app.state.runs
    st.start("task-e", "p", project="default")
    st.checkpoint_subtask("task-e", "s9", status="failed", attempts=2, result="",
                          verdict_json=json.dumps({"passed": False, "score": 30,
                                                   "reasons": ["broken"]}))
    st.set_status("task-e", "partial")
    _write_jsonl(tmp_path / "docs" / "task-e" / "events.jsonl", [
        {"type": "plan", "message": "planned",
         "data": {"summary": "s", "subtasks": [{"id": "s9", "title": "From event",
                                                "agent": "debugger"}]}},
    ])
    r = c.get("/api/tasks/task-e/review")
    assert r.status_code in (200, 501)
    if r.status_code == 200:
        sub = next(s for s in r.json()["subtasks"] if s["id"] == "s9")
        assert sub["title"] == "From event" and sub["agent"] == "debugger"
        assert sub["verdict"]["passed"] is False


def test_permissions_tolerates_missing_events_and_audit(tmp_path):
    c = _client(tmp_path)
    c.app.state.runs.start("task-p", "p")
    body = c.get("/api/tasks/task-p/permissions").json()
    assert body["policy"] == {} and body["tools_by_agent"] == {}
    assert body["denied"] == []
    # even a task with no run row / docs answers with empty fields, not an error
    assert c.get("/api/tasks/ghost/permissions").status_code == 200


def test_permissions_reads_policy_event_and_denied_audit_lines(tmp_path):
    c = _client(tmp_path)
    tid = "task-perm"
    _write_jsonl(tmp_path / "docs" / tid / "events.jsonl", [
        {"type": "status", "message": "hi", "data": {}},
        {"type": "policy", "message": "resolved policy",
         "data": {"policy": {"budget_usd": 5, "git_mode": "branch",
                             "protected_paths": ["infra/**"]},
                  "tools_by_agent": {"coder": ["write_file", "run_tests"]},
                  "review_target": "main", "task_branch": f"ada/{tid}"}},
    ])
    _write_jsonl(tmp_path / "docs" / tid / "audit.jsonl", [
        {"ts": 1.0, "agent": "coder", "tool": "write_file", "outcome": "ok"},
        {"ts": 2.0, "agent": "coder", "tool": "write_file",
         "outcome": "DENIED: protected path infra/main.tf"},
    ])
    body = c.get(f"/api/tasks/{tid}/permissions").json()
    assert body["policy"]["budget_usd"] == 5
    assert body["policy"]["protected_paths"] == ["infra/**"]
    assert body["tools_by_agent"]["coder"] == ["write_file", "run_tests"]
    assert body["review_target"] == "main" and body["task_branch"] == f"ada/{tid}"
    assert len(body["denied"]) == 1
    assert body["denied"][0]["outcome"].startswith("DENIED:")
    assert body["denied"][0]["agent"] == "coder"


def test_accept_unknown_task_is_404(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/tasks/nope/subtasks/s1/accept").status_code == 404


def test_accept_non_terminal_run_is_409(tmp_path):
    c = _client(tmp_path)
    c.app.state.runs.start("task-live", "p")  # status 'running'
    r = c.post("/api/tasks/task-live/subtasks/s1/accept")
    assert r.status_code == 409
    assert "running" in r.json()["error"]


def test_accept_missing_merge_commit_is_409(tmp_path):
    c = _client(tmp_path)
    _seed_terminal_run(c)  # checkpointed subtask, but no merge_commit recorded
    r = c.post("/api/tasks/task-r/subtasks/s1/accept")
    assert r.status_code == 409
    assert "merge commit" in r.json()["error"]


def test_accept_unknown_subtask_is_404(tmp_path):
    c = _client(tmp_path)
    _seed_terminal_run(c)
    assert c.post("/api/tasks/task-r/subtasks/does-not-exist/accept").status_code == 404


def _stub_accept_contracts(monkeypatch, c, tid, result):
    """Pin the concurrent contracts: subtask rows w/ merge_commit, the review
    recorder, and projects.accept_commit returning `result`."""
    states = {"s1": {"status": "passed", "attempts": 1, "result": "done",
                     "verdict": json.dumps({"passed": True, "score": 92}),
                     "merge_commit": "abc1234def", "changed": json.dumps(["a.py"])}}
    monkeypatch.setattr(c.app.state.runs, "get_subtask_states",
                        lambda rid: states if rid == tid else {}, raising=False)
    recorded = {}
    monkeypatch.setattr(
        c.app.state.runs, "set_subtask_review",
        lambda rid, sid, decision, comment: recorded.update(
            run=rid, sid=sid, decision=decision, comment=comment),
        raising=False)
    calls = {}
    def fake_accept(settings, slug, sha):
        calls.update(slug=slug, sha=sha)
        return result
    monkeypatch.setattr(projects, "accept_commit", fake_accept, raising=False)
    return recorded, calls


def test_accept_success_merges_and_records_decision(tmp_path, monkeypatch):
    c = _client(tmp_path)
    _seed_terminal_run(c)
    recorded, calls = _stub_accept_contracts(
        monkeypatch, c, "task-r",
        {"merged": True, "conflict": False, "files": ["a.py"], "commit": "deadbeef"})
    r = c.post("/api/tasks/task-r/subtasks/s1/accept")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged"] is True and body["decision"] == "accepted"
    assert calls == {"slug": "default", "sha": "abc1234def"}
    assert recorded == {"run": "task-r", "sid": "s1", "decision": "accepted", "comment": ""}


def test_accept_conflict_is_409_with_files(tmp_path, monkeypatch):
    c = _client(tmp_path)
    _seed_terminal_run(c)
    recorded, _calls = _stub_accept_contracts(
        monkeypatch, c, "task-r",
        {"merged": False, "conflict": True, "files": ["a.py", "b.py"]})
    r = c.post("/api/tasks/task-r/subtasks/s1/accept")
    assert r.status_code == 409
    body = r.json()
    assert body["conflict"] is True and body["files"] == ["a.py", "b.py"]
    assert recorded == {}  # a conflicted merge records no decision


def test_reject_records_review_and_feedback(tmp_path, monkeypatch):
    c = _client(tmp_path)
    _seed_terminal_run(c)
    recorded = {}
    monkeypatch.setattr(
        c.app.state.runs, "set_subtask_review",
        lambda rid, sid, decision, comment: recorded.update(
            run=rid, sid=sid, decision=decision, comment=comment),
        raising=False)
    r = c.post("/api/tasks/task-r/subtasks/s1/reject", json={"comment": "not right"})
    assert r.status_code == 200
    assert r.json()["decision"] == "rejected"
    assert recorded == {"run": "task-r", "sid": "s1", "decision": "rejected",
                        "comment": "not right"}
    # the rejection lands in the run's feedback so learning consumes it
    fb = c.app.state.runs.get_feedback("task-r")
    assert fb["accepted"] == 0 and fb["comment"] == "not right"


def test_reject_without_store_support_is_501_else_200(tmp_path):
    c = _client(tmp_path)
    _seed_terminal_run(c)
    r = c.post("/api/tasks/task-r/subtasks/s1/reject", json={})
    expected = 200 if hasattr(c.app.state.runs, "set_subtask_review") else 501
    assert r.status_code == expected
    if expected == 501:
        assert "not available" in r.json()["error"]


def test_reject_unknown_task_is_404(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/tasks/nope/subtasks/s1/reject", json={}).status_code == 404


# ---- F4: project home — per-project run history ----

def test_project_runs_lists_only_that_project(tmp_path):
    c = _client(tmp_path)
    st = c.app.state.runs
    st.start("run-1", "a", title="A", project="default")
    st.finish("run-1", status="completed", quality_score=90, cost_usd=0.5, tests="passed")
    st.start("run-2", "b", title="B", project="other-project")
    st.finish("run-2", status="completed", quality_score=10)
    r = c.get("/api/projects/default/runs")
    assert r.status_code == 200
    rows = r.json()
    ids = [x["id"] for x in rows]
    assert "run-1" in ids and "run-2" not in ids
    row = next(x for x in rows if x["id"] == "run-1")
    assert row["quality_score"] == 90 and row["tests"] == "passed"
    assert row["title"] == "A" and row["status"] == "completed"


def test_project_runs_unknown_project_is_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/projects/nope/runs").status_code == 404


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
