"""Held-out tests for repo_a — never visible to the agent during the run (E3)."""

from logutil import format_lines, format_log


def test_prefix_and_message_survive():
    assert format_log("info", "ok") == "[INFO] ok"


def test_mixed_case_level_is_normalized():
    assert format_log("Warning", "x") == "[WARNING] x"


def test_format_lines_prefixes_every_line():
    assert format_lines("info", ["a", "b"]) == ["[INFO] a", "[INFO] b"]
