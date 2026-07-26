"""Tests for attend.py — the terminal attention client. A real (threaded)
http.server stands in for the web server: it serves /api/home and records
steer POSTs, so the exact note grammar the client emits is asserted end-to-end.
All offline (loopback sockets only)."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ai_dev_assistant import cli
from ai_dev_assistant.attend import build_note, format_item, run_attend


class _Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        required = self.server.state.get("require_token")
        if not required:
            return True
        return self.headers.get("Authorization") == f"Bearer {required}"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/api/home":
            self.server.state["polls"] += 1
            return self._send(200, {"attention": self.server.state["attention"]})
        return self._send(404, {"error": "nope"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.state["steers"].append((self.path, body))
        return self._send(200, {"ok": True})

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.state = {"attention": [], "steers": [], "polls": 0, "require_token": None}
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield url, httpd.state
    httpd.shutdown()
    httpd.server_close()


def _ask(rid="ask-1", task="t1", **kw):
    item = {"task_id": task, "project": "acme", "kind": "ask", "id": rid,
            "agent": "architect", "question": "Postgres or SQLite?",
            "options": ["Use Postgres", "Use SQLite"]}
    item.update(kw)
    return item


def _perm(rid="permission-1", task="t1", **kw):
    item = {"task_id": task, "project": "acme", "kind": "permission", "id": rid,
            "agent": "devops", "request": "delete the old migrations", "options": []}
    item.update(kw)
    return item


# ---- rendering + answering through the fake server ----
def test_once_renders_item_and_posts_numbered_answer(server):
    url, state = server
    state["attention"] = [_ask()]
    lines: list[str] = []
    rc = run_attend(url, once=True, input_fn=lambda _: "2",
                    print_fn=lines.append)
    assert rc == 0
    text = "\n".join(lines)
    assert "architect" in text and "acme" in text and "Postgres or SQLite?" in text
    assert "[1] Use Postgres" in text and "[2] Use SQLite" in text
    assert state["steers"] == [
        ("/api/run/t1/steer", {"note": "[answer ask-1] Use SQLite"})]


def test_free_text_answer_posts_verbatim(server):
    url, state = server
    state["attention"] = [_ask()]
    run_attend(url, once=True, input_fn=lambda _: "start with SQLite, plan a migration",
               print_fn=lambda s: None)
    assert state["steers"][0][1]["note"] == (
        "[answer ask-1] start with SQLite, plan a migration")


def test_permission_shortcuts_map_to_grammar(server):
    url, state = server
    state["attention"] = [_perm("permission-1"), _perm("permission-2"),
                          _perm("permission-3")]
    answers = iter(["y", "once", "n"])
    run_attend(url, once=True, input_fn=lambda _: next(answers),
               print_fn=lambda s: None)
    notes = [body["note"] for _, body in state["steers"]]
    assert notes == [
        "[permission permission-1] ALLOW FOR THIS RUN: delete the old migrations",
        "[permission permission-2] ALLOW ONCE: delete the old migrations",
        "[permission permission-3] DENIED: delete the old migrations",
    ]


def test_permission_free_text_denies_with_reason(server):
    url, state = server
    state["attention"] = [_perm()]
    run_attend(url, once=True, input_fn=lambda _: "keep them until the release ships",
               print_fn=lambda s: None)
    assert state["steers"][0][1]["note"] == (
        "[permission permission-1] DENIED: keep them until the release ships")


def test_once_with_no_items_exits_zero(server):
    url, state = server
    lines: list[str] = []
    rc = run_attend(url, once=True,
                    input_fn=lambda _: pytest.fail("should not prompt"),
                    print_fn=lines.append)
    assert rc == 0
    assert any("No attention requests pending" in ln for ln in lines)
    assert state["steers"] == []


def test_auth_token_sent_and_401_reported(server):
    url, state = server
    state["require_token"] = "sekrit"
    state["attention"] = [_ask()]
    lines: list[str] = []
    assert run_attend(url, token=None, once=True,
                      input_fn=lambda _: "1", print_fn=lines.append) == 1
    assert any("HTTP 401" in ln and "ADA_API_TOKEN" in ln for ln in lines)
    assert run_attend(url, token="sekrit", once=True,
                      input_fn=lambda _: "1", print_fn=lambda s: None) == 0
    assert state["steers"][0][1]["note"] == "[answer ask-1] Use Postgres"


def test_unreachable_once_exits_nonzero():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    lines: list[str] = []
    rc = run_attend(f"http://127.0.0.1:{port}", once=True,
                    input_fn=lambda _: "x", print_fn=lines.append)
    assert rc == 1
    assert any("unreachable" in ln for ln in lines)


def test_unreachable_loop_retries_with_backoff_and_ctrl_c_exits():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    lines: list[str] = []
    waits: list[float] = []

    def sleep_fn(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 2:
            raise KeyboardInterrupt  # operator hits Ctrl-C mid-retry

    rc = run_attend(f"http://127.0.0.1:{port}", once=False, poll_seconds=5.0,
                    input_fn=lambda _: "x", print_fn=lines.append, sleep_fn=sleep_fn)
    assert rc == 0  # clean exit
    assert waits == [5.0, 10.0]  # backoff doubled after the first failure
    assert sum("retrying" in ln for ln in lines) >= 2


def test_loop_does_not_reprompt_seen_items(server):
    url, state = server
    state["attention"] = [_ask()]
    prompts: list[str] = []

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        return "1"

    sleeps = iter([None, None])

    def sleep_fn(seconds: float) -> None:
        try:
            next(sleeps)
        except StopIteration:
            raise KeyboardInterrupt from None

    rc = run_attend(url, once=False, poll_seconds=0.0, input_fn=input_fn,
                    print_fn=lambda s: None, sleep_fn=sleep_fn)
    assert rc == 0
    assert state["polls"] >= 2  # the same open item came back on later polls…
    assert len(prompts) == 1  # …but the operator was only asked once
    assert len(state["steers"]) == 1


# ---- note grammar / rendering units ----
def test_build_note_passthrough_and_deny_default():
    perm = _perm()
    assert build_note(perm, "ALLOW ONCE: with a tarball first") == (
        "[permission permission-1] ALLOW ONCE: with a tarball first")
    assert build_note(perm, "") == (
        "[permission permission-1] DENIED: delete the old migrations")
    ask = _ask(options=[])
    assert build_note(ask, "7") == "[answer ask-1] 7"  # no options: digits are text


def test_format_item_permission_shows_shortcut_help():
    text = format_item(_perm())
    assert "PERMISSION REQUEST" in text and "delete the old migrations" in text
    assert "[y]=allow for this run" in text


# ---- CLI ----
def test_cli_attend_parsing():
    p = cli._build_parser()
    a = p.parse_args(["attend"])
    assert (a.cmd, a.url, a.token, a.interval, a.once) == (
        "attend", "http://127.0.0.1:8000", None, 5.0, False)
    a = p.parse_args(["attend", "--url", "http://box:9", "--token", "tk",
                      "--interval", "2.5", "--once"])
    assert (a.url, a.token, a.interval, a.once) == ("http://box:9", "tk", 2.5, True)


def test_cli_attend_once_against_server(server, monkeypatch, capsys):
    url, state = server
    state["require_token"] = "env-token"
    state["attention"] = [_ask()]
    monkeypatch.setenv("ADA_API_TOKEN", "env-token")  # picked up without --token
    monkeypatch.setattr("builtins.input", lambda _="": "2")
    assert cli.main(["attend", "--once", "--url", url]) == 0
    assert "Postgres or SQLite?" in capsys.readouterr().out
    assert state["steers"][0][1]["note"] == "[answer ask-1] Use SQLite"