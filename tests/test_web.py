"""Smoke tests for the web surface, including the Tier 4/5 endpoints (no LLM needed)."""

from __future__ import annotations

import asyncio
import dataclasses
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
    body = c.get("/api/agents").json()
    builtin = body["builtin"]
    assert any(a["name"] == "coder" for a in builtin)
    # new tools are exposed to full agents
    assert "write_file" in next(a for a in builtin if a["name"] == "coder")["tools"]
    assert body["custom"] == []                       # nothing user-defined yet
    assert "read_file" in body["tools"]               # the toolbox universe
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
    """Pin the accept contracts: a subtask row with a merge commit in the real
    store, plus projects.accept_commit (the git half) returning `result`. The
    endpoint routes through projects.accept_subtask, which calls accept_commit
    and records the decision + cherry-pick sha on subtask_reviews itself."""
    c.app.state.runs.checkpoint_subtask(
        tid, "s1", status="passed", attempts=1, result="done",
        verdict_json=json.dumps({"passed": True, "score": 92}),
        merge_commit="abc1234def", changed_json=json.dumps(["a.py"]))
    calls = {}
    def fake_accept(settings, slug, sha):
        calls.update(slug=slug, sha=sha)
        return result
    monkeypatch.setattr(projects, "accept_commit", fake_accept, raising=False)
    return calls


def test_accept_success_merges_and_records_decision(tmp_path, monkeypatch):
    c = _client(tmp_path)
    _seed_terminal_run(c)
    calls = _stub_accept_contracts(
        monkeypatch, c, "task-r",
        {"merged": True, "conflict": False, "files": ["a.py"], "commit": "deadbeef"})
    r = c.post("/api/tasks/task-r/subtasks/s1/accept")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged"] is True and body["decision"] == "accepted"
    assert calls == {"slug": "default", "sha": "abc1234def"}
    review = c.app.state.runs.get_subtask_reviews("task-r")["s1"]
    assert review["decision"] == "accepted"
    # rollback bookkeeping: the cherry-pick sha the acceptance created is recorded
    assert review["accepted_commit"] == "deadbeef"


def test_accept_conflict_is_409_with_files(tmp_path, monkeypatch):
    c = _client(tmp_path)
    _seed_terminal_run(c)
    _stub_accept_contracts(
        monkeypatch, c, "task-r",
        {"merged": False, "conflict": True, "files": ["a.py", "b.py"]})
    r = c.post("/api/tasks/task-r/subtasks/s1/accept")
    assert r.status_code == 409
    body = r.json()
    assert body["conflict"] is True and body["files"] == ["a.py", "b.py"]
    assert c.app.state.runs.get_subtask_reviews("task-r") == {}  # no decision recorded


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


def test_run_request_has_project_deps_field():
    assert "project_deps" in RunRequest.model_fields


def test_run_project_deps_cycle_is_400(tmp_path):
    if not _has_fanout():
        pytest.skip("fan-out core not landed")
    c = _client(tmp_path)
    c.app.state.paused = True
    a = c.post("/api/projects", json={"name": "Dep A"}).json()["slug"]
    b = c.post("/api/projects", json={"name": "Dep B"}).json()["slug"]
    r = c.post("/api/run", json={"prompt": "x", "projects": [a, b],
                                 "project_deps": {a: [b], b: [a]}})
    assert r.status_code == 400
    assert "cycle" in r.json()["error"]
    assert c.app.state.runs.queue_pending() == []  # rejected before enqueue


def test_run_project_deps_unknown_slug_is_400(tmp_path):
    if not _has_fanout():
        pytest.skip("fan-out core not landed")
    c = _client(tmp_path)
    c.app.state.paused = True
    a = c.post("/api/projects", json={"name": "Dep C"}).json()["slug"]
    b = c.post("/api/projects", json={"name": "Dep D"}).json()["slug"]
    r = c.post("/api/run", json={"prompt": "x", "projects": [a, b],
                                 "project_deps": {a: ["not-in-run"]}})
    assert r.status_code == 400
    assert "unknown upstream project" in r.json()["error"]


def test_run_project_deps_persisted_in_queue_payload(tmp_path):
    if not _has_fanout():
        pytest.skip("fan-out core not landed")
    c = _client(tmp_path)
    c.app.state.paused = True  # keep the pump from launching a real fan-out
    a = c.post("/api/projects", json={"name": "Dep E"}).json()["slug"]
    b = c.post("/api/projects", json={"name": "Dep F"}).json()["slug"]
    r = c.post("/api/run", json={"prompt": "x", "projects": [a, b],
                                 "project_deps": {b: [a]}})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    payload = c.app.state.runs.queue_pending()[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["projects"] == [a, b]
    assert payload["project_deps"] == {b: [a]}


def test_fanout_launch_passes_project_deps_through(tmp_path, monkeypatch):
    """POST /api/run with project_deps routes deps= into run_cross_project."""
    import sys
    import threading
    import types

    from ai_dev_assistant.orchestration import fanout as real_fanout
    from ai_dev_assistant.web.server import create_app as _create_app

    called: dict = {}
    hit = threading.Event()

    async def fake_run_cross_project(settings, prompt, slugs, *, title=None,
                                     stagger=False, task_id=None, on_event=None,
                                     engine_factory=None, deps=None):
        called.update(prompt=prompt, slugs=list(slugs), deps=deps)
        hit.set()
        return {"parent_id": task_id, "status": "completed", "children": [],
                "docs_dir": ""}

    mod = types.ModuleType("ai_dev_assistant.orchestration.fanout")
    mod.run_cross_project = fake_run_cross_project
    mod.validate_project_deps = real_fanout.validate_project_deps
    mod._dependency_waves = real_fanout._dependency_waves
    monkeypatch.setitem(sys.modules, "ai_dev_assistant.orchestration.fanout", mod)

    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    app = _create_app(settings, api_token="")
    with TestClient(app) as c:
        a = c.post("/api/projects", json={"name": "Dep G"}).json()["slug"]
        b = c.post("/api/projects", json={"name": "Dep H"}).json()["slug"]
        r = c.post("/api/run", json={"prompt": "fan out", "projects": [a, b],
                                     "project_deps": {b: [a]}})
        assert r.status_code == 200, r.text
        assert hit.wait(timeout=10), "run_cross_project was never reached"
        assert called["slugs"] == [a, b]
        assert called["deps"] == {b: [a]}


def test_fanout_launch_without_deps_omits_kwarg(tmp_path, monkeypatch):
    """Old-style payloads (no project_deps) never pass deps= — fakes and older
    cores without the kwarg keep working."""
    import sys
    import threading
    import types

    from ai_dev_assistant.web.server import create_app as _create_app

    hit = threading.Event()

    async def fake_run_cross_project(settings, prompt, slugs, *, title=None,
                                     stagger=False, task_id=None, on_event=None,
                                     engine_factory=None):  # note: no deps kwarg
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
    with TestClient(app) as c:
        a = c.post("/api/projects", json={"name": "Dep I"}).json()["slug"]
        b = c.post("/api/projects", json={"name": "Dep J"}).json()["slug"]
        r = c.post("/api/run", json={"prompt": "fan out", "projects": [a, b]})
        assert r.status_code == 200, r.text
        assert hit.wait(timeout=10), "run_cross_project was never reached"


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


# ---- landing page vs console shell ----

def test_root_serves_landing_and_app_serves_console(tmp_path):
    c = _client(tmp_path)
    landing = c.get("/")
    assert landing.status_code == 200
    assert "Open console" in landing.text and 'href="/app"' in landing.text
    console = c.get("/app")
    assert console.status_code == 200
    assert 'id="palette-modal"' in console.text  # the SPA shell, not the landing


def test_landing_and_console_open_with_auth_enabled(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    c = TestClient(create_app(settings, api_token="sekrit-token"))
    assert c.get("/").status_code == 200
    assert c.get("/app").status_code == 200
    assert c.get("/api/projects").status_code == 401


# ---- QoL wave: memory curation API ----

def _mem_store(c, slug="default"):
    from ai_dev_assistant.memory.store import MemoryStore
    return MemoryStore(dataclasses.replace(c.app.state.settings, project=slug))


def test_project_memories_list_update_delete_roundtrip(tmp_path):
    c = _client(tmp_path)
    store = _mem_store(c)
    store.remember("longterm", "prefer recursive descent parsers")
    store.remember("longterm", "tests live under tests/")
    store.remember("episodic", "run 42 fixed the tokenizer")
    store.close()

    body = c.get("/api/projects/default/memories").json()
    assert body["project"] == "default" and body["total"] == 3
    rows = body["memories"]  # newest-first
    assert [r["content"] for r in rows] == ["run 42 fixed the tokenizer",
                                            "tests live under tests/",
                                            "prefer recursive descent parsers"]
    assert all(r["project"] == "default" for r in rows)
    assert {"id", "scope", "key", "content", "metadata", "created_at"} <= set(rows[0])

    # scope filter and pagination
    lt = c.get("/api/projects/default/memories", params={"scope": "longterm"}).json()
    assert lt["total"] == 2 and all(r["scope"] == "longterm" for r in lt["memories"])
    page = c.get("/api/projects/default/memories",
                 params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and len(page["memories"]) == 1
    assert page["memories"][0]["content"] == "tests live under tests/"

    # edit round-trips
    mid = rows[-1]["id"]
    r = c.patch(f"/api/projects/default/memories/{mid}",
                json={"content": "prefer PEG parsers"})
    assert r.status_code == 200 and r.json() == {"ok": True, "id": mid,
                                                 "content": "prefer PEG parsers"}
    listed = c.get("/api/projects/default/memories").json()["memories"]
    assert any(m["id"] == mid and m["content"] == "prefer PEG parsers" for m in listed)

    # delete removes the row
    assert c.delete(f"/api/projects/default/memories/{mid}").json()["ok"] is True
    after = c.get("/api/projects/default/memories").json()
    assert after["total"] == 2 and all(m["id"] != mid for m in after["memories"])


def test_project_memories_404s_and_validation(tmp_path):
    c = _client(tmp_path)
    # unknown project on every verb
    assert c.get("/api/projects/ghost/memories").status_code == 404
    assert c.patch("/api/projects/ghost/memories/1",
                   json={"content": "x"}).status_code == 404
    assert c.delete("/api/projects/ghost/memories/1").status_code == 404
    # known project without a memory database yet: empty listing, never an error
    assert c.get("/api/projects/default/memories").json() == {
        "project": "default", "total": 0, "memories": []}
    # known project, unknown memory id
    store = _mem_store(c)
    store.remember("longterm", "seed")
    store.close()
    r = c.patch("/api/projects/default/memories/999", json={"content": "x"})
    assert r.status_code == 404 and "memory" in r.json()["error"]
    assert c.delete("/api/projects/default/memories/999").status_code == 404
    # blank content is rejected before touching the store
    r = c.patch("/api/projects/default/memories/1", json={"content": "   "})
    assert r.status_code == 400 and "content" in r.json()["error"]


# ---- QoL wave: workspace GC endpoints ----

def test_gc_report_and_cleanup_endpoints(tmp_path):
    from test_gc_rollback import _make_task
    c = _client(tmp_path)
    settings = c.app.state.settings
    slug = projects.create_project(settings, "Gcweb")["slug"]
    repo = projects.project_checkout(settings, slug)
    _make_task(settings, slug, repo, "t-old")

    assert c.get("/api/projects/ghost/gc").status_code == 404
    assert c.post("/api/projects/ghost/gc", json={}).status_code == 404

    # the default retention (gc_keep_days=14) keeps a fresh terminal run
    body = c.get(f"/api/projects/{slug}/gc").json()
    assert body["keep_days"] == 14
    assert body["worktrees"] == [] and body["branches"] == []
    # keep_days=0 lists the worktree (branch kept: subtask never accepted)
    body = c.get(f"/api/projects/{slug}/gc", params={"keep_days": 0}).json()
    assert [w["task_id"] for w in body["worktrees"]] == ["t-old"]
    assert body["branches"] == []

    res = c.post(f"/api/projects/{slug}/gc", json={"keep_days": 0}).json()
    assert [w["task_id"] for w in res["removed"]["worktrees"]] == ["t-old"]
    assert res["skipped"] == []
    assert not (settings.workspace_dir / slug / "worktrees" / "t-old").exists()
    # idempotent: a second pass (even asking for the id) removes nothing
    res2 = c.post(f"/api/projects/{slug}/gc",
                  json={"keep_days": 0, "ids": ["t-old"]}).json()
    assert res2["removed"] == {"worktrees": [], "branches": []}
    assert res2["skipped"] and res2["skipped"][0]["task_id"] == "t-old"


def test_gc_keep_days_setting_is_the_default(tmp_path):
    from test_gc_rollback import _make_task
    c = _client(tmp_path)
    settings = c.app.state.settings
    slug = projects.create_project(settings, "Gcs")["slug"]
    repo = projects.project_checkout(settings, slug)
    _make_task(settings, slug, repo, "t-x")
    # a console edit applies live: GET without a param uses the setting
    assert c.patch("/api/settings", json={"gc_keep_days": 0}).status_code == 200
    body = c.get(f"/api/projects/{slug}/gc").json()
    assert body["keep_days"] == 0
    assert [w["task_id"] for w in body["worktrees"]] == ["t-x"]


# ---- QoL wave: accept records the sha; rollback endpoint ----

def test_accept_and_rollback_roundtrip_real_repo(tmp_path):
    from test_gc_rollback import _make_task
    c = _client(tmp_path)
    settings = c.app.state.settings
    slug = projects.create_project(settings, "Rbw")["slug"]
    repo = projects.project_checkout(settings, slug)
    _make_task(settings, slug, repo, "t1", filename="feat.txt", content="feature\n")

    # rollback before any accept: nothing recorded -> 409
    r = c.post("/api/runs/t1/subtasks/s1/rollback")
    assert r.status_code == 409 and "no recorded accept commit" in r.json()["error"]

    r = c.post("/api/tasks/t1/subtasks/s1/accept")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged"] is True and body["decision"] == "accepted"
    review = c.app.state.runs.get_subtask_reviews("t1")["s1"]
    assert review["decision"] == "accepted"
    assert review["accepted_commit"] == body["commit"]  # sha recorded for rollback
    assert (repo / "feat.txt").exists()

    r = c.post("/api/runs/t1/subtasks/s1/rollback")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["reverted"] and body["rollback_commit"]
    assert not (repo / "feat.txt").exists()
    review = c.app.state.runs.get_subtask_reviews("t1")["s1"]
    assert review["decision"] == "rolled_back"
    assert review["rollback_commit"] == body["rollback_commit"]

    # a second rollback has nothing accepted left to revert
    r = c.post("/api/runs/t1/subtasks/s1/rollback")
    assert r.status_code == 409 and "not in an accepted state" in r.json()["error"]
    # unknown task
    assert c.post("/api/runs/ghost/subtasks/s1/rollback").status_code == 404


def test_rollback_endpoint_conflict_is_409(tmp_path):
    from test_gc_rollback import _make_task, git
    c = _client(tmp_path)
    settings = c.app.state.settings
    slug = projects.create_project(settings, "Rbc")["slug"]
    repo = projects.project_checkout(settings, slug)
    (repo / "file.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    _make_task(settings, slug, repo, "t1", filename="file.txt", content="two\n")
    assert c.post("/api/tasks/t1/subtasks/s1/accept").status_code == 200
    # a later commit rewrites the same content -> the revert conflicts
    (repo / "file.txt").write_text("three\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "later change")

    r = c.post("/api/runs/t1/subtasks/s1/rollback")
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body.get("conflict") is True and body["error"]
    # not forced: the accept decision survives and the repo is left clean
    assert c.app.state.runs.get_subtask_reviews("t1")["s1"]["decision"] == "accepted"
    assert git(repo, "status", "--porcelain") == ""


# ---- QoL wave: cron schedules over the API ----

def test_schedule_create_with_cron_and_validation(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/schedules", json={"prompt": "nightly audit", "cron": "0 3 * * *"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["cron"] == "0 3 * * *" and row["every_hours"] is None
    assert row["next_run_at"] is not None
    # interval creation still works exactly as before
    r = c.post("/api/schedules", json={"prompt": "hourly", "every_hours": 1})
    assert r.status_code == 200 and r.json()["cron"] is None
    # mutually exclusive with every_hours
    r = c.post("/api/schedules",
               json={"prompt": "x", "cron": "0 3 * * *", "every_hours": 4})
    assert r.status_code == 400 and "not both" in r.json()["error"]
    # neither recurrence given
    r = c.post("/api/schedules", json={"prompt": "x"})
    assert r.status_code == 400
    # invalid expressions surface the validator's message
    r = c.post("/api/schedules", json={"prompt": "x", "cron": "61 * * * *"})
    assert r.status_code == 400 and "out of range" in r.json()["error"]
    r = c.post("/api/schedules", json={"prompt": "x", "cron": "* *"})
    assert r.status_code == 400 and "5 fields" in r.json()["error"]


def test_schedule_patch_cron(tmp_path):
    c = _client(tmp_path)
    sid = c.post("/api/schedules",
                 json={"prompt": "x", "every_hours": 24}).json()["id"]
    patched = c.patch(f"/api/schedules/{sid}", json={"cron": "*/15 * * * *"}).json()
    assert patched["cron"] == "*/15 * * * *"
    assert patched["next_run_at"] is not None
    r = c.patch(f"/api/schedules/{sid}", json={"cron": "bogus"})
    assert r.status_code == 400 and "cron" in r.json()["error"]


# ---- QoL wave: GitHub PR-comment follow-ups ----

def _qpayload(entry):
    payload = entry["payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


def _followup_client(tmp_path, monkeypatch, *, enable=True, post_status=None):
    monkeypatch.setenv("ADA_GITHUB_TOKEN", "gh-test-token")
    c = _client(tmp_path)
    c.app.state.paused = True  # keep the pump from ever launching a real engine
    patch = {"github_repos": "acme/widget=default"}
    if enable:
        patch["github_pr_followups"] = True
    assert c.patch("/api/settings", json=patch).status_code == 200
    calls = []
    posts = []  # (url, body) of every POSTed comment (follow-up result reports)
    post_status = post_status if post_status is not None else {"code": 201}

    comments = [{"id": 11, "user": {"login": "reviewer"},
                 "body": "please rename the helper",
                 "created_at": "2026-07-20T10:00:00Z"}]

    def transport(method, url, headers, body):
        calls.append((method, url, body))
        assert "gh-test-token" in headers.get("Authorization", "")
        if method == "POST" and url.endswith("/comments"):
            posts.append((url, body))
            return post_status["code"], {"id": 99}
        if "/repos/acme/widget/issues?" in url:
            return 200, []
        if url.endswith("/user"):
            return 200, {"login": "ada-bot"}
        if "/repos/acme/widget/pulls?" in url:
            return 200, [
                {"number": 5, "title": "feat: caching",
                 "head": {"ref": "ada/task-known"}, "body": ""},
                {"number": 6, "title": "chore: cleanup",
                 "head": {"ref": "ada/task-unknown"}, "body": ""},
                {"number": 7, "title": "human PR",
                 "head": {"ref": "feature/human"}, "body": ""},
            ]
        if "/issues/5/comments" in url or "/issues/6/comments" in url:
            return 200, comments
        if "/pulls/5/comments" in url or "/pulls/6/comments" in url:
            return 200, []
        if "/issues/7/comments" in url or "/pulls/7/comments" in url:
            raise AssertionError("comments fetched for a PR that is not our own")
        return 200, []

    c.app.state.github_transport = transport
    return c, calls, posts


def _followup_task_ids(c):
    """Pending follow-up queue task_ids keyed by the PR number in their prompt."""
    out = {}
    for entry in c.app.state.runs.queue_pending():
        prompt = _qpayload(entry)["prompt"]
        for n in (5, 6):
            if f"PR #{n} " in prompt:
                out[n] = entry["task_id"]
    return out


def test_github_pr_followups_reengage_fresh_and_dedupe(tmp_path, monkeypatch):
    c, _calls, _posts = _followup_client(tmp_path, monkeypatch)
    # the branch ada/task-known maps to a known finished run on 'default'
    st = c.app.state.runs
    st.start("task-known", "original work", title="Original", project="default")
    st.set_status("task-known", "completed")

    asyncio.run(c.app.state.github_tick())
    pending = c.app.state.runs.queue_pending()
    assert len(pending) == 2
    payloads = sorted((_qpayload(p) for p in pending),
                      key=lambda p: p["prompt"])
    reengage = next(p for p in payloads if "PR #5" in p["prompt"])
    fresh = next(p for p in payloads if "PR #6" in p["prompt"])
    # known branch -> re-engagement of the original task (continue_from lineage)
    assert reengage["continue_from"] == "task-known"
    assert reengage["project"] == "default"
    assert reengage["title"] == "Follow-up: feat: caching"
    assert reengage["prompt"].startswith('Address reviewer feedback on PR #5')
    assert "please rename the helper" in reengage["prompt"]
    assert "ada/task-known" in reengage["prompt"]
    # unknown branch -> a fresh run on the repo's mapped project
    assert fresh["continue_from"] is None and fresh["project"] == "default"
    # per-PR last-seen timestamps persisted (existing state shape, extended)
    state = json.loads((tmp_path / "data" / "github_seen.json").read_text())
    assert state["pr_seen"] == {"acme/widget#5": "2026-07-20T10:00:00Z",
                                "acme/widget#6": "2026-07-20T10:00:00Z"}
    assert state["seen"] == [] and state["tracked"] == {}

    # second tick: the same comments are older than the cursor -> no new runs
    asyncio.run(c.app.state.github_tick())
    assert len(c.app.state.runs.queue_pending()) == 2


def test_github_pr_followups_disabled_by_default(tmp_path, monkeypatch):
    c, calls, _posts = _followup_client(tmp_path, monkeypatch, enable=False)
    asyncio.run(c.app.state.github_tick())
    assert c.app.state.runs.queue_pending() == []
    assert not any("/pulls?" in u for (_m, u, _b) in calls)  # PRs never listed


def test_github_pr_followup_enqueue_records_pr_pending(tmp_path, monkeypatch):
    c, _calls, posts = _followup_client(tmp_path, monkeypatch)
    asyncio.run(c.app.state.github_tick())
    ids = _followup_task_ids(c)
    state = json.loads((tmp_path / "data" / "github_seen.json").read_text())
    assert state["pr_pending"] == {
        ids[5]: {"repo": "acme/widget", "pr": 5},
        ids[6]: {"repo": "acme/widget", "pr": 6},
    }
    assert posts == []  # both runs still queued — nothing to report yet


def test_github_pr_followup_completed_posts_result_comment_once(tmp_path, monkeypatch):
    c, _calls, posts = _followup_client(tmp_path, monkeypatch)
    asyncio.run(c.app.state.github_tick())
    ids = _followup_task_ids(c)
    tid = ids[5]
    st = c.app.state.runs
    st.start(tid, "follow-up work", title="Follow-up", project="default")
    st.checkpoint_subtask(tid, "s1", status="passed", attempts=1, result="ok",
                          verdict_json=json.dumps({"passed": True, "score": 91}))
    st.finish(tid, status="completed", cost_usd=1.5)

    asyncio.run(c.app.state.github_tick())
    assert len(posts) == 1
    url, body = posts[0]
    assert url == "https://api.github.com/repos/acme/widget/issues/5/comments"
    text = body["body"]
    assert text.startswith("Addressed reviewer feedback — branch updated.")
    assert "| s1 | passed | 91 |" in text            # verdict digest
    assert "**Cost:** $1.50" in text
    assert "updated in place" in text                # footer
    state = json.loads((tmp_path / "data" / "github_seen.json").read_text())
    assert tid not in state["pr_pending"]            # cleared on success
    assert state["pr_pending"] == {ids[6]: {"repo": "acme/widget", "pr": 6}}
    # pr_seen bumped past the batch so our own comment can never re-trigger
    assert state["pr_seen"]["acme/widget#5"] > "2026-07-20T10:00:00Z"

    asyncio.run(c.app.state.github_tick())
    assert len(posts) == 1                           # exactly once
    assert len(c.app.state.runs.queue_pending()) == 2  # no self-triggered run


def test_github_pr_followup_result_post_retries_then_caps(tmp_path, monkeypatch):
    box = {"code": 500}
    c, _calls, posts = _followup_client(tmp_path, monkeypatch, post_status=box)
    asyncio.run(c.app.state.github_tick())
    tid = _followup_task_ids(c)[5]
    c.app.state.runs.set_status(tid, "completed")
    state_path = tmp_path / "data" / "github_seen.json"

    asyncio.run(c.app.state.github_tick())           # attempt 1 fails
    state = json.loads(state_path.read_text())
    assert state["pr_pending"][tid] == {"repo": "acme/widget", "pr": 5,
                                        "attempts": 1}
    asyncio.run(c.app.state.github_tick())           # attempt 2 fails
    assert json.loads(state_path.read_text())["pr_pending"][tid]["attempts"] == 2
    asyncio.run(c.app.state.github_tick())           # attempt 3 fails — give up
    state = json.loads(state_path.read_text())
    assert tid not in state["pr_pending"]
    assert len(posts) == 3
    # never posted -> the last-seen cursor was never bumped
    assert state["pr_seen"]["acme/widget#5"] == "2026-07-20T10:00:00Z"

    asyncio.run(c.app.state.github_tick())           # dropped: no more attempts
    assert len(posts) == 3


def test_github_pr_followup_failed_run_posts_honest_variant(tmp_path, monkeypatch):
    c, _calls, posts = _followup_client(tmp_path, monkeypatch)
    asyncio.run(c.app.state.github_tick())
    tid = _followup_task_ids(c)[6]
    c.app.state.runs.set_status(tid, "failed")
    asyncio.run(c.app.state.github_tick())
    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/repos/acme/widget/issues/6/comments")
    assert f"(task `{tid}`)" in body["body"]
    assert "did not complete cleanly (status: failed)" in body["body"]
    assert "Addressed reviewer feedback" not in body["body"]


def test_github_pr_followup_result_pass_gated_off(tmp_path, monkeypatch):
    c, _calls, posts = _followup_client(tmp_path, monkeypatch, enable=False)
    st = c.app.state.runs
    st.start("t-old", "x", title="T", project="default")
    st.set_status("t-old", "completed")
    state_path = tmp_path / "data" / "github_seen.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    pending = {"t-old": {"repo": "acme/widget", "pr": 5}}
    state_path.write_text(json.dumps({
        "seen": [], "tracked": {},
        "pr_seen": {"acme/widget#5": "2026-07-20T10:00:00Z"},
        "pr_pending": pending,
    }))
    asyncio.run(c.app.state.github_tick())
    assert posts == []  # gated: no result comment, zero behavior change
    assert json.loads(state_path.read_text())["pr_pending"] == pending


def test_github_pr_followup_pending_dropped_when_run_deleted(tmp_path, monkeypatch):
    c, _calls, posts = _followup_client(tmp_path, monkeypatch)
    state_path = tmp_path / "data" / "github_seen.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "seen": [], "tracked": {},
        "pr_seen": {"acme/widget#5": "2026-07-20T10:00:00Z",
                    "acme/widget#6": "2026-07-20T10:00:00Z"},
        "pr_pending": {"ghost": {"repo": "acme/widget", "pr": 5}},
    }))
    asyncio.run(c.app.state.github_tick())
    assert posts == []  # no run row left — nothing to report
    assert json.loads(state_path.read_text())["pr_pending"] == {}


# ---- QoL wave: Slack/email notify settings reach the dispatch config ----

def test_notify_config_threads_slack_and_email_settings(tmp_path, monkeypatch):
    from ai_dev_assistant import notify
    from ai_dev_assistant.orchestration.events import Event
    c = _client(tmp_path)
    assert c.patch("/api/settings", json={
        "notify_slack_webhook": "https://hooks.slack.com/services/T/B/x",
        "notify_email_to": "dev@example.com",
        "notify_smtp_host": "smtp.example.com",
        "notify_smtp_port": 2525,
        "notify_smtp_user": "ada@example.com",
        "notify_smtp_starttls": False,
    }).status_code == 200
    seen = []
    monkeypatch.setattr(notify, "notify_event",
                        lambda cfg, **kw: seen.append(cfg) or True)
    c.app.state.notify_dispatch(Event("done", "Run ended.", {}), "t-1", "default")
    assert len(seen) == 1  # slack/email alone count as configured channels
    cfg = seen[0]
    assert cfg.slack_webhook_url == "https://hooks.slack.com/services/T/B/x"
    assert cfg.email_to == "dev@example.com" and cfg.smtp_host == "smtp.example.com"
    assert cfg.smtp_port == 2525 and cfg.smtp_user == "ada@example.com"
    assert cfg.smtp_starttls is False
    assert cfg.webhook_url == "" and cfg.desktop is False


# =====================================================================
# Away wave: knowledge graph v2, workspaces, custom agents, /api/home
# =====================================================================

# ---- Knowledge graph v2 (/api/projects/{slug}/graph2*) ----

def _seed_kg(c, slug="default"):
    """Write a small layered/weighted KG into the project's graph file (the same
    path the server opens read-only), exactly like the engine would."""
    from ai_dev_assistant.knowledge.graph import NetworkXKnowledgeGraph
    s = dataclasses.replace(c.app.state.settings, project=slug)
    kg = NetworkXKnowledgeGraph(s.graph_path)
    kg.add_fact("auth.py", "implements", "login flow", source="coder")
    kg.add_fact("auth.py", "implements", "login flow", source="reviewer")  # weight -> 2
    kg.add_fact("auth.py", "tested_by", "test_auth.py")
    kg.add_fact("s1", "assigned_to", "coder", layer="run")
    kg.save()
    return kg


def test_graph2_export_view_layers_and_weights_roundtrip(tmp_path):
    c = _client(tmp_path)
    _seed_kg(c)
    body = c.get("/api/projects/default/graph2").json()
    assert body["project"] == "default"
    ids = {n["id"] for n in body["nodes"]}
    assert {"auth.py", "login-flow", "test-auth.py", "s1", "coder"} <= ids
    edges = {(e["src"], e["dst"], e["relation"]): e for e in body["edges"]}
    imp = edges[("auth.py", "login-flow", "implements")]
    assert imp["weight"] == 2 and imp["layer"] == "domain"
    assert edges[("s1", "coder", "assigned_to")]["layer"] == "run"
    stats = body["stats"]
    assert stats["nodes"] == len(body["nodes"]) and stats["edges"] == 3
    assert stats["by_layer"] == {"domain": 2, "run": 1}

    # ?layer= filters edges; ?min_weight= keeps only the repeat-asserted fact
    domain = c.get("/api/projects/default/graph2?layer=domain").json()
    assert {e["relation"] for e in domain["edges"]} == {"implements", "tested_by"}
    heavy = c.get("/api/projects/default/graph2?min_weight=2").json()
    assert [e["relation"] for e in heavy["edges"]] == ["implements"]
    # ?limit= caps nodes (ranked by weighted degree, top node survives)
    capped = c.get("/api/projects/default/graph2?limit=1").json()
    assert [n["id"] for n in capped["nodes"]] == ["auth.py"]


def test_graph2_node_neighborhood_with_path_id(tmp_path):
    c = _client(tmp_path)
    _seed_kg(c)
    body = c.get("/api/projects/default/graph2/node/auth.py").json()
    assert body["node"] == "auth.py"
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {"auth.py", "login-flow", "test-auth.py"}
    # layer filter drops non-matching edges from traversal + result
    run_only = c.get("/api/projects/default/graph2/node/s1?layer=run").json()
    assert {n["id"] for n in run_only["nodes"]} == {"s1", "coder"}
    # unknown node -> empty, not an error
    missing = c.get("/api/projects/default/graph2/node/nope").json()
    assert missing["nodes"] == [] and missing["edges"] == []


def test_graph2_search(tmp_path):
    c = _client(tmp_path)
    _seed_kg(c)
    hits = c.get("/api/projects/default/graph2/search?q=auth").json()
    assert {n["id"] for n in hits["nodes"]} == {"auth.py", "test-auth.py"}
    assert c.get("/api/projects/default/graph2/search?q=").json()["nodes"] == []


def test_graph2_unknown_project_is_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/projects/nope/graph2").status_code == 404
    assert c.get("/api/projects/nope/graph2/search?q=x").status_code == 404
    assert c.get("/api/projects/nope/graph2/node/x").status_code == 404


def test_legacy_graph_endpoint_shape_survives_kg_rework(tmp_path):
    """/api/graph (the shape today's Knowledge tab renders) still returns
    id/type nodes and source/target/relation edges over the reworked KG."""
    c = _client(tmp_path)
    _seed_kg(c)
    body = c.get("/api/graph?project=default").json()
    assert body["project"] == "default"
    assert {"id", "type"} <= set(body["nodes"][0])
    assert {"source", "target", "relation"} <= set(body["edges"][0])
    assert any(e["relation"] == "implements" for e in body["edges"])


# ---- Workspaces ----

def test_workspace_crud_roundtrip(tmp_path):
    c = _client(tmp_path)
    a = c.post("/api/projects", json={"name": "Ws A"}).json()["slug"]
    r = c.post("/api/workspaces", json={"name": "Platform", "description": "core",
                                        "projects": [a]})
    assert r.status_code == 200, r.text
    ws = r.json()
    assert ws["slug"] == "platform" and ws["project_slugs"] == [a]
    assert [w["slug"] for w in c.get("/api/workspaces").json()] == ["platform"]
    patched = c.patch("/api/workspaces/platform",
                      json={"name": "Platform 2", "description": "renamed"}).json()
    assert patched["name"] == "Platform 2" and patched["description"] == "renamed"
    assert c.patch("/api/workspaces/nope", json={"name": "x"}).status_code == 404
    assert c.delete("/api/workspaces/platform").json()["ok"] is True
    assert c.get("/api/workspaces").json() == []
    # member project survives the workspace deletion
    assert a in [p["slug"] for p in c.get("/api/projects").json()]


def test_workspace_create_unknown_project_is_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/workspaces", json={"name": "Bad", "projects": ["nope"]})
    assert r.status_code == 400 and "unknown project" in r.json()["error"]
    assert c.get("/api/workspaces").json() == []  # nothing was written


def test_workspace_assign_moves_and_projects_expose_workspace(tmp_path):
    c = _client(tmp_path)
    a = c.post("/api/projects", json={"name": "Mv A"}).json()["slug"]
    c.post("/api/workspaces", json={"name": "One", "projects": [a]})
    c.post("/api/workspaces", json={"name": "Two"})
    # /api/projects grows an additive workspace field
    entry = next(p for p in c.get("/api/projects").json() if p["slug"] == a)
    assert entry["workspace"] == "one"
    # assigning elsewhere MOVES the project
    r = c.post("/api/workspaces/two/projects", json={"project": a})
    assert r.status_code == 200 and r.json()["project_slugs"] == [a]
    by_slug = {w["slug"]: w for w in c.get("/api/workspaces").json()}
    assert by_slug["one"]["project_slugs"] == []
    entry = next(p for p in c.get("/api/projects").json() if p["slug"] == a)
    assert entry["workspace"] == "two"
    # unassign -> ungrouped (null workspace)
    assert c.delete(f"/api/workspaces/two/projects/{a}").status_code == 200
    entry = next(p for p in c.get("/api/projects").json() if p["slug"] == a)
    assert entry["workspace"] is None
    # unknown workspace / unknown project statuses
    assert c.post("/api/workspaces/nope/projects", json={"project": a}).status_code == 404
    assert c.post("/api/workspaces/two/projects",
                  json={"project": "ghost"}).status_code == 400


def test_workspace_deps_put_validates(tmp_path):
    c = _client(tmp_path)
    a = c.post("/api/projects", json={"name": "Dp A"}).json()["slug"]
    b = c.post("/api/projects", json={"name": "Dp B"}).json()["slug"]
    c.post("/api/workspaces", json={"name": "Deps", "projects": [a, b]})
    ok = c.put("/api/workspaces/deps/deps", json={"deps": {b: [a]}})
    assert ok.status_code == 200 and ok.json()["default_deps"] == {b: [a]}
    cyc = c.put("/api/workspaces/deps/deps", json={"deps": {a: [b], b: [a]}})
    assert cyc.status_code == 400 and "cycle" in cyc.json()["error"]
    bad = c.put("/api/workspaces/deps/deps", json={"deps": {a: ["ghost"]}})
    assert bad.status_code == 400
    assert c.put("/api/workspaces/nope/deps", json={"deps": {}}).status_code == 404
    # failed PUTs never clobbered the stored map
    assert c.get("/api/workspaces").json()[0]["default_deps"] == {b: [a]}


def test_workspace_run_enqueues_exactly_like_manual_multi_project_run(tmp_path):
    """POST /api/workspaces/{ws}/run must be indistinguishable from a manual
    POST /api/run with projects+project_deps, save the additive 'workspace' key."""
    c = _client(tmp_path)
    c.app.state.paused = True  # keep the pump from launching anything
    a = c.post("/api/projects", json={"name": "Wr A"}).json()["slug"]
    b = c.post("/api/projects", json={"name": "Wr B"}).json()["slug"]
    c.post("/api/workspaces", json={"name": "Fleet", "projects": [a, b]})
    assert c.put("/api/workspaces/fleet/deps", json={"deps": {b: [a]}}).status_code == 200

    r = c.post("/api/workspaces/fleet/run",
               json={"prompt": "upgrade all", "title": "Fleet run", "effort": "low"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    ws_payload = c.app.state.runs.queue_pending()[0]["payload"]
    if isinstance(ws_payload, str):
        ws_payload = json.loads(ws_payload)

    m = c.post("/api/run", json={"prompt": "upgrade all", "title": "Fleet run",
                                 "effort": "low", "projects": [a, b],
                                 "project_deps": {b: [a]}})
    assert m.status_code == 200
    manual_payload = c.app.state.runs.queue_pending()[1]["payload"]
    if isinstance(manual_payload, str):
        manual_payload = json.loads(manual_payload)

    assert ws_payload.pop("workspace") == "fleet"  # additive attribution only
    assert ws_payload == manual_payload
    assert manual_payload["projects"] == [a, b]
    assert manual_payload["project_deps"] == {b: [a]}


def test_workspace_run_subset_and_single_member_collapse(tmp_path):
    c = _client(tmp_path)
    c.app.state.paused = True
    a = c.post("/api/projects", json={"name": "Sub A"}).json()["slug"]
    b = c.post("/api/projects", json={"name": "Sub B"}).json()["slug"]
    c.post("/api/workspaces", json={"name": "Subset", "projects": [a, b]})
    # subset of one member behaves exactly like a single-project run
    r = c.post("/api/workspaces/subset/run", json={"prompt": "just a", "subset": [a]})
    assert r.status_code == 200
    payload = c.app.state.runs.queue_pending()[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["project"] == a and not payload.get("projects")
    assert payload["workspace"] == "subset"
    # non-member subset is a 400; unknown workspace a 404; empty workspace a 400
    assert c.post("/api/workspaces/subset/run",
                  json={"prompt": "x", "subset": ["ghost"]}).status_code == 400
    assert c.post("/api/workspaces/nope/run", json={"prompt": "x"}).status_code == 404
    c.post("/api/workspaces", json={"name": "Hollow"})
    r = c.post("/api/workspaces/hollow/run", json={"prompt": "x"})
    assert r.status_code == 400 and "no member projects" in r.json()["error"]


# ---- Custom agents ----

_VALID_AGENT = {
    "name": "sql_tuner", "description": "Tunes slow SQL.",
    "when_to_use": "Use for slow queries.",
    "system_prompt": "You are a SQL tuning agent.",
    "tools": ["read_file", "grep"],
}


def test_custom_agent_crud_and_roster_pickup(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/agents", json={"spec": _VALID_AGENT})
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["name"] == "sql_tuner"
    body = c.get("/api/agents").json()
    assert [a["name"] for a in body["custom"]] == ["sql_tuner"]
    assert body["custom"][0]["system_prompt"] == "You are a SQL tuning agent."
    assert not any(a["name"] == "sql_tuner" for a in body["builtin"])
    # new runs pick customs up automatically: build_agents composes them per engine
    from ai_dev_assistant.agents.registry import build_agents
    assert "sql_tuner" in build_agents(c.app.state.settings)
    # upsert by name is an update, not a collision
    assert c.post("/api/agents", json={"spec": {**_VALID_AGENT,
                                                "description": "v2"}}).status_code == 200
    assert c.get("/api/agents").json()["custom"][0]["description"] == "v2"
    assert c.delete("/api/agents/sql_tuner").json()["ok"] is True
    assert c.get("/api/agents").json()["custom"] == []
    assert c.delete("/api/agents/sql_tuner").status_code == 404


def test_custom_agent_validation_and_builtin_guard(tmp_path):
    c = _client(tmp_path)
    bad_tool = c.post("/api/agents", json={"spec": {**_VALID_AGENT,
                                                    "tools": ["not_a_tool"]}})
    assert bad_tool.status_code == 400 and "not_a_tool" in bad_tool.json()["error"]
    collision = c.post("/api/agents", json={"spec": {**_VALID_AGENT, "name": "coder"}})
    assert collision.status_code == 400 and "coder" in collision.json()["error"]
    bad_name = c.post("/api/agents", json={"spec": {**_VALID_AGENT, "name": "Bad Name!"}})
    assert bad_name.status_code == 400
    # deleting a builtin is a 400 (not a 404)
    r = c.delete("/api/agents/coder")
    assert r.status_code == 400 and "built-in" in r.json()["error"]
    assert c.get("/api/agents").json()["custom"] == []  # nothing slipped through


# ---- /api/home ----

def test_home_empty_everything(tmp_path):
    c = _client(tmp_path)
    body = c.get("/api/home").json()
    assert body["attention"] == [] and body["running"] == [] and body["queued"] == []
    assert body["recent"] == [] and body["workspaces"] == []
    assert body["benchmarks"] == {"latest": None, "delta": None, "series": []}
    assert body["spend"]["total_usd"] == 0.0
    assert body["counts"] == {"projects": 1, "workspaces": 0, "custom_agents": 0}
    assert body["errors"] == []


def test_home_aggregates_attention_running_queued_recent_benchmarks(tmp_path):
    from ai_dev_assistant.evals.history import history_path
    from ai_dev_assistant.orchestration.events import Event
    from ai_dev_assistant.orchestration.run_control import RunControl
    from ai_dev_assistant.web.server import Broker

    c = _client(tmp_path)
    st = c.app.state.runs

    # recent: a terminal run
    st.start("run-done", "ship it", title="Shipped", project="default")
    st.finish("run-done", status="completed", quality_score=88, cost_usd=1.25)
    # running: a live run with an open ask request on its control/broker
    st.start("run-live", "big task", title="Live", project="default")
    c.app.state.running.add("run-live")
    ctrl = RunControl()
    loop = asyncio.new_event_loop()
    try:
        ctrl._pending["ask-1"] = loop.create_future()
        c.app.state.controls["run-live"] = ctrl
        broker = Broker()
        broker.publish(Event("ask", "[coder] Which DB?", {
            "id": "ask-1", "agent": "coder", "question": "Which DB?",
            "options": ["sqlite", "postgres"]}))
        c.app.state.brokers["run-live"] = broker
        # queued: a pending queue entry
        st.enqueue("run-wait", "later", "Waiting",
                   {"prompt": "later", "project": "default"})
        # benchmarks: one recorded history entry
        history_path(c.app.state.settings).write_text(json.dumps(
            {"suite": "replay", "git_sha": "abc123", "ts": "2026-07-26T00:00:00Z",
             "pass_rate": 1.0, "quality_mean": 90.0, "quality_min": 80.0,
             "cost_usd": 0.0}) + "\n")
        # workspaces + counts
        c.post("/api/workspaces", json={"name": "Home Ws"})

        body = c.get("/api/home").json()
    finally:
        c.app.state.controls.pop("run-live", None)
        loop.close()

    assert body["errors"] == []
    assert body["attention"] == [{
        "task_id": "run-live", "project": "default", "kind": "ask", "id": "ask-1",
        "agent": "coder", "options": ["sqlite", "postgres"], "question": "Which DB?"}]
    assert [r["task_id"] for r in body["running"]] == ["run-live"]
    assert [q["task_id"] for q in body["queued"]] == ["run-wait"]
    assert body["queued"][0]["position"] == 1
    recent = body["recent"]
    assert [r["task_id"] for r in recent] == ["run-done"]
    assert recent[0] == {"task_id": "run-done", "title": "Shipped",
                         "project": "default", "status": "completed", "quality": 88,
                         "cost_usd": 1.25, "ended_at": recent[0]["ended_at"]}
    assert recent[0]["ended_at"] is not None
    assert body["benchmarks"]["latest"]["git_sha"] == "abc123"
    assert body["benchmarks"]["series"][0]["sha"] == "abc123"
    assert body["workspaces"] == [{"slug": "home-ws", "name": "Home Ws", "projects": 0}]
    assert body["counts"]["workspaces"] == 1


def test_home_answered_attention_requests_disappear(tmp_path):
    from ai_dev_assistant.orchestration.events import Event
    from ai_dev_assistant.orchestration.run_control import RunControl
    from ai_dev_assistant.web.server import Broker

    c = _client(tmp_path)
    c.app.state.runs.start("run-ans", "t", project="default")
    c.app.state.running.add("run-ans")
    ctrl = RunControl()
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        ctrl._pending["permission-1"] = fut
        c.app.state.controls["run-ans"] = ctrl
        broker = Broker()
        broker.publish(Event("permission", "[coder] rm -rf?", {
            "id": "permission-1", "agent": "coder", "request": "delete build dir"}))
        c.app.state.brokers["run-ans"] = broker
        body = c.get("/api/home").json()
        assert body["attention"][0]["kind"] == "permission"
        assert body["attention"][0]["request"] == "delete build dir"
        fut.set_result("ALLOW ONCE")  # answered -> no longer needs attention
        assert c.get("/api/home").json()["attention"] == []
    finally:
        c.app.state.controls.pop("run-ans", None)
        loop.close()
