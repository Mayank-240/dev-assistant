"""Web Push plumbing for the console PWA.

Attention requests (agent asks / permission requests) need to reach the user
even when the console tab is closed — on the phone that means Web Push through
the browser's push service, delivered to the service worker registered by the
console (``web/static/sw.js``).

This module owns the server-side pieces:

- a subscription store at ``<data_dir>/push_subscriptions.json``,
- VAPID key management (auto-generated once into ``<data_dir>/vapid.json``,
  file mode 0600),
- ``send_push()`` — fan a JSON payload out to every subscription, pruning
  endpoints the push service reports as gone (404/410).

Dependencies degrade gracefully: key generation needs ``cryptography`` (a
transitive dependency of ``pywebpush``), sending needs ``pywebpush`` (the
optional ``push`` extra). When either is missing nothing raises — the feature
just reports itself unavailable via :func:`push_available` /
:func:`push_unavailable_reason` so the UI can say why.

Payload convention (what ``sw.js`` expects): ``{title, body, tag, url}`` where
``url`` is a console deep link like ``/app#task=<id>``.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

# ``cryptography`` ships as a transitive dependency of pywebpush (via
# py-vapid); probe it at import time so availability checks are cheap.
try:  # pragma: no cover - exercised implicitly by every test run
    from cryptography.hazmat.primitives import serialization as _serialization
    from cryptography.hazmat.primitives.asymmetric import ec as _ec

    _HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover
    _serialization = None  # type: ignore[assignment]
    _ec = None  # type: ignore[assignment]
    _HAVE_CRYPTOGRAPHY = False

SUBSCRIPTIONS_FILENAME = "push_subscriptions.json"
VAPID_FILENAME = "vapid.json"

# JWT "sub" claim sent to push services with each message (they may use it to
# contact the operator of a misbehaving sender). Override via env if desired.
_DEFAULT_VAPID_SUB = "mailto:admin@example.com"


def _vapid_sub() -> str:
    return os.environ.get("ADA_VAPID_SUB", "").strip() or _DEFAULT_VAPID_SUB


def _b64url(raw: bytes) -> str:
    """Unpadded urlsafe base64 — the alphabet Web Push APIs speak."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def _have_pywebpush() -> bool:
    try:
        return importlib.util.find_spec("pywebpush") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def push_available() -> bool:
    """True when this install can both mint VAPID keys and send pushes."""
    return _HAVE_CRYPTOGRAPHY and _have_pywebpush()


def push_unavailable_reason() -> str | None:
    """One-line explanation for the UI, or None when push works."""
    if not _HAVE_CRYPTOGRAPHY:
        return "the 'cryptography' package is not installed (install the 'push' extra)"
    if not _have_pywebpush():
        return "the 'pywebpush' package is not installed (install the 'push' extra)"
    return None


# ---------------------------------------------------------------------------
# Subscription store
# ---------------------------------------------------------------------------

def _subs_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / SUBSCRIPTIONS_FILENAME


def list_subscriptions(data_dir: Path | str) -> list[dict]:
    """All stored subscriptions; [] on missing/corrupt/odd-shaped file."""
    path = _subs_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [
        s for s in raw
        if isinstance(s, dict) and isinstance(s.get("endpoint"), str) and s["endpoint"]
    ]


def _save_subscriptions(data_dir: Path | str, subs: list[dict]) -> None:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    _subs_path(data_dir).write_text(
        json.dumps(subs, indent=2) + "\n", encoding="utf-8"
    )


def add_subscription(
    data_dir: Path | str, subscription: dict, *, ua: str | None = None
) -> dict:
    """Store a browser PushSubscription (deduped by endpoint) and return it.

    ``subscription`` is the JSON a browser's ``PushSubscription.toJSON()``
    produces: ``{endpoint, keys: {p256dh, auth}}``. Raises ValueError when the
    shape is unusable — callers turn that into a 400.
    """
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("subscription missing endpoint")
    if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("subscription missing keys.p256dh/keys.auth")

    entry: dict[str, Any] = {
        "endpoint": endpoint,
        "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
        "created_at": time.time(),
    }
    if ua:
        entry["ua"] = ua

    subs = [s for s in list_subscriptions(data_dir) if s.get("endpoint") != endpoint]
    subs.append(entry)
    _save_subscriptions(data_dir, subs)
    return entry


def remove_subscription(data_dir: Path | str, endpoint: str) -> bool:
    """Drop the subscription with this endpoint. True if one was removed."""
    subs = list_subscriptions(data_dir)
    kept = [s for s in subs if s.get("endpoint") != endpoint]
    if len(kept) == len(subs):
        return False
    _save_subscriptions(data_dir, kept)
    return True


# ---------------------------------------------------------------------------
# VAPID keys
# ---------------------------------------------------------------------------

def ensure_vapid_keys(data_dir: Path | str) -> dict | None:
    """Load-or-create the VAPID key pair; None when cryptography is absent.

    Returns ``{public_key, private_key}`` — both unpadded urlsafe base64:
    the public key as the 65-byte uncompressed P-256 point (exactly what
    browsers want for ``applicationServerKey``), the private key as the raw
    32-byte scalar (the string form ``pywebpush`` accepts directly).
    """
    if not _HAVE_CRYPTOGRAPHY:
        return None

    data_dir = Path(data_dir)
    path = data_dir / VAPID_FILENAME
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (
            isinstance(existing, dict)
            and isinstance(existing.get("public_key"), str)
            and isinstance(existing.get("private_key"), str)
        ):
            return {
                "public_key": existing["public_key"],
                "private_key": existing["private_key"],
            }
    except (OSError, ValueError):
        pass  # missing or corrupt — regenerate below

    key = _ec.generate_private_key(_ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(
        _serialization.Encoding.X962,
        _serialization.PublicFormat.UncompressedPoint,
    )
    pair = {
        "public_key": _b64url(public_raw),
        "private_key": _b64url(private_raw),
    }

    data_dir.mkdir(parents=True, exist_ok=True)
    record = dict(pair, created_at=time.time())
    # Private key material: create with 0600 from the start.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2) + "\n")
    except Exception:
        os.close(fd)  # pragma: no cover - fdopen failure only
        raise
    os.chmod(path, 0o600)  # in case the file pre-existed with wider bits
    return pair


def vapid_public_key(data_dir: Path | str) -> str | None:
    """The applicationServerKey for the browser, or None when unavailable."""
    pair = ensure_vapid_keys(data_dir)
    return pair["public_key"] if pair else None


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_push(
    data_dir: Path | str,
    payload: dict,
    *,
    webpush: Callable[..., Any] | None = None,
) -> dict:
    """Send ``payload`` (as JSON) to every stored subscription.

    Returns ``{sent, failed, gone}`` — ``gone`` counts subscriptions the push
    service reported 404/410 for, which are pruned from the store. When the
    sender stack is unavailable the dict also carries ``unavailable: True``.
    Never raises; per-endpoint failures are counted, not propagated.

    ``webpush`` is injectable for tests; by default ``pywebpush.webpush`` is
    imported lazily.
    """
    result = {"sent": 0, "failed": 0, "gone": 0}

    if webpush is None:
        try:
            from pywebpush import webpush as _real_webpush
        except ImportError:
            return {**result, "unavailable": True}
        webpush = _real_webpush

    keys = ensure_vapid_keys(data_dir)
    if keys is None:
        return {**result, "unavailable": True}

    data = json.dumps(payload)
    gone_endpoints: list[str] = []
    for sub in list_subscriptions(data_dir):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": sub.get("keys", {}),
                },
                data=data,
                vapid_private_key=keys["private_key"],
                vapid_claims={"sub": _vapid_sub()},
            )
            result["sent"] += 1
        except Exception as exc:
            # pywebpush raises WebPushException carrying the HTTP response;
            # 404/410 mean the subscription is dead — prune it.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                result["gone"] += 1
                gone_endpoints.append(sub["endpoint"])
            else:
                result["failed"] += 1

    for endpoint in gone_endpoints:
        remove_subscription(data_dir, endpoint)
    return result
