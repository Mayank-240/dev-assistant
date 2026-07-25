"""Held-out tests (E3): the agent never sees this file. Robust to whether slugify is
reached via the package root or textutils.core, per the task's export requirement."""


def _slugify():
    import textutils

    if hasattr(textutils, "slugify"):
        return textutils.slugify
    from textutils import core

    return core.slugify


def test_basic():
    assert _slugify()("Hello, World!") == "hello-world"


def test_collapses_separator_runs():
    assert _slugify()("A  --  B__C") == "a-b-c"


def test_strips_edge_hyphens():
    assert _slugify()("  ...Already Trimmed?  ") == "already-trimmed"


def test_keeps_digits():
    assert _slugify()("Version 2.0 (beta 3)") == "version-2-0-beta-3"


def test_empty_and_symbol_only():
    assert _slugify()("") == ""
    assert _slugify()("!!!") == ""


def test_existing_helpers_untouched():
    from textutils import normalize_whitespace, truncate

    assert normalize_whitespace(" x  y ") == "x y"
    assert truncate("abcdef", 4) == "abc…"
