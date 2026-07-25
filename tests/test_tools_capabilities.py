"""Tests for the T3/T4/T5 capability tools: symbols, find_references, delegate, web_fetch."""

import asyncio
import email.message
import urllib.error
from pathlib import Path

from ai_dev_assistant.tools import registry as registry_mod
from ai_dev_assistant.tools.registry import ToolBox, ToolContext


def make_box(base: Path, **overrides) -> ToolBox:
    # These tools never touch memory/kb/kg/bus, so stubs are fine here.
    ctx = ToolContext(memory=None, kb=None, kg=None, bus=None, agent_name="tester",
                      task_scope="t", base_dir=base, workspace=base, **overrides)
    return ToolBox(ctx)


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _code_fixture(ws: Path) -> None:
    (ws / "alpha.py").write_text(
        "import beta\n\n\nclass Widget:\n    pass\n\n\ndef make_widget():\n"
        "    return Widget()\n")
    (ws / "beta.py").write_text("def helper():\n    return 1\n\n\nhelpers = 2\n")
    (ws / "app.js").write_text("export function renderWidget() {}\nconst go = () => 1;\n")


# ---- symbols (T3) ----

def test_symbols_single_python_file(tmp_path):
    ws = _ws(tmp_path)
    _code_fixture(ws)
    out = make_box(ws).dispatch("symbols", {"path": "alpha.py"})
    assert "class Widget" in out
    assert "function make_widget" in out
    assert "imports: beta" in out
    assert "<untrusted" in out


def test_symbols_single_js_file(tmp_path):
    ws = _ws(tmp_path)
    _code_fixture(ws)
    out = make_box(ws).dispatch("symbols", {"path": "app.js"})
    assert "function renderWidget" in out and "function go" in out


def test_symbols_whole_workspace_index_ranked(tmp_path):
    ws = _ws(tmp_path)
    _code_fixture(ws)
    out = make_box(ws).dispatch("symbols", {})
    # All source files appear; beta.py is imported by alpha.py so it ranks first.
    for name in ("alpha.py", "beta.py", "app.js"):
        assert name in out, name
    assert out.index("beta.py") < out.index("alpha.py")
    assert "[imported by 1]" in out
    assert "class Widget" in out and "function helper" in out
    assert "<untrusted" in out
    assert len(out) < registry_mod._MAX_FILE_CHARS + 500  # bounded


def test_symbols_respects_workspace_boundary_and_denylist(tmp_path):
    ws = _ws(tmp_path)
    (tmp_path / "outside.py").write_text("def secret_fn():\n    pass\n")
    (ws / ".env").write_text("API_KEY=hunter2hunter2")
    box = make_box(ws)
    assert box.dispatch("symbols", {"path": "../outside.py"}).startswith("DENIED")
    assert box.dispatch("symbols", {"path": ".env"}).startswith("DENIED")


# ---- find_references (T3) ----

def test_find_references_defs_and_uses_across_files(tmp_path):
    ws = _ws(tmp_path)
    _code_fixture(ws)
    (ws / "gamma.py").write_text("from alpha import Widget\n\nw = Widget()\n")
    out = make_box(ws).dispatch("find_references", {"name": "Widget"})
    assert "alpha.py" in out and "gamma.py" in out
    assert "class Widget" in out          # definition site
    assert "w = Widget()" in out          # usage site
    assert "<untrusted" in out


def test_find_references_word_boundary(tmp_path):
    ws = _ws(tmp_path)
    _code_fixture(ws)
    out = make_box(ws).dispatch("find_references", {"name": "helper"})
    assert "def helper" in out
    assert "helpers = 2" not in out       # \bhelper\b must not match 'helpers'


def test_find_references_output_is_bounded(tmp_path):
    ws = _ws(tmp_path)
    (ws / "big.py").write_text("\n".join(
        f"needle = needle + {i}  # {'pad' * 20}" for i in range(2000)))
    out = make_box(ws).dispatch("find_references", {"name": "needle"})
    assert len(out) <= registry_mod._MAX_GREP_CHARS + 500


def test_find_references_rejects_non_identifier(tmp_path):
    ws = _ws(tmp_path)
    out = make_box(ws).dispatch("find_references", {"name": "a b; rm"})
    assert out.startswith("ERROR")


# ---- delegate (T4) ----

def test_delegate_unavailable_without_spawn(tmp_path):
    ws = _ws(tmp_path)
    out = make_box(ws).dispatch("delegate", {"agent": "coder", "task": "do a thing"})
    assert "delegation unavailable" in out


def test_delegate_invokes_spawn_through_dispatch(tmp_path):
    ws = _ws(tmp_path)
    calls = []

    async def fake_spawn(agent: str, task: str) -> str:
        calls.append((agent, task))
        return f"done by {agent}"

    out = make_box(ws, spawn=fake_spawn).dispatch(
        "delegate", {"agent": "coder", "task": "build it"})
    assert out == "done by coder"
    assert calls == [("coder", "build it")]


def test_delegate_works_from_a_running_event_loop(tmp_path):
    # Both providers call dispatch() synchronously from the event-loop thread; the
    # handler must still resolve the spawn coroutine without deadlocking.
    ws = _ws(tmp_path)

    async def fake_spawn(agent: str, task: str) -> str:
        await asyncio.sleep(0)
        return "ok:" + agent

    box = make_box(ws, spawn=fake_spawn)

    async def driver() -> str:
        return box.dispatch("delegate", {"agent": "tester2", "task": "x"})

    assert asyncio.run(driver()) == "ok:tester2"


def test_delegate_caps_task_length(tmp_path):
    ws = _ws(tmp_path)
    seen = {}

    async def fake_spawn(agent: str, task: str) -> str:
        seen["len"] = len(task)
        return "ok"

    make_box(ws, spawn=fake_spawn).dispatch("delegate", {"agent": "a", "task": "x" * 10_000})
    assert seen["len"] == registry_mod._MAX_DELEGATE_TASK_CHARS


def test_delegate_requires_agent_and_task(tmp_path):
    ws = _ws(tmp_path)
    box = make_box(ws)
    assert box.dispatch("delegate", {"agent": "coder"}).startswith("ERROR")
    assert box.dispatch("delegate", {"task": "t"}).startswith("ERROR")


# ---- web_fetch (T5) ----

class _FakeResp:
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def read(self, n: int = -1) -> bytes:
        return (b"<html><head><script>evil()</script></head>"
                b"<body><h1>Hello &amp; welcome</h1></body></html>")

    def close(self) -> None:
        pass


def test_web_fetch_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ADA_ALLOW_WEB", raising=False)
    ws = _ws(tmp_path)
    out = make_box(ws).dispatch("web_fetch", {"url": "https://example.com/"})
    assert "web access disabled" in out and "ADA_ALLOW_WEB" in out


def test_web_fetch_rejects_non_http_schemes(tmp_path):
    ws = _ws(tmp_path)
    box = make_box(ws, allow_web=True)
    for url in ("ftp://example.com/file", "file:///etc/passwd", "gopher://x"):
        out = box.dispatch("web_fetch", {"url": url})
        assert out.startswith("ERROR") and "http/https" in out, url


def test_web_fetch_rejects_private_loopback_and_metadata_hosts(tmp_path):
    ws = _ws(tmp_path)
    box = make_box(ws, allow_web=True)
    for url in ("http://169.254.169.254/latest/meta-data/",   # cloud metadata (link-local)
                "http://127.0.0.1/", "http://10.0.0.8/x",
                "http://192.168.1.10/", "http://172.16.0.1/", "http://[::1]/"):
        out = box.dispatch("web_fetch", {"url": url})
        assert out.startswith("ERROR") and "refusing" in out, url


def test_web_fetch_rejects_hosts_resolving_to_private_addresses(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    # A benign-looking name whose DNS answer includes a private address (rebinding-style).
    monkeypatch.setattr(registry_mod, "_resolve_addrs",
                        lambda host: ["93.184.216.34", "10.0.0.5"])
    out = make_box(ws, allow_web=True).dispatch("web_fetch", {"url": "http://example.com/"})
    assert out.startswith("ERROR") and "refusing" in out


def test_web_fetch_public_url_stripped_and_untrusted(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setattr(registry_mod, "_resolve_addrs", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(registry_mod, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResp())
    out = make_box(ws, allow_web=True).dispatch(
        "web_fetch", {"url": "https://example.com/page"})
    assert "<untrusted" in out and 'source="https://example.com/page"' in out
    assert "Hello & welcome" in out        # entity unescaped
    assert "<h1>" not in out and "evil()" not in out  # tags/scripts stripped


def test_web_fetch_env_fallback_enables(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setenv("ADA_ALLOW_WEB", "true")
    monkeypatch.setattr(registry_mod, "_resolve_addrs", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(registry_mod, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResp())
    out = make_box(ws).dispatch("web_fetch", {"url": "https://example.com/"})  # ctx False
    assert "Hello" in out


def test_web_fetch_does_not_follow_redirects(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    hdrs = email.message.Message()
    hdrs["Location"] = "http://127.0.0.1/steal"

    def raise_redirect(url, timeout):
        raise urllib.error.HTTPError(url, 302, "Found", hdrs, None)

    monkeypatch.setattr(registry_mod, "_resolve_addrs", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(registry_mod, "_urlopen_no_redirect", raise_redirect)
    out = make_box(ws, allow_web=True).dispatch("web_fetch", {"url": "http://example.com/"})
    assert "not followed" in out and "http://127.0.0.1/steal" in out
