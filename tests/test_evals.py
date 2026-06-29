"""Deterministic graders for the eval harness (no LLM)."""

from __future__ import annotations

from ai_dev_assistant.evals.graders import ast_defines, file_exists
from ai_dev_assistant.evals.graders import tests_pass as grade_tests  # avoid pytest collecting it


def test_graders(tmp_path):
    (tmp_path / "reverse_string.py").write_text("def reverse_string(s):\n    return s[::-1]\n")
    (tmp_path / "test_reverse_string.py").write_text(
        "from reverse_string import reverse_string\n"
        "def test_basic():\n    assert reverse_string('abc') == 'cba'\n"
    )
    assert file_exists(tmp_path, "reverse_string.py").passed
    assert ast_defines(tmp_path, "reverse_string").passed
    assert not ast_defines(tmp_path, "nonexistent").passed
    assert grade_tests(tmp_path, timeout=60).passed
