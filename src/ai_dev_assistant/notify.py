"""Out-of-tab notifications for the moments that need a human.

The engine emits ``ask``/``permission`` events when an agent is blocked on operator
input, and ``done``/``error`` when a run finishes — exactly the moments where someone
who tabbed away should be pulled back. The web server calls :func:`notify_event` from
its event stream; this module fans the event out to the configured channels:

* **Webhook** — a JSON POST to an operator-supplied URL (Slack shim, ntfy, home-grown
  receiver, …).
* **macOS desktop** — an ``osascript 'display notification'`` banner (darwin only).

Design constraints:

* stdlib only (``urllib`` + ``subprocess``);
* :func:`notify_event` NEVER raises — a broken notifier must not take down a run.
  Failures are logged and reported as ``False``;
* everything that leaves the process is passed through the secret redactor first.

Security decisions (documented on the relevant helpers too):

* Webhook URLs must be ``http(s)``. Private/loopback/link-local IP literals are
  rejected to keep a mis-pasted (or attacker-influenced) URL from turning the server
  into an SSRF probe of the internal network — EXCEPT the explicit spellings
  ``localhost`` / ``127.0.0.1`` / ``::1``, because a self-hosted receiver on the same
  box is the single most common legitimate setup. Hostnames are validated as written;
  we deliberately do not resolve DNS (a resolving check is TOCTOU-racy anyway), so a
  public DNS name pointing at a private address is out of scope and the operator's
  responsibility.
* Event ``data`` is slimmed before POSTing: result bodies, diffs and other bulky keys
  are dropped, nested containers are dropped, and the surviving strings are redacted
  and truncated. Notifications are pings, not transcripts.
* The osascript command is built with every interpolated string escaped
  (backslashes then double quotes), closing the AppleScript-injection hole that raw
  agent-authored text would otherwise open.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from .security.redaction import redact

log = logging.getLogger(__name__)

# transport signature: (url, payload_bytes, headers) -> None; raises on failure.
Transport = Callable[[str, bytes, dict[str, str]], None]

_DEFAULT_EVENTS = ("ask", "permission", "done", "error")
_TRUTHY = {"1", "true", "yes", "on"}

# data keys that tend to carry result bodies / bulk text — never shipped off-box.
_DROP_DATA_KEYS = {
    "result", "results", "output", "stdout", "stderr", "diff", "patch", "body",
    "content", "text", "answer", "brief", "tldr", "key_points", "question",
    "request", "options", "log", "logs", "traceback",
}
_MAX_DATA_KEYS = 20
_MAX_DATA_STR = 200
_MAX_DESKTOP_TEXT = 200
_WEBHOOK_TIMEOUT_S = 5.0
_OSASCRIPT_TIMEOUT_S = 5.0

_ALLOWED_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class NotifyConfig:
    webhook_url: str = ""  # ADA_NOTIFY_WEBHOOK
    desktop: bool = False  # ADA_NOTIFY_DESKTOP (darwin only; auto-false elsewhere)
    events: tuple[str, ...] = _DEFAULT_EVENTS  # ADA_NOTIFY_EVENTS csv

    @classmethod
    def from_env(cls) -> "NotifyConfig":
        webhook = os.environ.get("ADA_NOTIFY_WEBHOOK", "").strip()
        desktop_raw = os.environ.get("ADA_NOTIFY_DESKTOP", "").strip().lower()
        # Desktop banners are osascript-only, so the knob is inert off-macOS: it is
        # forced False rather than left to fail at notify time.
        desktop = desktop_raw in _TRUTHY and sys.platform == "darwin"
        events_raw = os.environ.get("ADA_NOTIFY_EVENTS", "")
        events = tuple(e.strip().lower() for e in events_raw.split(",") if e.strip())
        return cls(webhook_url=webhook, desktop=desktop,
                   events=events or _DEFAULT_EVENTS)


def should_notify(cfg: NotifyConfig, event_type: str) -> bool:
    """True when this event type is on the configured notify list."""
    return event_type in cfg.events


def redact_for_notification(message: str) -> str:
    """Scrub secret-shaped strings before a message leaves the process."""
    return redact(message or "")


def _webhook_url_ok(url: str) -> bool:
    """Validate a webhook destination. http(s) only; private/loopback IP literals are
    rejected (SSRF hardening) except the explicit localhost/127.0.0.1/::1 spellings,
    which stay allowed because self-hosted receivers on the same machine are a
    legitimate, common setup. See the module docstring for the full rationale."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in _ALLOWED_LOOPBACK_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True  # a DNS name, validated as written (no resolution — see docstring)
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
        return False
    return True


def _slim_data(data: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only shallow scalar fields; drop result-body-ish keys and nested
    containers; redact + truncate surviving strings. Notifications carry context
    for routing/display, never run output."""
    if not data:
        return {}
    slim: dict[str, Any] = {}
    for key, value in data.items():
        if len(slim) >= _MAX_DATA_KEYS:
            break
        if key.lower() in _DROP_DATA_KEYS:
            continue
        if isinstance(value, str):
            slim[key] = redact_for_notification(value)[:_MAX_DATA_STR]
        elif isinstance(value, (int, float, bool)) or value is None:
            slim[key] = value
        # dicts/lists/objects: dropped — that is where result bodies live
    return slim


def _default_transport(url: str, payload: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_S):  # noqa: S310 (validated)
        pass


def _send_webhook(cfg: NotifyConfig, payload: dict[str, Any],
                  transport: Transport | None) -> bool:
    if not cfg.webhook_url:
        return False
    if not _webhook_url_ok(cfg.webhook_url):
        log.warning("notify: webhook URL rejected (scheme/host policy): %r",
                    cfg.webhook_url)
        return False
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "User-Agent": "ai-dev-assistant-notify"}
    send = transport or _default_transport
    try:
        send(cfg.webhook_url, body, headers)
        return True
    except Exception:
        log.warning("notify: webhook delivery failed", exc_info=True)
        return False


def _applescript_escape(text: str) -> str:
    """Escape a string for interpolation into an AppleScript double-quoted literal.
    Backslashes first, then double quotes — otherwise the escaping escapes itself.
    Newlines become spaces (a banner is one line anyway) so text can never break
    out of the literal."""
    return (text.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", " ").replace("\r", " "))


def _send_desktop(cfg: NotifyConfig, *, event_type: str, project: str,
                  message: str) -> bool:
    if not cfg.desktop or sys.platform != "darwin":
        return False
    text = _applescript_escape(
        redact_for_notification(message)[:_MAX_DESKTOP_TEXT])
    title = _applescript_escape(f"ADA · {project}")
    subtitle = _applescript_escape(event_type)
    script = (f'display notification "{text}" '
              f'with title "{title}" subtitle "{subtitle}"')
    try:
        proc = subprocess.run(  # noqa: S603 (fixed binary, escaped args)
            ["osascript", "-e", script],
            capture_output=True, timeout=_OSASCRIPT_TIMEOUT_S, check=False,
        )
        if proc.returncode != 0:
            log.warning("notify: osascript exited %s: %s",
                        proc.returncode, proc.stderr.decode(errors="replace")[:200])
            return False
        return True
    except Exception:
        log.warning("notify: desktop notification failed", exc_info=True)
        return False


def notify_event(cfg: NotifyConfig, *, event_type: str, task_id: str, project: str,
                 message: str, data: dict[str, Any] | None = None,
                 transport: Transport | None = None) -> bool:
    """Fan one engine event out to every configured channel.

    Returns True if at least one channel fired. NEVER raises: a notification is a
    convenience, and a broken webhook or a missing osascript must not surface as a
    run failure — problems are logged and reported as False.

    ``transport`` (``callable(url, payload_bytes, headers)``) replaces the default
    urllib POST; tests inject it to stay offline.
    """
    try:
        if not should_notify(cfg, event_type):
            return False
        payload = {
            "event": event_type,
            "task_id": task_id,
            "project": project,
            "message": redact_for_notification(message),
            "ts": time.time(),
            "data": _slim_data(data),
        }
        sent_webhook = _send_webhook(cfg, payload, transport)
        sent_desktop = _send_desktop(cfg, event_type=event_type, project=project,
                                     message=message)
        return sent_webhook or sent_desktop
    except Exception:
        # Belt over the per-channel braces: nothing escapes notify_event.
        log.warning("notify: unexpected failure", exc_info=True)
        return False
