"""check_builtin_tool_use confines the SDK's built-in file tools to the run workspace,
and budget_gate starves an over-budget run of further tool calls.

Pure unit tests: the checker is exercised directly, without the claude-agent-sdk runtime
(the provider module imports the SDK lazily, so importing the checker never needs it).
The hook-level tests stub the SDK's HookMatcher so no runtime is needed there either.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from ai_dev_assistant.llm.claude_sdk_provider import (
    ClaudeSdkProvider,
    budget_gate,
    check_builtin_tool_use,
)
from ai_dev_assistant.llm.usage import UsageTotals


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_read_inside_workspace_allowed(tmp_path):
    ws = _ws(tmp_path)
    (ws / "notes.txt").write_text("hi")
    ok, reason = check_builtin_tool_use("Read", {"file_path": str(ws / "notes.txt")}, ws)
    assert ok, reason


def test_relative_path_inside_workspace_allowed(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use("Write", {"file_path": "sub/new_file.py"}, ws)
    assert ok, reason


def test_read_absolute_path_outside_denied(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use("Read", {"file_path": "/etc/passwd"}, ws)
    assert not ok
    assert "outside" in reason


def test_dotdot_escape_denied(tmp_path):
    ws = _ws(tmp_path)
    (tmp_path / "secret.txt").write_text("s")
    ok, reason = check_builtin_tool_use("Read", {"file_path": "../secret.txt"}, ws)
    assert not ok
    assert "outside" in reason


def test_symlink_pointing_outside_denied(tmp_path):
    ws = _ws(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("s")
    (ws / "innocent.txt").symlink_to(outside)
    ok, reason = check_builtin_tool_use("Read", {"file_path": str(ws / "innocent.txt")}, ws)
    assert not ok
    assert "outside" in reason


def test_workspace_dotenv_denied(tmp_path):
    ws = _ws(tmp_path)
    (ws / ".env").write_text("API_KEY=x")
    ok, reason = check_builtin_tool_use("Read", {"file_path": str(ws / ".env")}, ws)
    assert not ok
    assert "denylist" in reason


def test_workspace_id_rsa_denied(tmp_path):
    ws = _ws(tmp_path)
    (ws / "id_rsa").write_text("key")
    ok, reason = check_builtin_tool_use("Read", {"file_path": "id_rsa"}, ws)
    assert not ok
    assert "denylist" in reason


def test_write_outside_denied(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use(
        "Write", {"file_path": str(tmp_path / "elsewhere.txt"), "content": "x"}, ws
    )
    assert not ok
    assert "outside" in reason


def test_glob_with_path_outside_denied(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use("Glob", {"pattern": "**/*.py", "path": "/etc"}, ws)
    assert not ok
    assert "outside" in reason


def test_glob_absolute_pattern_outside_denied(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use("Glob", {"pattern": "/etc/**"}, ws)
    assert not ok
    assert "outside" in reason


def test_glob_relative_wildcard_pattern_allowed(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use("Glob", {"pattern": "**/*.py", "path": str(ws)}, ws)
    assert ok, reason


def test_grep_workspace_allowed(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use(
        "Grep", {"pattern": "TODO|FIXME", "path": str(ws), "glob": "*.py"}, ws
    )
    assert ok, reason


def test_secret_dir_inside_workspace_denied(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use("Read", {"file_path": ".ssh/known_hosts"}, ws)
    assert not ok
    assert "denylist" in reason


def test_non_file_tools_pass_through(tmp_path):
    ws = _ws(tmp_path)
    for name, inp in (
        ("TodoWrite", {"todos": [{"content": "x", "status": "pending"}]}),
        ("WebSearch", {"query": "/etc/passwd"}),
        ("mcp__ada__read_file", {"path": "/etc/passwd"}),  # MCP tools enforce their own sandbox
    ):
        ok, reason = check_builtin_tool_use(name, inp, ws)
        assert ok, (name, reason)


def test_missing_or_non_string_args_pass_through(tmp_path):
    ws = _ws(tmp_path)
    ok, reason = check_builtin_tool_use("Grep", {"pattern": "x"}, ws)  # no path arg: cwd-scoped
    assert ok, reason
    ok, reason = check_builtin_tool_use("Read", {"file_path": None}, ws)
    assert ok, reason


# --- budget_gate: pure decision -------------------------------------------------------


def test_budget_gate_no_cap_allows():
    ok, reason = budget_gate(123.45, None)
    assert ok and reason == ""


def test_budget_gate_under_budget_allows():
    ok, reason = budget_gate(0.04, 0.05)
    assert ok and reason == ""


def test_budget_gate_at_cap_denies():
    ok, reason = budget_gate(0.05, 0.05)
    assert not ok
    assert "budget exhausted" in reason
    assert "summary" in reason


def test_budget_gate_over_cap_denies():
    ok, reason = budget_gate(0.10, 0.05)
    assert not ok
    assert "budget exhausted" in reason


# --- budget hook wiring (stubbed HookMatcher; no SDK runtime) -------------------------


class _StubMatcher:
    def __init__(self, matcher=None, hooks=None, timeout=None):
        self.matcher = matcher
        self.hooks = hooks or []


def _provider(spent_usd: float) -> ClaudeSdkProvider:
    p = ClaudeSdkProvider.__new__(ClaudeSdkProvider)  # skip __init__: no SDK import
    p._sdk = SimpleNamespace(HookMatcher=_StubMatcher)
    p.usage = UsageTotals()
    p.usage.add(cost_usd=spent_usd)
    return p


def _budget_hook(provider: ClaudeSdkProvider, ws: Path, cap: float | None):
    matchers = provider._confinement_hooks(str(ws), max_cost_usd=cap)["PreToolUse"]
    catch_all = [m for m in matchers if m.matcher is None]
    return catch_all[0].hooks[0] if catch_all else None


def _run_hook(hook, tool_name: str, tool_input: dict) -> dict:
    return asyncio.run(hook({"tool_name": tool_name, "tool_input": tool_input}, None, None))


def test_budget_hook_absent_without_cap(tmp_path):
    matchers = _provider(1.0)._confinement_hooks(str(_ws(tmp_path)))["PreToolUse"]
    assert len(matchers) == 1  # confinement only; no catch-all budget matcher
    assert matchers[0].matcher is not None


def test_budget_hook_allows_under_budget(tmp_path):
    hook = _budget_hook(_provider(0.01), _ws(tmp_path), cap=0.05)
    assert _run_hook(hook, "mcp__ada__read_file", {"path": "x.py"}) == {}


def test_budget_hook_denies_over_budget_for_all_tools(tmp_path):
    hook = _budget_hook(_provider(0.10), _ws(tmp_path), cap=0.05)
    for name, inp in (
        ("mcp__ada__read_file", {"path": "x.py"}),  # MCP toolbox tool
        ("Read", {"file_path": "notes.txt"}),  # built-in, in-workspace
        ("TodoWrite", {"todos": []}),  # built-in without path args
    ):
        out = _run_hook(hook, name, inp)
        spec = out["hookSpecificOutput"]
        assert spec["permissionDecision"] == "deny", name
        assert "budget exhausted" in spec["permissionDecisionReason"], name


def test_budget_hook_rechecks_live_usage(tmp_path):
    provider = _provider(0.0)
    hook = _budget_hook(provider, _ws(tmp_path), cap=0.05)
    assert _run_hook(hook, "Read", {"file_path": "a.txt"}) == {}
    provider.usage.add(cost_usd=0.06)  # _record_usage books another result message
    out = _run_hook(hook, "Read", {"file_path": "a.txt"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_confinement_hook_unchanged_under_budget(tmp_path):
    ws = _ws(tmp_path)
    matchers = _provider(0.0)._confinement_hooks(str(ws), max_cost_usd=0.05)["PreToolUse"]
    gate = [m for m in matchers if m.matcher is not None][0].hooks[0]
    assert _run_hook(gate, "Read", {"file_path": "notes.txt"}) == {}
    out = _run_hook(gate, "Read", {"file_path": "/etc/passwd"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "outside" in out["hookSpecificOutput"]["permissionDecisionReason"]
