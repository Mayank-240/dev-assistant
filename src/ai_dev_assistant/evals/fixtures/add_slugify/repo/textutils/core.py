"""Core text helpers."""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip the ends."""
    return _WS.sub(" ", text).strip()


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """Cut ``text`` to at most ``limit`` characters, appending ``ellipsis`` when cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))] + ellipsis
