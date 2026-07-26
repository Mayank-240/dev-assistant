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


def test_config_endpoint_reflects_console_override(tmp_path):
    """/api/config reads the live base, so a console edit shows up without restart."""
    c = _client(tmp_path)
    assert c.get("/api/config").json()["budget_usd"] == 0.0
    assert c.patch("/api/settings", json={"budget_usd": 3.5}).status_code == 200
    assert c.get("/api/config").json()["budget_usd"] == 3.5
    assert c.delete("/api/settings/budget_usd").status_code == 200
    assert c.get("/api/config").json()["budget_usd"] == 0.0


def test_tasks_list_rows_carry_project(tmp_path):
    """GET /api/tasks exposes each run's project (UI scopes recent tasks by it)."""
    c = _client(tmp_path)
    c.app.state.runs.start("run-proj", "scoped task", title="Scoped", project="default")
    rows = c.get("/api/tasks").json()
    row = next(r for r in rows if r["id"] == "run-proj")
    assert row["project"] == "default"


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


# ---- UI-completeness: cancelling a still-queued task must work (task-list Cancel) ----

def test_cancel_queued_task_removes_it_from_the_queue(tmp_path):
    c = _client(tmp_path)
    c.app.state.paused = True  # keep the pump from launching a real engine
    r = c.post("/api/run", json={"prompt": "wait your turn"})
    tid = r.json()["task_id"]
    assert r.json()["status"] == "queued"
    r = c.post(f"/api/run/{tid}/cancel")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert tid not in [p["task_id"] for p in c.app.state.runs.queue_pending()]
    assert c.app.state.runs.get(tid)["status"] == "cancelled"
    # cancelling again (nothing queued or running any more) is a 404, as before
    assert c.post(f"/api/run/{tid}/cancel").status_code == 404


def test_cancel_unknown_task_is_404(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/run/never-existed/cancel")
    assert r.status_code == 404 and r.json()["ok"] is False


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


# ---- F3/F6: cross-project fan-out (composer + children endpoint) ----
# orchestration/fanout.py lands concurrently; the launch path is pinned by injecting a
# fake module, and the raw endpoints tolerate 501 while the core is missing.

def _has_fanout() -> bool:
    try:
        from ai_dev_assistant.orchestration.fanout import run_cross_project  # noqa: F401
        return True
    except ImportError:
        return False


def test_run_request_has_projects_and_stagger_fields():
    assert "projects" in RunRequest.model_fields
    assert "stagger" in RunRequest.model_fields
    assert "project" in RunRequest.model_fields  # the single-project field survives


def test_run_with_unknown_project_slug_is_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/run", json={"prompt": "x", "projects": ["default", "nope"]})
    assert r.status_code == 400
    assert "nope" in r.json()["error"] and "default" not in r.json()["error"]


def test_run_with_single_projects_entry_behaves_as_project(tmp_path):
    c = _client(tmp_path)
    c.app.state.paused = True  # keep the pump from launching a real engine
    slug = c.post("/api/projects", json={"name": "Solo"}).json()["slug"]
    r = c.post("/api/run", json={"prompt": "x", "projects": [slug]})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    payload = c.app.state.runs.queue_pending()[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload.get("project") == slug
    assert not payload.get("projects")  # not routed through the fan-out path


def test_run_fanout_without_core_is_501_else_accepted(tmp_path):
    c = _client(tmp_path)
    c.app.state.paused = True
    a = c.post("/api/projects", json={"name": "Fan A"}).json()["slug"]
    b = c.post("/api/projects", json={"name": "Fan B"}).json()["slug"]
    r = c.post("/api/run", json={"prompt": "x", "projects": [a, b]})
    if _has_fanout():
        assert r.status_code == 200
    else:
        assert r.status_code == 501
        assert "not available" in r.json()["error"]


def test_fanout_launch_reaches_run_cross_project(tmp_path, monkeypatch):
    """POST /api/run with 2+ projects routes the background task through
    run_cross_project, streaming its events via the parent task's broker."""
    import sys
    import threading
    import types

    from ai_dev_assistant.orchestration.events import Event
    from ai_dev_assistant.web.server import create_app as _create_app

    called: dict = {}
    hit = threading.Event()

    async def fake_run_cross_project(settings, prompt, slugs, *, title=None,
                                     stagger=False, task_id=None, on_event=None,
                                     engine_factory=None):
        called.update(prompt=prompt, slugs=list(slugs), title=title,
                      stagger=stagger, task_id=task_id)
        if on_event:
            on_event(Event("done", "Rollup ready.", {"task_id": task_id}))
        hit.set()
        return {"parent_id": task_id, "status": "completed", "children": [],
                "docs_dir": ""}

    mod = types.ModuleType("ai_dev_assistant.orchestration.fanout")
    mod.run_cross_project = fake_run_cross_project
    monkeypatch.setitem(sys.modules, "ai_dev_assistant.orchestration.fanout", mod)

    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    app = _create_app(settings, api_token="")
    with TestClient(app) as c:  # context manager keeps the loop alive for the bg task
        a = c.post("/api/projects", json={"name": "Fan A"}).json()["slug"]
        b = c.post("/api/projects", json={"name": "Fan B"}).json()["slug"]
        r = c.post("/api/run", json={"prompt": "fan out", "projects": [a, b],
                                     "stagger": True, "title": "Fan run"})
        assert r.status_code == 200, r.text
        tid = r.json()["task_id"]
        assert hit.wait(timeout=10), "run_cross_project was never reached"
        assert called["slugs"] == [a, b]
        assert called["stagger"] is True
        assert called["task_id"] == tid
        assert called["prompt"] == "fan out" and called["title"] == "Fan run"
        # its events landed on the parent broker (same streaming as normal runs)
        broker = app.state.brokers.get(tid)
        assert broker is not None
        assert any(e["type"] == "done" for e in broker.events)


def test_children_endpoint_501_or_shape(tmp_path):
    c = _client(tmp_path)
    r = c.get("/api/tasks/parent-x/children")
    if hasattr(c.app.state.runs, "children_of"):
        assert r.status_code == 200
        assert r.json()["children"] == []  # unknown parent -> empty, never an error
    else:
        assert r.status_code == 501
        assert "not available" in r.json()["error"]


def test_children_endpoint_joins_run_rows(tmp_path, monkeypatch):
    c = _client(tmp_path)
    rows = [{"id": "child-1", "project": "proj-a", "title": "Child A", "prompt": "p",
             "status": "completed", "run_status": "completed", "quality_score": 90,
             "cost_usd": 0.4, "task_branch": "ada/child-1", "review_target": "main"}]
    monkeypatch.setattr(c.app.state.runs, "children_of",
                        lambda pid: rows if pid == "parent-x" else [], raising=False)
    body = c.get("/api/tasks/parent-x/children").json()
    assert len(body["children"]) == 1
    kid = body["children"][0]
    assert kid["id"] == "child-1"
    assert kid["slug"] == "proj-a" and kid["project"] == "proj-a"
    assert kid["status"] == "completed" and kid["run_status"] == "completed"
    assert kid["quality_score"] == 90 and kid["cost_usd"] == 0.4
    assert kid["task_branch"] == "ada/child-1" and kid["review_target"] == "main"


def test_children_of_real_store_via_parent_links(tmp_path):
    """The run store's children_of (landed with the fan-out core) feeds the endpoint."""
    c = _client(tmp_path)
    st = c.app.state.runs
    if not hasattr(st, "children_of"):
        pytest.skip("run store without children_of (landing separately)")
    st.start("fan-parent", "fan out", title="Fan", project="multi")
    st.start("fan-c1", "child one", project="proj-a")
    st.set_parent("fan-c1", "fan-parent")
    st.finish("fan-c1", status="completed", quality_score=88, cost_usd=0.2)
    body = c.get("/api/tasks/fan-parent/children").json()
    ids = [k["id"] for k in body["children"]]
    assert ids == ["fan-c1"]
    assert body["children"][0]["slug"] == "proj-a"
    assert body["children"][0]["quality_score"] == 88


def test_multi_pseudo_project_activity(tmp_path):
    """The 'multi' pseudo-project aggregates fan-out parents for the activity table."""
    c = _client(tmp_path)
    st = c.app.state.runs
    st.start("fan-parent", "fan out", title="Fan run", project="multi")
    c.app.state.running.add("fan-parent")
    st.enqueue("fan-queued", "another fan-out", "Queued fan",
               {"prompt": "another fan-out", "projects": ["a", "b"], "stagger": False})
    r = c.get("/api/projects/multi/activity")
    assert r.status_code == 200
    body = r.json()
    assert [x["id"] for x in body["running"]] == ["fan-parent"]
    assert body["running"][0]["title"] == "Fan run"
    assert [q["id"] for q in body["queued"]] == ["fan-queued"]
    # fan-out queue entries don't leak into an ordinary project's activity
    default = c.get("/api/projects/default/activity").json()
    assert "fan-queued" not in [q["id"] for q in default["queued"]]


def test_resume_multi_parent_is_501(tmp_path):
    c = _client(tmp_path)
    st = c.app.state.runs
    st.start("fan-parent", "fan out", project="multi")
    st.set_status("fan-parent", "failed")
    r = c.post("/api/tasks/fan-parent/resume")
    assert r.status_code == 501
    assert "cross-project" in r.json()["error"]


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


# ---- Cross-project combine: /api/graph and /api/memory with ?projects=a,b ----
# (read-only merged views; knowledge/combine.py)

def _seed_project_data(c, name, facts=(), memories=()):
    """Create a project via the API, then write real KG facts + memories to its
    per-project stores (the same APIs the engine uses)."""
    import dataclasses as _dc

    from ai_dev_assistant.knowledge.graph import NetworkXKnowledgeGraph
    from ai_dev_assistant.memory.store import MemoryStore

    slug = c.post("/api/projects", json={"name": name}).json()["slug"]
    s = _dc.replace(c.app.state.settings, project=slug)
    kg = NetworkXKnowledgeGraph(s.graph_path)
    for subj, rel, obj in facts:
        kg.add_fact(subj, rel, obj)
    kg.save()
    store = MemoryStore(s)
    for content in memories:
        store.remember("longterm", content, metadata={"author": "tester"})
    store.close()
    return slug


def test_graph_endpoint_combines_projects_with_sources(tmp_path):
    c = _client(tmp_path)
    a = _seed_project_data(c, "Combi A", facts=[("app.py", "imports", "flask")])
    b = _seed_project_data(c, "Combi B", facts=[("worker.py", "imports", "celery")])
    r = c.get(f"/api/graph?projects={a},{b}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["projects"] == [a, b]
    nodes = {n["id"]: n for n in body["nodes"]}
    assert nodes["app.py"]["sources"] == [a]
    assert nodes["worker.py"]["sources"] == [b]
    edges = {(e["source"], e["target"], e["relation"]): e for e in body["edges"]}
    assert edges[("app.py", "flask", "imports")]["sources"] == [a]
    assert edges[("worker.py", "celery", "imports")]["sources"] == [b]


def test_graph_endpoint_ignores_unknown_slugs(tmp_path):
    c = _client(tmp_path)
    a = _seed_project_data(c, "Combi Solo", facts=[("x", "rel", "y")])
    r = c.get(f"/api/graph?projects={a},ghost")
    assert r.status_code == 200  # unknown slugs are dropped, never a 400
    assert r.json()["projects"] == [a]


def test_graph_endpoint_without_projects_param_unchanged(tmp_path):
    c = _client(tmp_path)
    _seed_project_data(c, "Combi Base", facts=[("m.py", "imports", "os")])
    body = c.get("/api/graph").json()
    # exactly today's single-project shape: no "projects" list, no "sources"
    assert set(body) == {"project", "nodes", "edges"}
    assert body["project"] == "default"
    assert all("sources" not in n for n in body["nodes"])


def test_memory_endpoint_combines_projects_tagged(tmp_path):
    c = _client(tmp_path)
    a = _seed_project_data(c, "Mem A", memories=["postgres pooling tip from a"])
    b = _seed_project_data(c, "Mem B", memories=["postgres tuning tip from b"])
    # listing (no q): merged recent rows, each tagged with its project
    rows = c.get(f"/api/memory?projects={a},{b}").json()
    assert {row["project"] for row in rows} == {a, b}
    assert all({"id", "scope", "content", "created_at", "mem_scope"} <= set(row)
               for row in rows)
    # search (&q=): scored hits tagged with their project, sorted by score desc
    hits = c.get(f"/api/memory?projects={a},{b}&q=postgres").json()
    assert hits
    assert {h["project"] for h in hits if "postgres" in h["content"]} == {a, b}
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_memory_endpoint_projects_with_unknown_slug(tmp_path):
    c = _client(tmp_path)
    a = _seed_project_data(c, "Mem Solo", memories=["a lesson to find"])
    rows = c.get(f"/api/memory?projects={a},ghost").json()
    assert rows and all(row["project"] == a for row in rows)


def test_memory_endpoint_without_projects_param_unchanged(tmp_path):
    import dataclasses as _dc

    from ai_dev_assistant.memory.store import MemoryStore

    c = _client(tmp_path)
    store = MemoryStore(_dc.replace(c.app.state.settings, project="default"))
    store.remember("longterm", "a default-project lesson")
    store.close()
    rows = c.get("/api/memory").json()
    # exactly today's item shape: no additive "project"/"score" fields
    assert rows
    assert all("project" not in row and "score" not in row for row in rows)
    assert rows[0]["mem_scope"] in ("project", "global")


def test_deliver_endpoint_merges_branch_and_records_decisions(tmp_path, monkeypatch):
    """POST /api/tasks/{id}/deliver: 409 while active, merges the task branch into
    the review target on a terminal run, records undecided subtasks as accepted."""
    import subprocess

    from ai_dev_assistant import projects as projmod
    from ai_dev_assistant.config import Settings as _S

    settings = _S(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    client = TestClient(create_app(settings, api_token=""))
    app = client.app
    proj = projmod.create_project(settings, "Ship It")
    repo = projmod.project_checkout(settings, proj["slug"])

    def g(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=repo, check=True, capture_output=True)

    g("checkout", "-q", "-b", "ada/t-del")
    (repo / "done.md").write_text("done\n")
    g("add", "-A"); g("commit", "-q", "-m", "work")
    g("checkout", "-q", proj["default_branch"])

    store = app.state.runs
    store.start("t-del", "ship it", project=proj["slug"])
    store.set_run_branch("t-del", "ada/t-del", proj["default_branch"])
    store.checkpoint_subtask("t-del", "s1", status="passed", attempts=1,
                             result="r", verdict_json=None)

    r = client.post("/api/tasks/t-del/deliver")
    assert r.status_code == 409  # still running

    store.finish("t-del", status="completed")
    r = client.post("/api/tasks/t-del/deliver")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged"] and body["decision"] == "accepted"
    assert "s1" in body["accepted_subtasks"]
    assert (repo / "done.md").is_file()
    assert store.get_subtask_reviews("t-del")["s1"]["decision"] == "accepted"

    # idempotent second delivery
    r2 = client.post("/api/tasks/t-del/deliver")
    assert r2.status_code == 200 and r2.json()["merged"]

    assert client.post("/api/tasks/nope/deliver").status_code == 404


# ---- Playbooks: settings_overrides thread through _settings_for ----

def test_settings_for_applies_playbook_overrides():
    from ai_dev_assistant.web.server import _settings_for

    base = Settings()
    s = _settings_for(base, None, None,
                      settings_overrides={"agent_effort": "max", "budget_usd": 3.0})
    assert s.agent_effort == "max" and s.budget_usd == 3.0
    # explicit request choices still beat the playbook's overrides
    s2 = _settings_for(base, None, 9.0, settings_overrides={"budget_usd": 3.0})
    assert s2.budget_usd == 9.0
    # unknown keys in a playbook's overrides are ignored, never a crash
    s3 = _settings_for(base, None, None, settings_overrides={"not_a_field": 1})
    assert s3 == _settings_for(base, None, None)


# ---- S8 completion: cookie-session login (the browser UI's auth path) ----
# Bearer/query-param auth itself is covered in test_web_auth.py; these pin the
# /api/auth/status + /api/login + /api/logout endpoints and the ada_token cookie.

_TOKEN = "s8-test-token"


def _auth_client(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    return TestClient(create_app(settings, api_token=_TOKEN))


def test_auth_off_keeps_api_open_and_status_says_so(tmp_path):
    c = _client(tmp_path)  # api_token="" -> auth off, exactly as before
    assert c.get("/api/tasks").status_code == 200
    assert c.get("/api/auth/status").json() == {"auth_required": False, "authorized": True}
    # login is refused when there is no token to match (nothing to set a cookie for)
    assert c.post("/api/login", json={"token": "anything"}).status_code == 403


def test_token_set_requires_credentials_but_status_stays_open(tmp_path):
    c = _auth_client(tmp_path)
    assert c.get("/api/tasks").status_code == 401
    assert c.get("/api/tasks", headers={"Authorization": f"Bearer {_TOKEN}"}).status_code == 200
    body = c.get("/api/auth/status").json()  # readable without any credentials
    assert body == {"auth_required": True, "authorized": False}
    with_bearer = c.get("/api/auth/status", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert with_bearer.json()["authorized"] is True


def test_login_sets_cookie_and_cookie_alone_authorizes(tmp_path):
    c = _auth_client(tmp_path)
    r = c.post("/api/login", json={"token": _TOKEN})
    assert r.status_code == 204
    set_cookie = r.headers["set-cookie"]
    assert "ada_token" in set_cookie and "HttpOnly" in set_cookie
    # the client's jar keeps the cookie; no Authorization header from here on
    assert c.get("/api/tasks").status_code == 200
    assert c.get("/api/auth/status").json()["authorized"] is True


def test_login_wrong_token_is_403_and_sets_no_cookie(tmp_path):
    c = _auth_client(tmp_path)
    r = c.post("/api/login", json={"token": "wrong"})
    assert r.status_code == 403
    assert "set-cookie" not in r.headers
    assert c.get("/api/tasks").status_code == 401
    assert c.post("/api/login", json={}).status_code == 403  # blank never matches


def test_logout_clears_cookie(tmp_path):
    c = _auth_client(tmp_path)
    assert c.post("/api/login", json={"token": _TOKEN}).status_code == 204
    assert c.get("/api/tasks").status_code == 200
    assert c.post("/api/logout").status_code == 204
    assert c.get("/api/tasks").status_code == 401


def test_ws_accepts_session_cookie(tmp_path):
    c = _auth_client(tmp_path)
    with c.websocket_connect("/ws/whatever",
                             headers={"cookie": f"ada_token={_TOKEN}"}) as ws:
        assert ws.receive_json()["type"] == "error"  # unknown task, but authenticated
