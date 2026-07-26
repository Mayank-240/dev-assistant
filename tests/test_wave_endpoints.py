"""Feature-wave web endpoints: playbooks, schedules (+tick), notifications, global
search, spend analytics, KB upload, GitHub poller, and the A/B replay endpoint.
Everything runs offline — no LLM, no network (GitHub uses an injected transport)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest

from ai_dev_assistant import notify
from ai_dev_assistant.config import Settings
from ai_dev_assistant.orchestration.events import Event
from ai_dev_assistant.web.server import create_app

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

TOKEN = "wave-token-42"


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )


def _client(tmp_path, **kw) -> TestClient:
    kw.setdefault("api_token", "")
    c = TestClient(create_app(_settings(tmp_path), **kw))
    c.app.state.paused = True  # keep the pump from ever launching a real engine
    return c


def _payload_of(entry) -> dict:
    payload = entry["payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


# ---------------------------------------------------------------------------
# Playbooks
# ---------------------------------------------------------------------------

def test_playbooks_catalog(tmp_path):
    c = _client(tmp_path)
    cat = c.get("/api/playbooks").json()
    ids = [p["id"] for p in cat]
    assert "raise-coverage" in ids and "security-audit" in ids
    entry = next(p for p in cat if p["id"] == "raise-coverage")
    assert entry["name"] and entry["description"]
    assert any(spec["key"] == "path" for spec in entry["params"])
    assert entry["settings_overrides"].get("agent_effort") == "high"


def test_playbook_run_enqueues_through_the_run_pipeline(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/playbooks/raise-coverage/run",
               json={"params": {"path": "src/x.py", "target_percent": 90}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["title"] == "Raise coverage of src/x.py to 90%"
    tid = body["task_id"]
    pending = {p["task_id"]: p for p in c.app.state.runs.queue_pending()}
    assert tid in pending
    payload = _payload_of(pending[tid])
    assert "src/x.py" in payload["prompt"] and "90 percent" in payload["prompt"]
    # the playbook's Settings overrides ride the payload into _start/_settings_for
    assert payload["settings_overrides"] == {"agent_effort": "high",
                                             "verify_run_tests": True}
    # the run row exists (same enqueue path as /api/run) and a broker was created
    assert (c.app.state.runs.get(tid) or {}).get("status") == "queued"
    assert tid in c.app.state.brokers


def test_playbook_run_bad_params_is_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/playbooks/raise-coverage/run", json={"params": {}})
    assert r.status_code == 400
    assert "missing required param 'path'" in r.json()["error"]
    r = c.post("/api/playbooks/raise-coverage/run",
               json={"params": {"path": "x", "target_percent": "not-a-number"}})
    assert r.status_code == 400
    r = c.post("/api/playbooks/security-audit/run",
               json={"params": {"mode": "nuke-it"}})
    assert r.status_code == 400 and "must be one of" in r.json()["error"]


def test_playbook_run_unknown_playbook_is_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/playbooks/does-not-exist/run", json={"params": {}})
    assert r.status_code == 400
    assert "unknown playbook" in r.json()["error"]


# ---------------------------------------------------------------------------
# Schedules: CRUD + the due tick
# ---------------------------------------------------------------------------

def _make_schedule(c, **over):
    body = {"prompt": "audit the dependencies", "title": "Nightly audit",
            "every_hours": 24, "budget_usd": 2.0}
    body.update(over)
    r = c.post("/api/schedules", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_schedules_crud_roundtrip(tmp_path):
    c = _client(tmp_path)
    row = _make_schedule(c)
    sid = row["id"]
    assert row["project"] == "default" and row["enabled"] is True
    assert row["every_hours"] == 24.0 and row["budget_usd"] == 2.0
    assert row["next_run_at"] is not None  # never run -> due at creation time

    rows = c.get("/api/schedules").json()
    assert [s["id"] for s in rows] == [sid]
    assert all("next_run_at" in s for s in rows)
    assert c.get("/api/schedules", params={"project": "default"}).json()
    assert c.get("/api/schedules", params={"project": "ghost"}).json() == []

    patched = c.patch(f"/api/schedules/{sid}", json={"enabled": False}).json()
    assert patched["enabled"] is False and patched["next_run_at"] is None

    assert c.delete(f"/api/schedules/{sid}").json()["ok"] is True
    assert c.get("/api/schedules").json() == []


def test_schedules_validation_and_missing_ids(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/schedules", json={"prompt": "x", "every_hours": 0.01})
    assert r.status_code == 400 and "every_hours" in r.json()["error"]
    r = c.post("/api/schedules", json={"prompt": "   ", "every_hours": 1})
    assert r.status_code == 400 and "prompt" in r.json()["error"]
    sid = _make_schedule(c)["id"]
    r = c.patch(f"/api/schedules/{sid}", json={"every_hours": 0.05})
    assert r.status_code == 400
    assert c.patch("/api/schedules/nope", json={"enabled": False}).status_code == 404
    assert c.delete("/api/schedules/nope").status_code == 404
    r = c.patch(f"/api/schedules/{sid}", json={})
    assert r.status_code == 400  # "no fields to update"


def test_schedules_tick_enqueues_due_runs_with_fake_clock(tmp_path):
    c = _client(tmp_path)
    row = _make_schedule(c, every_hours=1)
    t0 = time.time()
    # never-run schedule is due immediately
    assert asyncio.run(c.app.state.schedules_tick(t0)) == 1
    pending = c.app.state.runs.queue_pending()
    assert len(pending) == 1
    payload = _payload_of(pending[0])
    assert payload["prompt"] == "audit the dependencies"
    assert payload["title"] == "Nightly audit"
    assert payload["project"] == "default"
    assert payload["budget"] == 2.0
    # mark_started advanced the interval: not due again inside the hour
    assert asyncio.run(c.app.state.schedules_tick(t0 + 60)) == 0
    assert len(c.app.state.runs.queue_pending()) == 1
    # ...but due once the interval elapses
    assert asyncio.run(c.app.state.schedules_tick(t0 + 3700)) == 1
    assert len(c.app.state.runs.queue_pending()) == 2
    # the schedule row records the started run
    updated = next(s for s in c.get("/api/schedules").json() if s["id"] == row["id"])
    assert updated["last_task_id"] in [p["task_id"] for p in c.app.state.runs.queue_pending()]


def test_background_loops_start_and_stop_with_the_app(tmp_path):
    app = create_app(_settings(tmp_path), api_token="")
    app.state.paused = True
    with TestClient(app):
        assert len(app.state.bg_tasks) == 3  # schedules + github + maintenance/spend tickers
        assert all(not t.done() for t in app.state.bg_tasks)
    assert app.state.bg_tasks == []  # cancelled and cleared at shutdown


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def test_settings_schema_exposes_notification_and_github_groups(tmp_path):
    c = _client(tmp_path)
    body = c.get("/api/settings").json()
    groups = {g["name"]: {f["key"]: f for f in g["fields"]} for g in body["groups"]}
    assert {"notify_webhook", "notify_desktop", "notify_events"} <= set(groups["Notifications"])
    assert {"github_repos", "github_label"} <= set(groups["GitHub"])
    for fields in (groups["Notifications"], groups["GitHub"]):
        assert all(f["help"] for f in fields.values())
    # the GitHub token is env-only: never a settings key, never in the response
    all_keys = {k for fields in groups.values() for k in fields}
    assert not any("token" in k for k in all_keys)


def test_notify_dispatch_fires_on_configured_done_event(tmp_path, monkeypatch):
    c = _client(tmp_path)
    assert c.patch("/api/settings", json={
        "notify_webhook": "http://localhost:9999/hook",
        "notify_events": "done,error"}).status_code == 200
    calls = []
    monkeypatch.setattr(notify, "notify_event",
                        lambda cfg, **kw: calls.append((cfg, kw)) or True)
    c.app.state.notify_dispatch(
        Event("done", "Run ended.", {"status": "completed"}), "t-1", "default")
    assert len(calls) == 1
    cfg, kw = calls[0]
    assert cfg.webhook_url == "http://localhost:9999/hook"
    assert cfg.events == ("done", "error")
    assert kw["event_type"] == "done" and kw["task_id"] == "t-1"
    assert kw["project"] == "default" and kw["message"] == "Run ended."
    # event types outside the configured list are filtered before dispatch
    c.app.state.notify_dispatch(Event("status", "chatter", {}), "t-1", "default")
    assert len(calls) == 1


def test_notify_dispatch_noop_without_channels(tmp_path, monkeypatch):
    c = _client(tmp_path)  # no webhook, no desktop -> nothing to send
    calls = []
    monkeypatch.setattr(notify, "notify_event",
                        lambda cfg, **kw: calls.append(kw) or True)
    c.app.state.notify_dispatch(Event("done", "Run ended.", {}), "t-2", "default")
    assert calls == []


# ---------------------------------------------------------------------------
# Global search + analytics (seeded data)
# ---------------------------------------------------------------------------

def _seed_run_and_memory(c, tmp_path):
    from ai_dev_assistant.memory.store import MemoryStore

    st = c.app.state.runs
    st.start("run-s1", "implement the parser module", title="Parser work",
             project="default")
    st.finish("run-s1", status="completed", cost_usd=1.5, input_tokens=100,
              output_tokens=50, summary="parser done", quality_score=88)
    store = MemoryStore(dataclasses.replace(c.app.state.settings, project="default"))
    store.remember("longterm", "the parser prefers recursive descent")
    store.close()


def test_search_finds_tasks_and_memories(tmp_path):
    c = _client(tmp_path)
    _seed_run_and_memory(c, tmp_path)
    hits = c.get("/api/search", params={"q": "parser"}).json()
    assert hits
    assert all({"kind", "project", "title", "snippet", "score", "ref"} <= set(h)
               for h in hits)
    kinds = {h["kind"] for h in hits}
    assert "task" in kinds and "memory" in kinds
    task_hit = next(h for h in hits if h["kind"] == "task")
    assert task_hit["ref"]["task_id"] == "run-s1"
    # kind filter narrows the modalities
    only_tasks = c.get("/api/search", params={"q": "parser", "kinds": "task"}).json()
    assert only_tasks and all(h["kind"] == "task" for h in only_tasks)
    # unknown project filter -> no hits, never an error
    assert c.get("/api/search", params={"q": "parser", "projects": "ghost"}).json() == []


def test_search_empty_query_is_empty_list(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/search").json() == []
    assert c.get("/api/search", params={"q": "   "}).json() == []


def test_analytics_shapes_over_seeded_runs(tmp_path):
    c = _client(tmp_path)
    _seed_run_and_memory(c, tmp_path)
    over = c.get("/api/analytics/overview", params={"days": 30}).json()
    assert over["window_days"] == 30 and over["runs"] >= 1
    assert over["total_usd"] == pytest.approx(1.5)
    assert any(p["project"] == "default" for p in over["by_project"])
    assert "completed" in over["by_status"]

    proj = c.get("/api/analytics/project/default", params={"days": 90}).json()
    assert proj["project"] == "default" and proj["runs"] >= 1

    out = c.get("/api/analytics/outcomes").json()
    assert {"total_usd", "completed_runs", "usd_per_completed_run"} <= set(out)
    assert out["completed_runs"] >= 1
    assert out["usd_per_completed_run"] == pytest.approx(1.5)


def test_analytics_run_breakdown_from_events(tmp_path):
    c = _client(tmp_path)
    _seed_run_and_memory(c, tmp_path)
    docs = tmp_path / "docs" / "run-s1"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "events.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"type": "subtask_start", "data": {"id": "s1", "agent": "coder"}},
        {"type": "subtask_review",
         "data": {"id": "s1", "cost": {"cost_usd": 0.5, "input_tokens": 10,
                                       "output_tokens": 5}}},
    ]) + "\n")
    body = c.get("/api/analytics/run/run-s1").json()
    assert body["task_id"] == "run-s1"
    assert body["subtasks"] == [{"subtask": "s1", "agent": "coder", "usd": 0.5,
                                 "tokens_in": 10, "tokens_out": 5}]
    assert body["run_total_usd"] == pytest.approx(1.5)
    assert body["unattributed_usd"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# KB upload
# ---------------------------------------------------------------------------

def test_kb_upload_roundtrip_search_finds_it(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/projects/default/kb/upload",
               files={"file": ("notes.txt",
                               b"zorblatt frobnicator calibration notes: dial to eleven")})
    assert r.status_code == 200, r.text
    assert r.json() == {"chunks": 1}
    hits = c.get("/api/search", params={"q": "zorblatt", "kinds": "kb"}).json()
    assert hits and hits[0]["kind"] == "kb"
    assert hits[0]["ref"]["ref_id"].startswith("upload:notes.txt")
    # re-upload replaces, never duplicates
    assert c.post("/api/projects/default/kb/upload",
                  files={"file": ("notes.txt", b"zorblatt v2")}).json() == {"chunks": 1}


def test_kb_upload_limits_and_unknown_project(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/projects/ghost/kb/upload", files={"file": ("a.txt", b"x")})
    assert r.status_code == 404
    big = b"x" * (2 * 1024 * 1024 + 1)
    r = c.post("/api/projects/default/kb/upload", files={"file": ("big.txt", big)})
    assert r.status_code == 413
    assert "2 MB" in r.json()["error"]


# ---------------------------------------------------------------------------
# GitHub poller
# ---------------------------------------------------------------------------

def _github_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_GITHUB_TOKEN", "gh-test-token")
    c = _client(tmp_path)
    assert c.patch("/api/settings",
                   json={"github_repos": "acme/widget=default"}).status_code == 200
    calls: list[tuple] = []

    def transport(method, url, headers, body):
        calls.append((method, url, body))
        assert "gh-test-token" in headers.get("Authorization", "")
        if method == "GET" and "/repos/acme/widget/issues?" in url:
            if "page=1" in url:
                return 200, [{"number": 7, "title": "Fix the frobnicator",
                              "body": "It is broken.", "html_url": "http://x/7",
                              "labels": [{"name": "ada"}]}]
            return 200, []
        if method == "POST" and url.endswith("/issues/7/comments"):
            return 201, {}
        if method == "POST" and url.endswith("/repos/acme/widget/pulls"):
            return 201, {"html_url": "https://github.com/acme/widget/pull/9",
                         "number": 9}
        return 200, {}

    c.app.state.github_transport = transport
    return c, calls


def test_github_status_reports_config_without_token(tmp_path, monkeypatch):
    c = _client(tmp_path)
    body = c.get("/api/github/status").json()
    assert body == {"enabled": False, "repos": {}, "label": "ada",
                    "tracked": 0, "seen": 0}
    c2, _calls = _github_client(tmp_path / "gh", monkeypatch)
    body = c2.get("/api/github/status").json()
    assert body["enabled"] is True and body["repos"] == {"acme/widget": "default"}
    assert body["label"] == "ada"
    assert "gh-test-token" not in json.dumps(body)


def test_github_tick_enqueues_comments_and_dedupes(tmp_path, monkeypatch):
    c, calls = _github_client(tmp_path, monkeypatch)
    asyncio.run(c.app.state.github_tick())
    pending = c.app.state.runs.queue_pending()
    assert len(pending) == 1
    payload = _payload_of(pending[0])
    assert payload["prompt"].startswith("Fix the frobnicator")
    assert payload["project"] == "default"
    assert payload["title"] == "Fix the frobnicator"
    tid = pending[0]["task_id"]
    start_comments = [b for (m, u, b) in calls
                      if m == "POST" and u.endswith("/issues/7/comments")]
    assert start_comments == [{"body": f"ADA started task {tid}"}]
    # persisted dedupe state
    state = json.loads((tmp_path / "data" / "github_seen.json").read_text())
    assert state["seen"] == ["acme/widget#7"]
    assert state["tracked"] == {tid: {"repo": "acme/widget", "number": 7}}
    st = c.get("/api/github/status").json()
    assert st["seen"] == 1 and st["tracked"] == 1
    # a second tick sees the same issue but the seen-set suppresses a re-run
    asyncio.run(c.app.state.github_tick())
    assert len(c.app.state.runs.queue_pending()) == 1
    assert len([b for (m, u, b) in calls
                if m == "POST" and u.endswith("/issues/7/comments")]) == 1


def test_github_tick_opens_pr_when_tracked_run_finishes(tmp_path, monkeypatch):
    c, calls = _github_client(tmp_path, monkeypatch)
    asyncio.run(c.app.state.github_tick())
    tid = c.app.state.runs.queue_pending()[0]["task_id"]
    # simulate the run finishing with a delivery branch
    st = c.app.state.runs
    st.set_run_branch(tid, "ada/fix-frob", "main")
    st.finish(tid, status="completed", summary="Fixed the frobnicator.",
              tests="passed", quality_score=90, cost_usd=0.12)
    asyncio.run(c.app.state.github_tick())
    prs = [(u, b) for (m, u, b) in calls if m == "POST" and u.endswith("/pulls")]
    assert len(prs) == 1
    _url, pr_payload = prs[0]
    assert pr_payload["head"] == "ada/fix-frob" and pr_payload["base"] == "main"
    assert pr_payload["title"] == "Fix the frobnicator"
    assert "Fixed the frobnicator." in pr_payload["body"]
    done_comments = [b["body"] for (m, u, b) in calls
                     if m == "POST" and u.endswith("/issues/7/comments")
                     and "finished" in (b or {}).get("body", "")]
    assert done_comments == [
        f"ADA finished task {tid}: https://github.com/acme/widget/pull/9"]
    # untracked after delivery; the issue stays in the seen-set
    body = c.get("/api/github/status").json()
    assert body["tracked"] == 0 and body["seen"] == 1
    # a further tick opens no duplicate PR
    asyncio.run(c.app.state.github_tick())
    assert len([1 for (m, u, _b) in calls if m == "POST" and u.endswith("/pulls")]) == 1


def test_github_tick_disabled_without_config_does_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("ADA_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    c = _client(tmp_path)

    def exploding_transport(*a):  # must never be called while disabled
        raise AssertionError("transport used while integration disabled")

    c.app.state.github_transport = exploding_transport
    asyncio.run(c.app.state.github_tick())
    assert c.app.state.runs.queue_pending() == []


# ---------------------------------------------------------------------------
# A/B replay endpoint
# ---------------------------------------------------------------------------

def test_ab_endpoint_validation_is_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/ab", json={"knob": "NOT_ADA", "values": ["x"]})
    assert r.status_code == 400 and "ADA_" in r.json()["error"]
    r = c.post("/api/ab", json={"knob": "ADA_AGENT_EFFORT", "values": ["low", "low"]})
    assert r.status_code == 400 and "distinct" in r.json()["error"]
    r = c.post("/api/ab", json={"knob": "ADA_AGENT_EFFORT", "values": []})
    assert r.status_code == 400


def test_ab_endpoint_runs_replay_and_returns_report(tmp_path, monkeypatch):
    import ai_dev_assistant.evals.ab as ab_mod

    seen = {}

    class FakeReport:
        def to_dict(self):
            return {"knob": "ADA_AGENT_EFFORT", "best": "low", "arms": []}

    def fake_run_ab(base, knob, values, **kw):
        seen.update(knob=knob, values=values, **kw)
        return FakeReport()

    monkeypatch.setattr(ab_mod, "run_ab", fake_run_ab)
    c = _client(tmp_path)
    r = c.post("/api/ab", json={"knob": "ADA_AGENT_EFFORT", "values": ["low", "high"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"knob": "ADA_AGENT_EFFORT", "best": "low", "arms": []}
    assert seen["knob"] == "ADA_AGENT_EFFORT"
    assert seen["values"] == ["low", "high"]
    assert seen["replay"] is True


# ---------------------------------------------------------------------------
# Auth: every new route sits behind the bearer-token middleware
# ---------------------------------------------------------------------------

def test_wave_endpoints_require_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_API_TOKEN", TOKEN)
    c = TestClient(create_app(_settings(tmp_path)))
    c.app.state.paused = True
    protected = [
        ("GET", "/api/playbooks", {}),
        ("POST", "/api/playbooks/raise-coverage/run", {"json": {"params": {}}}),
        ("GET", "/api/schedules", {}),
        ("POST", "/api/schedules", {"json": {"prompt": "p", "every_hours": 1}}),
        ("PATCH", "/api/schedules/x", {"json": {"enabled": False}}),
        ("DELETE", "/api/schedules/x", {}),
        ("GET", "/api/search?q=x", {}),
        ("GET", "/api/analytics/overview", {}),
        ("GET", "/api/analytics/outcomes", {}),
        ("GET", "/api/analytics/project/default", {}),
        ("GET", "/api/analytics/run/t1", {}),
        ("POST", "/api/projects/default/kb/upload",
         {"files": {"file": ("a.txt", b"x")}}),
        ("GET", "/api/github/status", {}),
        ("POST", "/api/ab", {"json": {"knob": "BAD", "values": []}}),
    ]
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    for method, url, kw in protected:
        r = c.request(method, url, **kw)
        assert r.status_code == 401, f"{method} {url} was not protected"
        r = c.request(method, url, headers=hdr, **kw)
        assert r.status_code != 401, f"{method} {url} rejected a valid token"
