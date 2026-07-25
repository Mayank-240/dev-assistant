"""check_builtin_tool_use confines the SDK's built-in file tools to the run workspace.

Pure unit tests: the checker is exercised directly, without the claude-agent-sdk runtime
(the provider module imports the SDK lazily, so importing the checker never needs it).
"""

from __future__ import annotations

from pathlib import Path

from ai_dev_assistant.llm.claude_sdk_provider import check_builtin_tool_use


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
