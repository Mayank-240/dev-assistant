"""Offline tests for src/ai_dev_assistant/web/push.py.

No network and no real push service: send tests inject a fake ``webpush``
callable. VAPID tests skip cleanly when ``cryptography`` is absent.
"""

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest

from ai_dev_assistant.web import push as push_mod
from ai_dev_assistant.web.push import (
    add_subscription,
    ensure_vapid_keys,
    list_subscriptions,
    push_available,
    push_unavailable_reason,
    remove_subscription,
    send_push,
    vapid_public_key,
)

STATIC = Path(push_mod.__file__).parent / "static"

needs_crypto = pytest.mark.skipif(
    not push_mod._HAVE_CRYPTOGRAPHY, reason="cryptography not installed"
)


def _sub(n: int = 1) -> dict:
    return {
        "endpoint": f"https://push.example.com/reg/{n}",
        "keys": {"p256dh": f"p256dh-{n}", "auth": f"auth-{n}"},
    }


# ---------------------------------------------------------------- store

class TestSubscriptionStore:
    def test_add_list_roundtrip_with_ua(self, tmp_path):
        entry = add_subscription(tmp_path, _sub(1), ua="Mobile Safari")
        assert entry["ua"] == "Mobile Safari"
        assert entry["created_at"] > 0
        subs = list_subscriptions(tmp_path)
        assert len(subs) == 1
        assert subs[0]["endpoint"] == _sub(1)["endpoint"]
        assert subs[0]["keys"] == {"p256dh": "p256dh-1", "auth": "auth-1"}

    def test_add_dedupes_by_endpoint(self, tmp_path):
        add_subscription(tmp_path, _sub(1))
        replacement = dict(_sub(1), keys={"p256dh": "new", "auth": "new"})
        add_subscription(tmp_path, replacement)
        subs = list_subscriptions(tmp_path)
        assert len(subs) == 1
        assert subs[0]["keys"]["p256dh"] == "new"

    def test_add_rejects_malformed(self, tmp_path):
        with pytest.raises(ValueError):
            add_subscription(tmp_path, {"keys": {"p256dh": "x", "auth": "y"}})
        with pytest.raises(ValueError):
            add_subscription(tmp_path, {"endpoint": "https://e", "keys": {"auth": "y"}})

    def test_remove(self, tmp_path):
        add_subscription(tmp_path, _sub(1))
        add_subscription(tmp_path, _sub(2))
        assert remove_subscription(tmp_path, _sub(1)["endpoint"]) is True
        assert remove_subscription(tmp_path, _sub(1)["endpoint"]) is False
        assert [s["endpoint"] for s in list_subscriptions(tmp_path)] == [
            _sub(2)["endpoint"]
        ]

    def test_tolerant_reads(self, tmp_path):
        # Missing file
        assert list_subscriptions(tmp_path) == []
        path = tmp_path / "push_subscriptions.json"
        # Corrupt JSON
        path.write_text("{not json", encoding="utf-8")
        assert list_subscriptions(tmp_path) == []
        # Wrong top-level shape
        path.write_text('{"endpoint": "x"}', encoding="utf-8")
        assert list_subscriptions(tmp_path) == []
        # Junk entries filtered, good entries kept
        path.write_text(
            json.dumps([42, {"no": "endpoint"}, _sub(3)]), encoding="utf-8"
        )
        assert [s["endpoint"] for s in list_subscriptions(tmp_path)] == [
            _sub(3)["endpoint"]
        ]


# ---------------------------------------------------------------- vapid

@needs_crypto
class TestVapid:
    def test_ensure_idempotent_and_0600(self, tmp_path):
        first = ensure_vapid_keys(tmp_path)
        second = ensure_vapid_keys(tmp_path)
        assert first == second
        mode = stat.S_IMODE((tmp_path / "vapid.json").stat().st_mode)
        assert mode == 0o600

    def test_public_key_is_urlsafe_b64_uncompressed_point(self, tmp_path):
        pub = vapid_public_key(tmp_path)
        assert pub is not None
        assert "=" not in pub and "+" not in pub and "/" not in pub
        raw = base64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))
        assert len(raw) == 65 and raw[0] == 0x04  # uncompressed P-256 point
        # Private key is the raw 32-byte scalar, also urlsafe b64.
        priv = ensure_vapid_keys(tmp_path)["private_key"]
        praw = base64.urlsafe_b64decode(priv + "=" * (-len(priv) % 4))
        assert len(praw) == 32

    def test_corrupt_vapid_file_regenerates(self, tmp_path):
        (tmp_path / "vapid.json").write_text("garbage", encoding="utf-8")
        pair = ensure_vapid_keys(tmp_path)
        assert pair is not None and pair["public_key"]


# ---------------------------------------------------------------- send

@needs_crypto
class TestSendPush:
    def test_send_success_passes_json_payload(self, tmp_path):
        add_subscription(tmp_path, _sub(1))
        add_subscription(tmp_path, _sub(2))
        calls = []

        def fake(**kwargs):
            calls.append(kwargs)

        payload = {"title": "Agent asks", "body": "?", "tag": "t1", "url": "/app#task=abc"}
        result = send_push(tmp_path, payload, webpush=fake)
        assert result == {"sent": 2, "failed": 0, "gone": 0}
        assert len(calls) == 2
        assert json.loads(calls[0]["data"]) == payload  # JSON string, not dict
        assert calls[0]["subscription_info"]["endpoint"] == _sub(1)["endpoint"]
        assert calls[0]["subscription_info"]["keys"]["auth"] == "auth-1"
        assert calls[0]["vapid_private_key"] == ensure_vapid_keys(tmp_path)["private_key"]
        assert calls[0]["vapid_claims"]["sub"].startswith("mailto:")

    def test_per_endpoint_failure_counted_not_raised(self, tmp_path):
        add_subscription(tmp_path, _sub(1))
        add_subscription(tmp_path, _sub(2))

        def fake(*, subscription_info, **kwargs):
            if subscription_info["endpoint"] == _sub(1)["endpoint"]:
                raise RuntimeError("push service down")

        result = send_push(tmp_path, {"title": "x"}, webpush=fake)
        assert result == {"sent": 1, "failed": 1, "gone": 0}
        assert len(list_subscriptions(tmp_path)) == 2  # failures don't prune

    @pytest.mark.parametrize("status", [404, 410])
    def test_gone_prunes_subscription(self, tmp_path, status):
        add_subscription(tmp_path, _sub(1))
        add_subscription(tmp_path, _sub(2))

        class FakeResponse:
            status_code = status

        def fake(*, subscription_info, **kwargs):
            if subscription_info["endpoint"] == _sub(1)["endpoint"]:
                exc = RuntimeError("gone")
                exc.response = FakeResponse()
                raise exc

        result = send_push(tmp_path, {"title": "x"}, webpush=fake)
        assert result == {"sent": 1, "failed": 0, "gone": 1}
        assert [s["endpoint"] for s in list_subscriptions(tmp_path)] == [
            _sub(2)["endpoint"]
        ]

    def test_no_subscriptions_is_a_quiet_noop(self, tmp_path):
        def boom(**kwargs):  # must never be called
            raise AssertionError("webpush called with no subscriptions")

        assert send_push(tmp_path, {"title": "x"}, webpush=boom) == {
            "sent": 0, "failed": 0, "gone": 0,
        }


# ---------------------------------------------------------------- unavailable

class TestUnavailable:
    def test_missing_pywebpush_reports_unavailable(self, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_pywebpush(name, *args, **kwargs):
            if name == "pywebpush":
                raise ImportError("no pywebpush")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pywebpush)
        add_subscription(tmp_path, _sub(1))
        result = send_push(tmp_path, {"title": "x"})  # no injected sender
        assert result == {"sent": 0, "failed": 0, "gone": 0, "unavailable": True}

    def test_missing_cryptography_reports_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(push_mod, "_HAVE_CRYPTOGRAPHY", False)
        assert ensure_vapid_keys(tmp_path) is None
        assert vapid_public_key(tmp_path) is None
        assert push_available() is False
        assert "cryptography" in push_unavailable_reason()
        result = send_push(tmp_path, {"title": "x"}, webpush=lambda **kw: None)
        assert result == {"sent": 0, "failed": 0, "gone": 0, "unavailable": True}

    def test_available_reports_no_reason(self):
        if push_available():
            assert push_unavailable_reason() is None
        else:
            assert isinstance(push_unavailable_reason(), str)


# ---------------------------------------------------------------- assets

class TestStaticAssets:
    def test_manifest_exists_and_has_required_fields(self):
        manifest = json.loads(
            (STATIC / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        assert manifest["name"] == "AI Dev Assistant"
        assert manifest["short_name"] == "ADA"
        assert manifest["start_url"] == "/app"
        assert manifest["display"] == "standalone"
        assert manifest["theme_color"] == "#16130f"
        assert manifest["background_color"] == "#16130f"
        icons = manifest["icons"]
        assert icons and all(i["type"] == "image/svg+xml" for i in icons)
        assert all(i["src"] == "/static/icon.svg" for i in icons)
        assert (STATIC / "icon.svg").read_text(encoding="utf-8").startswith("<svg")

    def test_service_worker_exists_with_push_handlers(self):
        sw = (STATIC / "sw.js").read_text(encoding="utf-8")
        assert "addEventListener('push'" in sw
        assert "addEventListener('notificationclick'" in sw
        assert "showNotification" in sw
