"""Extract and validate JSON from free-form model text.

The Claude Agent SDK returns plain assistant text (no native structured-output mode),
so for structured calls we instruct the model to emit JSON and recover it here, even if
it's wrapped in prose or a ```json fence.
"""

from __future__ import annotations

import json
from typing import Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def extract_json(text: str) -> str:
    """Return the first balanced JSON object/array found in ``text``."""
    s = text.strip()
    # Strip a leading ```json / ``` fence if present.
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()

    start = _first_open(s)
    if start is None:
        return s  # let json.loads raise a clear error
    return _balanced_slice(s, start)


def _first_open(s: str) -> int | None:
    for i, ch in enumerate(s):
        if ch in "{[":
            return i
    return None


def _balanced_slice(s: str, start: int) -> str:
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return s[start:]  # unbalanced; let the parser surface the error


def parse_model(text: str, schema: Type[T]) -> T:
    payload = extract_json(text)
    data = json.loads(payload)
    return schema.model_validate(data)
