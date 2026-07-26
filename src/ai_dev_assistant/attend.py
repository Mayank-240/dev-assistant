"""Terminal attention client (`ada attend`).

Polls a running server's ``GET /api/home`` for open attention requests —
clarifying questions (``ask``) and permission requests (``permission``) raised
by agents mid-run — renders each new one in the terminal, reads an answer on
stdin, and submits it through the same steer channel the web console uses:

    POST /api/run/{task_id}/steer   {"note": "<note>"}

with the note shaped exactly like the console's attention screen produces:

    [answer <id>] <free text or chosen option>
    [permission <id>] ALLOW ONCE: … | ALLOW FOR THIS RUN: … | DENIED: …

Stdlib only (urllib); auth via ``Authorization: Bearer <token>`` when a token
is supplied (``--token`` / ``ADA_API_TOKEN``).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

DEFAULT_URL = "http://127.0.0.1:8000"
POLL_SECONDS = 5.0
_MAX_BACKOFF = 60.0

_PERMISSION_HELP = ("[y]=allow for this run · [once]=allow once · [n]=deny · "
                    "anything else denies with your text as the reason")


class AttendClient:
    """Minimal JSON-over-HTTP client for the attention endpoints."""

    def __init__(self, base_url: str = DEFAULT_URL, token: str | None = None,
                 timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def fetch_attention(self) -> list[dict]:
        home = self._request("GET", "/api/home")
        items = home.get("attention") or []
        return [i for i in items if isinstance(i, dict)]

    def steer(self, task_id: str, note: str) -> None:
        """Deliver an answer note; raises urllib.error.HTTPError if the run is
        no longer live (the server 404s when there is no running task)."""
        self._request("POST", f"/api/run/{task_id}/steer", {"note": note})


# ---- rendering & answer mapping ------------------------------------------------
def format_item(item: dict) -> str:
    kind = item.get("kind") or "ask"
    text = item.get("question") or item.get("request") or ""
    lines = [
        "",
        f"--- {'PERMISSION REQUEST' if kind == 'permission' else 'QUESTION'} "
        f"[{item.get('id', '?')}] ---",
        f"  task:    {item.get('task_id', '?')}"
        + (f"  ·  project: {item['project']}" if item.get("project") else ""),
        f"  agent:   {item.get('agent') or '?'}",
        f"  {'request' if kind == 'permission' else 'question'}: {text}",
    ]
    options = item.get("options") or []
    for n, opt in enumerate(options, 1):
        lines.append(f"    [{n}] {opt}")
    if kind == "permission":
        lines.append(f"  answer:  {_PERMISSION_HELP}")
    elif options:
        lines.append("  answer:  a number above, or free text")
    return "\n".join(lines)


def build_note(item: dict, raw: str) -> str:
    """Map terminal input to the exact steer-note grammar the server resolves."""
    rid = item.get("id") or ""
    raw = (raw or "").strip()
    if (item.get("kind") or "ask") == "permission":
        request_text = item.get("request") or item.get("question") or ""
        low = raw.lower()
        upper = raw.upper()
        if low in ("y", "yes", "allow"):
            body = f"ALLOW FOR THIS RUN: {request_text}"
        elif low in ("once", "o"):
            body = f"ALLOW ONCE: {request_text}"
        elif low in ("n", "no", "deny"):
            body = f"DENIED: {request_text}"
        elif upper.startswith(("ALLOW ONCE", "ALLOW FOR THIS RUN", "DENIED")):
            body = raw  # already speaks the grammar — pass through
        else:  # unrecognized input must never grant — deny, carrying the reason
            body = f"DENIED: {raw}" if raw else f"DENIED: {request_text}"
        return f"[permission {rid}] {body}"
    options = item.get("options") or []
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        raw = str(options[int(raw) - 1])
    return f"[answer {rid}] {raw}"


# ---- main loop -----------------------------------------------------------------
def run_attend(base_url: str = DEFAULT_URL, token: str | None = None, *,
               once: bool = False, poll_seconds: float = POLL_SECONDS,
               input_fn: Callable[[str], str] | None = None,
               print_fn: Callable[[str], None] = print,
               sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """Poll → render → answer until interrupted (or one pass with ``once``).

    Returns an exit code: 0 normally; 1 when ``once`` cannot reach the server.
    """
    if input_fn is None:
        input_fn = input  # resolved at call time (testable via builtins.input)
    client = AttendClient(base_url, token)
    seen: set[tuple[str, str]] = set()
    backoff = poll_seconds
    first = True
    while True:
        try:
            items = client.fetch_attention()
        except urllib.error.HTTPError as exc:
            print_fn(f"Server error from {client.base_url}: HTTP {exc.code}"
                     + (" — is ADA_API_TOKEN set/correct?" if exc.code in (401, 403)
                        else ""))
            if once:
                return 1
            sleep_fn(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
            continue
        except (urllib.error.URLError, OSError, ValueError):
            print_fn(f"Server unreachable at {client.base_url} — "
                     f"retrying in {backoff:.0f}s (Ctrl-C to quit)")
            if once:
                return 1
            try:
                sleep_fn(backoff)
            except KeyboardInterrupt:
                print_fn("\nStopped.")
                return 0
            backoff = min(backoff * 2, _MAX_BACKOFF)
            continue
        backoff = poll_seconds  # reachable again — reset

        fresh = [i for i in items
                 if (str(i.get("task_id") or ""), str(i.get("id") or "")) not in seen]
        if first and not fresh:
            print_fn("No attention requests pending."
                     + ("" if once else " Watching…"))
        first = False
        for item in fresh:
            seen.add((str(item.get("task_id") or ""), str(item.get("id") or "")))
            print_fn(format_item(item))
            try:
                raw = input_fn("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print_fn("\nStopped.")
                return 0
            if not raw:
                print_fn("  skipped (rerun `ada attend` to answer later)")
                continue
            note = build_note(item, raw)
            try:
                client.steer(str(item.get("task_id") or ""), note)
                print_fn("  sent.")
            except urllib.error.HTTPError as exc:
                print_fn(f"  could not deliver (HTTP {exc.code}) — "
                         "the run may have already finished")
            except (urllib.error.URLError, OSError) as exc:
                print_fn(f"  could not deliver: {exc}")
        if once:
            return 0
        try:
            sleep_fn(poll_seconds)
        except KeyboardInterrupt:
            print_fn("\nStopped.")
            return 0
