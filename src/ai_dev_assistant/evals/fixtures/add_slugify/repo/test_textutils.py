"""In-repo tests for the existing helpers (pass at baseline)."""

from textutils import normalize_whitespace, truncate


def test_normalize_whitespace():
    assert normalize_whitespace("  a \t b\n\nc ") == "a b c"


def test_truncate_short():
    assert truncate("abc", 10) == "abc"


def test_truncate_cuts():
    assert truncate("abcdefgh", 5) == "abcd…"
