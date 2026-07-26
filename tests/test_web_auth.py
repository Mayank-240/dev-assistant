"""S8/W4/W5/W6 web-surface tests: bearer-token auth (incl. the project endpoints),
the diff endpoint, broker-evicted event replay, and import-time laziness (no LLM needed)."""

from __future__ import annotations

import json
import subprocess

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.web.server import _settings_for, create_app

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from starlette.websockets import WebSocketDisconnect  # noqa: E402

TOKEN = "test-token-123"


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )


def _client(tmp_path, **kw) -> TestClient:
    return TestClient(create_app(_settings(tmp_path), **kw))


# ---- S8: bearer-token auth ----

def test_api_requires_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    assert c.get("/api/tasks").status_code == 401
    assert c.get("/api/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/tasks", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    # query-param fallback (used by <a download> links, which can't send headers)
    assert c.get(f"/api/tasks?token={TOKEN}").status_code == 200


def test_health_ready_index_stay_open_with_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 200
    assert c.get("/").status_code == 200  # SPA shell (static) is exempt


def test_ws_rejects_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/whatever"):
            pass
    # with the token as a query param the socket is accepted normally
    with c.websocket_connect(f"/ws/whatever?token={TOKEN}") as ws:
        assert ws.receive_json()["type"] == "error"  # unknown task, but authenticated


def test_loopback_without_token_stays_open(tmp_path, monkeypatch):
    monkeypatch.delenv("ADA_API_TOKEN", raising=False)
    c = _client(tmp_path)  # default host is loopback -> auth off
    assert c.app.state.api_token == ""
    assert c.get("/api/tasks").status_code == 200
    with c.websocket_connect("/ws/whatever") as ws:
        assert ws.receive_json()["type"] == "error"  # unknown task; no auth required


def test_nonloopback_autogenerates_and_prints_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ADA_API_TOKEN", raising=False)
    app = create_app(_settings(tmp_path), host="0.0.0.0")
    token = app.state.api_token
    assert token  # auto-generated because the bind host is non-loopback
    assert token in capsys.readouterr().out  # printed at startup (container logs)
    c = TestClient(app)
    assert c.get("/api/stats").status_code == 401
    assert c.get("/api/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# ---- Clean break: per-run repo binding is gone; git_finalize is the only per-run knob ----

def test_settings_for_has_no_repo_kwarg_and_applies_git_finalize():
    base = Settings()
    s = _settings_for(base, None, None, git_finalize=True)
    assert s.git_finalize is True
    s2 = _settings_for(base, None, None)  # absent -> keep the server default
    assert s2.git_finalize == base.git_finalize
    with pytest.raises(TypeError):
        _settings_for(base, None, None, repo={"repo_path": "/some/repo"})


# ---- Project endpoints stay behind the auth middleware ----

def test_project_endpoints_require_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    assert c.post("/api/projects/import", json={"source": "/x"}).status_code == 401
    assert c.get("/api/projects/default/status").status_code == 401
    assert c.get("/api/projects/default/activity").status_code == 401
    assert c.patch("/api/projects/default", json={"archived": True}).status_code == 401
    assert c.delete("/api/projects/default").status_code == 401
    # with the token, the requests reach the handlers (any non-401 outcome)
    assert c.get("/api/projects/default/activity", headers=hdr).status_code == 200
    assert c.delete("/api/projects/default", headers=hdr).status_code == 409


# ---- F4: review/permissions + project-runs endpoints stay behind the auth middleware ----

def test_review_and_permissions_endpoints_require_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    assert c.get("/api/tasks/t1/review").status_code == 401
    assert c.get("/api/tasks/t1/permissions").status_code == 401
    assert c.post("/api/tasks/t1/subtasks/s1/accept").status_code == 401
    assert c.post("/api/tasks/t1/subtasks/s1/reject", json={}).status_code == 401
    assert c.get("/api/projects/default/runs").status_code == 401
    # with the token, requests reach the handlers (any non-401 outcome)
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/api/tasks/t1/permissions", headers=hdr).status_code == 200
    assert c.get("/api/tasks/t1/review", headers=hdr).status_code != 401
    assert c.post("/api/tasks/t1/subtasks/s1/accept", headers=hdr).status_code == 404
    assert c.get("/api/projects/default/runs", headers=hdr).status_code == 200


# ---- F3: cross-project children endpoint stays behind the auth middleware ----

def test_children_endpoint_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    assert c.get("/api/tasks/t1/children").status_code == 401
    # with the token, the request reaches the handler (200 with children, or 501
    # while the fan-out core lands separately — never 401)
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    r = c.get("/api/tasks/t1/children", headers=hdr)
    assert r.status_code in (200, 501)


# ---- W5: diff endpoint ----

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_diff_non_git_workspace_is_clean_empty(tmp_path):
    ws = tmp_path / "ws" / "task-1"
    ws.mkdir(parents=True)
    (ws / "a.txt").write_text("hello\n")
    c = _client(tmp_path, api_token="")
    d = c.get("/api/tasks/task-1/diff").json()
    assert d["is_git"] is False and d["diff"] == "" and d["status"] == ""
    # unknown task behaves the same
    assert c.get("/api/tasks/nope/diff").json()["is_git"] is False


def test_diff_git_workspace_with_change(tmp_path):
    ws = tmp_path / "ws" / "task-2"
    ws.mkdir(parents=True)
    _git(ws, "init")
    (ws / "a.txt").write_text("old line\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "init")
    (ws / "a.txt").write_text("new line\n")
    (ws / "b.txt").write_text("untracked\n")
    c = _client(tmp_path, api_token="")
    d = c.get("/api/tasks/task-2/diff").json()
    assert d["is_git"] is True
    assert "+new line" in d["diff"] and "-old line" in d["diff"]
    assert "b.txt" in d["status"]  # untracked files surface via git status --porcelain
    assert d["truncated"] is False


# ---- W4: event history survives broker eviction ----

def test_ws_replays_events_jsonl_when_broker_gone(tmp_path):
    tid = "old-task"
    docs = tmp_path / "docs" / tid
    docs.mkdir(parents=True)
    events = [
        {"seq": 0, "type": "status", "message": "Backend: anthropic", "data": {}, "ts": 1.0},
        {"seq": 1, "type": "done", "message": "Run ended.", "data": {}, "ts": 2.0},
    ]
    (docs / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    c = _client(tmp_path, api_token="")
    assert tid not in c.app.state.brokers  # no broker in RAM — durable log only
    with c.websocket_connect(f"/ws/{tid}") as ws:
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json()["type"] == "done"


# ---- W6: importing web.server must not build an app / open SQLite ----

def test_module_has_no_import_time_app():
    import ai_dev_assistant.web.server as server
    assert not hasattr(server, "app")


# ---- W5: run comparison ----

def test_compare_returns_both_runs_metrics(tmp_path):
    c = _client(tmp_path, api_token="")
    st = c.app.state.runs
    st.start("run-a", "task a", title="Run A")
    st.finish("run-a", status="completed", subtasks_total=3, subtasks_passed=3,
              tests="passed", cost_usd=1.25, input_tokens=1000, output_tokens=500,
              sessions_spawned=2, sessions_reaped=1, quality_score=88)
    st.start("run-b", "task b", title="Run B")
    st.finish("run-b", status="partial", subtasks_total=4, subtasks_passed=2,
              tests="failed", cost_usd=2.5, input_tokens=2000, output_tokens=900,
              sessions_spawned=4, sessions_reaped=2, quality_score=55)
    d = c.get("/api/runs/compare", params={"a": "run-a", "b": "run-b"}).json()
    a, b = d["a"], d["b"]
    assert a["id"] == "run-a" and b["id"] == "run-b"
    assert a["status"] == "completed" and b["status"] == "partial"
    assert (a["subtasks_passed"], a["subtasks_total"]) == (3, 3)
    assert (b["subtasks_passed"], b["subtasks_total"]) == (2, 4)
    assert a["tests"] == "passed" and b["tests"] == "failed"
    assert a["cost_usd"] == 1.25 and b["cost_usd"] == 2.5
    assert a["input_tokens"] == 1000 and b["output_tokens"] == 900
    assert a["quality_score"] == 88 and b["quality_score"] == 55
    assert a["sessions_spawned"] == 2 and b["sessions_reaped"] == 2
    assert a["duration_s"] is not None and a["duration_s"] >= 0


def test_compare_unknown_run_is_404(tmp_path):
    c = _client(tmp_path, api_token="")
    c.app.state.runs.start("run-a", "task a")
    r = c.get("/api/runs/compare", params={"a": "run-a", "b": "nope"})
    assert r.status_code == 404 and "nope" in r.json()["error"]


# ---- W5: safe config endpoint (feeds the UI's cost-vs-budget meter) ----

def test_config_exposes_budget_and_no_secrets(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="sk-super-secret", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
        budget_usd=7.5,
    )
    c = TestClient(create_app(settings, api_token=""))
    cfg = c.get("/api/config").json()
    assert cfg["budget_usd"] == 7.5
    assert set(cfg) == {"budget_usd", "llm_backend", "max_concurrent_runs", "sdk_model"}
    assert "sk-super-secret" not in json.dumps(cfg)


# ---- W5: retry/resume endpoint ----

def test_resume_gates_on_run_status(tmp_path):
    c = _client(tmp_path, api_token="")
    st = c.app.state.runs
    assert c.post("/api/tasks/nope/resume").status_code == 404
    st.start("done-run", "p")
    st.set_status("done-run", "completed")
    assert c.post("/api/tasks/done-run/resume").status_code == 409
    st.start("live-run", "p")  # status 'running'
    assert c.post("/api/tasks/live-run/resume").status_code == 409


def test_resume_resumable_status_starts_or_501s(tmp_path):
    """Resume either enqueues the run (Engine.run grew a resume kwarg) or answers
    501 'resume not available yet' — both are valid while R1 lands in parallel."""
    c = _client(tmp_path, api_token="")
    st = c.app.state.runs
    st.start("bad-run", "fix the thing")
    st.set_status("bad-run", "failed")
    c.app.state.paused = True  # keep the pump from actually launching an engine
    r = c.post("/api/tasks/bad-run/resume")
    assert r.status_code in (200, 501)
    if r.status_code == 200:
        assert r.json()["task_id"] == "bad-run"
        assert (st.get("bad-run") or {}).get("status") == "queued"
    else:
        assert "resume not available" in r.json()["error"]
        # nothing was enqueued on the 501 path
        assert st.queue_positions() == {}


# ---- R5: queue pump over-subscription guard + startup rebuild sanity ----

def test_pump_skips_task_already_running(tmp_path):
    app = create_app(_settings(tmp_path), api_token="")
    app.state.concurrency = 2  # a free slot, so the pump actually reaches the stale entry
    app.state.runs.enqueue("dup-task", "p", None, {"prompt": "p"})
    app.state.running.add("dup-task")  # simulate the run already being live
    with TestClient(app):  # startup fires the pump
        assert "dup-task" not in app.state.tasks  # no second copy started
        assert app.state.runs.queue_positions() == {}  # stale entry consumed, not re-run


# ---- Global settings console endpoints stay behind the auth middleware ----

def test_settings_console_endpoints_require_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    assert c.get("/api/settings").status_code == 401
    assert c.patch("/api/settings", json={"trace": False}).status_code == 401
    assert c.delete("/api/settings/trace").status_code == 401
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/api/settings", headers=hdr).status_code == 200
    assert c.patch("/api/settings", json={"trace": False}, headers=hdr).status_code == 200
    assert c.delete("/api/settings/trace", headers=hdr).status_code == 200


# ---- Named multi-user auth: users.json on top of the ADA_API_TOKEN owner ----

OWNER_HDR = {"Authorization": f"Bearer {TOKEN}"}


def _make_user(c, name: str) -> str:
    r = c.post("/api/users", json={"name": name}, headers=OWNER_HDR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == name and body["token"]
    return body["token"]


def test_named_user_token_authorizes_bad_token_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    utok = _make_user(c, "alice")
    assert c.get("/api/tasks", headers={"Authorization": f"Bearer {utok}"}).status_code == 200
    assert c.get(f"/api/tasks?token={utok}").status_code == 200  # query-param path too
    assert c.get("/api/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_auth_status_reports_resolved_user(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    utok = _make_user(c, "alice")
    assert c.get("/api/auth/status", headers=OWNER_HDR).json()["user"] == "owner"
    assert c.get("/api/auth/status",
                 headers={"Authorization": f"Bearer {utok}"}).json()["user"] == "alice"
    anon = c.get("/api/auth/status").json()
    assert anon["authorized"] is False and anon["user"] is None


def test_auth_status_user_is_local_when_auth_off(tmp_path):
    c = _client(tmp_path, api_token="")
    body = c.get("/api/auth/status").json()
    assert body == {"auth_required": False, "authorized": True, "user": "local"}


def test_login_with_named_user_token_sets_raw_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    utok = _make_user(c, "bob")
    assert c.post("/api/login", json={"token": "wrong"}).status_code == 403
    r = c.post("/api/login", json={"token": utok})
    assert r.status_code == 204
    # the cookie stays the RAW supplied token, so later requests re-resolve to bob
    assert c.cookies.get("ada_token") == utok
    status = c.get("/api/auth/status").json()  # cookie-only request
    assert status["authorized"] is True and status["user"] == "bob"


def test_user_admin_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    utok = _make_user(c, "carol")
    hdr = {"Authorization": f"Bearer {utok}"}
    assert c.get("/api/users", headers=hdr).status_code == 403
    assert c.post("/api/users", json={"name": "eve"}, headers=hdr).status_code == 403
    assert c.delete("/api/users/carol", headers=hdr).status_code == 403
    # anonymous requests never even reach the owner gate
    assert c.get("/api/users").status_code == 401


def test_user_create_reveals_token_once_and_stores_only_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    utok = _make_user(c, "dora")
    rows = c.get("/api/users", headers=OWNER_HDR).json()
    assert [r["name"] for r in rows] == ["dora"]
    assert rows[0]["created_at"] and "token" not in rows[0] and "token_sha256" not in rows[0]
    stored = json.loads((tmp_path / "data" / "users.json").read_text())
    assert stored[0]["name"] == "dora"
    assert utok not in json.dumps(stored)  # only the sha256 is persisted
    assert len(stored[0]["token_sha256"]) == 64


def test_user_name_validation_400s(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    for bad in ("Not A Slug", "UPPER", "-lead", "", "owner", "local"):
        r = c.post("/api/users", json={"name": bad}, headers=OWNER_HDR)
        assert r.status_code == 400, bad
    _make_user(c, "erin")
    dup = c.post("/api/users", json={"name": "erin"}, headers=OWNER_HDR)
    assert dup.status_code == 400 and "exists" in dup.json()["error"]


def test_user_delete_revokes_access(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    utok = _make_user(c, "frank")
    hdr = {"Authorization": f"Bearer {utok}"}
    assert c.get("/api/tasks", headers=hdr).status_code == 200
    assert c.delete("/api/users/frank", headers=OWNER_HDR).json()["ok"] is True
    assert c.get("/api/tasks", headers=hdr).status_code == 401  # token stopped resolving
    assert c.post("/api/login", json={"token": utok}).status_code == 403
    assert c.delete("/api/users/frank", headers=OWNER_HDR).status_code == 404


def test_user_admin_open_when_auth_off(tmp_path):
    c = _client(tmp_path, api_token="")  # local single-operator mode
    assert c.get("/api/users").json() == []
    assert c.post("/api/users", json={"name": "solo"}).status_code == 200
    assert c.delete("/api/users/solo").json()["ok"] is True


def test_ws_accepts_named_user_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    utok = _make_user(c, "grace")
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/whatever?token=nope"):
            pass
    with c.websocket_connect(f"/ws/whatever?token={utok}") as ws:
        assert ws.receive_json()["type"] == "error"  # unknown task, but authenticated


def test_run_payload_carries_user_and_task_rows_expose_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    utok = _make_user(c, "hana")
    c.app.state.paused = True  # keep the pump from launching a real engine
    r = c.post("/api/run", json={"prompt": "ship it"},
               headers={"Authorization": f"Bearer {utok}"})
    assert r.status_code == 200
    tid = r.json()["task_id"]
    payload = c.app.state.runs.queue_pending()[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["user"] == "hana"  # additive attribution in the queue payload
    row = next(t for t in c.get("/api/tasks", headers=OWNER_HDR).json() if t["id"] == tid)
    assert row["user"] == "hana"
    (tmp_path / "docs" / tid).mkdir(parents=True)  # docs dir gates the detail route
    detail = c.get(f"/api/tasks/{tid}", headers=OWNER_HDR).json()
    assert detail["meta"]["user"] == "hana"


def test_run_payload_not_stamped_when_auth_off(tmp_path):
    c = _client(tmp_path, api_token="")
    c.app.state.paused = True
    tid = c.post("/api/run", json={"prompt": "local task"}).json()["task_id"]
    payload = c.app.state.runs.queue_pending()[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert "user" not in payload  # "local" carries no attribution value
    row = next(t for t in c.get("/api/tasks").json() if t["id"] == tid)
    assert row["user"] is None


def test_workspace_run_payload_carries_user(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = _client(tmp_path)
    c.app.state.paused = True
    a = c.post("/api/projects", json={"name": "Wu A"}, headers=OWNER_HDR).json()["slug"]
    assert c.post("/api/workspaces", json={"name": "Crew", "projects": [a]},
                  headers=OWNER_HDR).status_code == 200
    utok = _make_user(c, "ivan")
    r = c.post("/api/workspaces/crew/run", json={"prompt": "go"},
               headers={"Authorization": f"Bearer {utok}"})
    assert r.status_code == 200, r.text
    payload = c.app.state.runs.queue_pending()[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["user"] == "ivan" and payload["workspace"] == "crew"


def test_startup_drops_queue_entries_for_terminal_runs(tmp_path):
    app = create_app(_settings(tmp_path), api_token="")
    app.state.runs.enqueue("done-task", "p", None, {"prompt": "p"})
    app.state.runs.set_status("done-task", "completed")  # run row already terminal
    with TestClient(app):
        assert app.state.runs.queue_positions() == {}  # sanitized before pumping
        assert "done-task" not in app.state.running
        assert app.state.tasks == {}
        assert (app.state.runs.get("done-task") or {}).get("status") == "completed"
