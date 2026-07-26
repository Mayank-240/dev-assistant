"""Server wiring for the mega-wave core modules: Web Push endpoints + event dispatch,
the maintenance enqueue tick, monthly spend alerts, and the backup endpoints.

Offline throughout: push sends inject a fake ``webpush`` callable (the server's
``app.state.push_webpush`` hook, mirroring ``github_transport``), spend alerts inject
a fake notify Transport (``app.state.notify_transport``), and the queue pump is
paused so no engine ever starts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from ai_dev_assistant import projects
from ai_dev_assistant.config import Settings
from ai_dev_assistant.orchestration.events import Event
from ai_dev_assistant.web import push as push_mod
from ai_dev_assistant.web.server import create_app

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

T0 = datetime(2026, 7, 1, 12, 0).timestamp()  # deterministic local noon
HOUR = 3600.0


def _client(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    return TestClient(create_app(settings, api_token=""))


def _sub(n: int = 1) -> dict:
    return {"endpoint": f"https://push.example.com/reg/{n}",
            "keys": {"p256dh": f"p256dh-{n}", "auth": f"auth-{n}"}}


class _FakeWebpush:
    """Stands in for pywebpush.webpush; records every (endpoint, payload)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, *, subscription_info, data, **_kw) -> None:
        self.calls.append((subscription_info["endpoint"], json.loads(data)))


# ---------------------------------------------------------------------------
# Web Push endpoints
# ---------------------------------------------------------------------------

def test_push_status_subscribe_unsubscribe_roundtrip(tmp_path):
    c = _client(tmp_path)
    body = c.get("/api/push/status").json()
    assert body["available"] is True and body["subscriptions"] == 0
    assert body["public_key"]  # first status call mints the VAPID pair

    r = c.post("/api/push/subscribe", json={"subscription": _sub(1), "ua": "Mobile Safari"})
    assert r.status_code == 200 and r.json()["endpoint"] == _sub(1)["endpoint"]
    assert c.get("/api/push/status").json()["subscriptions"] == 1

    r = c.request("DELETE", "/api/push/subscribe",
                  json={"endpoint": _sub(1)["endpoint"]})
    assert r.json() == {"ok": True, "removed": True}
    assert c.get("/api/push/status").json()["subscriptions"] == 0
    # removing again reports removed: False, still 200
    r = c.request("DELETE", "/api/push/subscribe",
                  json={"endpoint": _sub(1)["endpoint"]})
    assert r.json()["removed"] is False


def test_push_status_reports_unavailable_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(push_mod, "_HAVE_CRYPTOGRAPHY", False)
    c = _client(tmp_path)
    body = c.get("/api/push/status").json()
    assert body["available"] is False
    assert "cryptography" in body["reason"]
    assert "public_key" not in body


def test_push_subscribe_bad_shape_is_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://e/1"}})
    assert r.status_code == 400 and "keys" in r.json()["error"]


def test_push_test_sends_via_injected_webpush(tmp_path):
    c = _client(tmp_path)
    c.post("/api/push/subscribe", json={"subscription": _sub(1)})
    fake = _FakeWebpush()
    c.app.state.push_webpush = fake

    result = c.post("/api/push/test").json()
    assert result["sent"] == 1 and result["failed"] == 0
    endpoint, payload = fake.calls[0]
    assert endpoint == _sub(1)["endpoint"]
    assert set(payload) == {"title", "body", "tag", "url"}


# ---------------------------------------------------------------------------
# Event dispatch -> Web Push (same kinds as the notify_events selection)
# ---------------------------------------------------------------------------

def test_dispatch_pushes_selected_event_kinds(tmp_path):
    c = _client(tmp_path)
    c.post("/api/push/subscribe", json={"subscription": _sub(1)})
    fake = _FakeWebpush()
    c.app.state.push_webpush = fake

    # default selection: ask, permission, done, error — status is NOT selected
    c.app.state.notify_dispatch(Event("done", "Run ended.", {}), "t-1", "proj-a")
    c.app.state.notify_dispatch(Event("status", "working…", {}), "t-1", "proj-a")
    assert len(fake.calls) == 1
    _endpoint, payload = fake.calls[0]
    assert payload == {"title": "[proj-a] done", "body": "Run ended.",
                       "tag": "t-1", "url": "/app#task=t-1"}

    # narrowing notify_events narrows push identically (same selection, live settings)
    assert c.patch("/api/settings", json={"notify_events": "error"}).status_code == 200
    c.app.state.notify_dispatch(Event("done", "Run ended.", {}), "t-2", "proj-a")
    assert len(fake.calls) == 1
    c.app.state.notify_dispatch(Event("error", "boom", {}), "t-2", "proj-a")
    assert len(fake.calls) == 2 and fake.calls[1][1]["tag"] == "t-2"


def test_dispatch_push_failure_never_raises(tmp_path):
    c = _client(tmp_path)
    c.post("/api/push/subscribe", json={"subscription": _sub(1)})

    def _boom(**_kw):
        raise RuntimeError("push service down")

    c.app.state.push_webpush = _boom
    c.app.state.notify_dispatch(Event("done", "Run ended.", {}), "t-1", "proj-a")  # no raise


# ---------------------------------------------------------------------------
# Maintenance: policy endpoints + the enqueue tick
# ---------------------------------------------------------------------------

def _project(c) -> str:
    return projects.create_project(c.app.state.base_settings, "Alpha")["slug"]


def test_maintenance_policy_get_put_and_validation(tmp_path):
    c = _client(tmp_path)
    slug = _project(c)

    pol = c.get(f"/api/projects/{slug}/maintenance").json()
    assert pol["enabled"] is False and pol["cadence"] is None

    r = c.put(f"/api/projects/{slug}/maintenance",
              json={"enabled": True, "cadence": 4, "tasks": ["doc-drift"]})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True and r.json()["cadence"] == 4.0
    assert c.get(f"/api/projects/{slug}/maintenance").json()["tasks"] == ["doc-drift"]

    r = c.put(f"/api/projects/{slug}/maintenance", json={"cadence": -1})
    assert r.status_code == 400 and "must be > 0" in r.json()["error"]
    r = c.put(f"/api/projects/{slug}/maintenance", json={"tasks": ["nope"]})
    assert r.status_code == 400 and "unknown maintenance task" in r.json()["error"]

    assert c.get("/api/projects/nope/maintenance").status_code == 404
    assert c.put("/api/projects/nope/maintenance", json={}).status_code == 404


def test_maintenance_tick_enqueues_once_and_marks_started(tmp_path):
    c = _client(tmp_path)
    c.app.state.paused = True  # keep the pump from ever launching a real engine
    slug = _project(c)
    r = c.put(f"/api/projects/{slug}/maintenance",
              json={"enabled": True, "cadence": 4,
                    "tasks": ["doc-drift", "dead-code"], "budget_usd": 2.5})
    assert r.status_code == 200, r.text

    started = asyncio.run(c.app.state.maintenance_tick(T0))
    assert started == 2
    pending = c.app.state.runs.queue_pending()
    assert len(pending) == 2
    payloads = [json.loads(p["payload"]) if isinstance(p["payload"], str) else p["payload"]
                for p in pending]
    for payload in payloads:
        assert payload["maintenance"] is True
        assert payload["project"] == slug
        assert payload["budget"] == 2.5
        assert payload["prompt"] and payload["title"]
        assert "settings_overrides" in payload
    # last_run_at advanced exactly once, to the injected now
    assert c.get(f"/api/projects/{slug}/maintenance").json()["last_run_at"] == T0

    # idempotent within the cadence window: a re-tick enqueues nothing new
    assert asyncio.run(c.app.state.maintenance_tick(T0 + HOUR)) == 0
    assert len(c.app.state.runs.queue_pending()) == 2
    # …and fires again once the cadence has elapsed
    assert asyncio.run(c.app.state.maintenance_tick(T0 + 5 * HOUR)) == 2


# ---------------------------------------------------------------------------
# Spend alerts (monthly_budget_usd; fire-once per month)
# ---------------------------------------------------------------------------

def test_spend_alerts_tick_fires_notify_and_push_once(tmp_path):
    c = _client(tmp_path)
    sent = []

    def transport(url, payload, headers):
        sent.append((url, json.loads(payload.decode("utf-8"))))

    c.app.state.notify_transport = transport
    c.post("/api/push/subscribe", json={"subscription": _sub(1)})
    fake_push = _FakeWebpush()
    c.app.state.push_webpush = fake_push

    assert c.patch("/api/settings", json={
        "monthly_budget_usd": 10.0,
        "notify_webhook": "https://hooks.example.com/ada",
    }).status_code == 200

    # 6 USD of spend against a 10 USD cap -> exactly the 50% threshold
    st = c.app.state.runs
    st.start("r-spend", "expensive work")
    st.finish("r-spend", status="completed", cost_usd=6.0)

    assert asyncio.run(c.app.state.spend_alerts_tick(T0)) == 1
    assert len(sent) == 1
    url, payload = sent[0]
    assert url == "https://hooks.example.com/ada"
    assert payload["event"] == "spend_alert" and payload["data"]["percent"] == 50
    assert payload["data"]["monthly_cap"] == 10.0
    assert len(fake_push.calls) == 1
    assert "50%" in fake_push.calls[0][1]["title"]

    # fire-once per calendar month: a re-tick is silent
    assert asyncio.run(c.app.state.spend_alerts_tick(T0)) == 0
    assert len(sent) == 1 and len(fake_push.calls) == 1


def test_spend_alerts_tick_disabled_without_cap(tmp_path):
    c = _client(tmp_path)
    sent = []
    c.app.state.notify_transport = lambda *a: sent.append(a)
    st = c.app.state.runs
    st.start("r-spend", "work")
    st.finish("r-spend", status="completed", cost_usd=100.0)
    assert asyncio.run(c.app.state.spend_alerts_tick(T0)) == 0  # monthly cap defaults to 0
    assert sent == []


# ---------------------------------------------------------------------------
# Backup endpoints (restore stays CLI-only)
# ---------------------------------------------------------------------------

def test_backup_create_list_download(tmp_path):
    c = _client(tmp_path)
    body = c.post("/api/backup").json()
    assert body["path"].endswith(".tar.gz") and body["size"] > 0

    rows = c.get("/api/backups").json()
    assert len(rows) == 1 and rows[0]["path"] == body["path"]
    assert rows[0]["size"] == body["size"] and rows[0]["created"]

    # download by absolute path and by bare filename (resolved into backups/)
    r = c.get("/api/backup/download", params={"path": body["path"]})
    assert r.status_code == 200 and len(r.content) == body["size"]
    name = body["path"].rsplit("/", 1)[-1]
    r = c.get("/api/backup/download", params={"path": name})
    assert r.status_code == 200 and len(r.content) == body["size"]


def test_backup_download_traversal_guard(tmp_path):
    c = _client(tmp_path)
    c.post("/api/backup")
    settings = c.app.state.base_settings
    (settings.data_dir / "secret.txt").write_text("token")
    # relative traversal, absolute escape, and non-archive names are all 404
    for path in ("../secret.txt", str(settings.data_dir / "secret.txt"),
                 "../../etc/passwd", "", "."):
        assert c.get("/api/backup/download",
                     params={"path": path}).status_code == 404, path
