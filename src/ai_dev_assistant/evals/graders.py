"""Deterministic graders — objective ground truth for the eval harness.

No LLM: these inspect the produced workspace and return pass/fail with a reason. They are
the one objective signal the eval/learning loop can trust.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from ..execution import run_workspace_tests_sync


@dataclass
class GraderResult:
    name: str
    passed: bool
    detail: str = ""


def file_exists(workspace: Path, pattern: str) -> GraderResult:
    hits = list(workspace.rglob(pattern))
    return GraderResult(f"file_exists:{pattern}", bool(hits),
                        ", ".join(str(h.relative_to(workspace)) for h in hits[:3]) or "none found")


def ast_defines(workspace: Path, symbol: str) -> GraderResult:
    """True if any .py file in the workspace defines a function/class named ``symbol``."""
    for p in workspace.rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
                return GraderResult(f"ast_defines:{symbol}", True, str(p.relative_to(workspace)))
    return GraderResult(f"ast_defines:{symbol}", False, "not defined")


def tests_pass(workspace: Path, timeout: float = 120.0) -> GraderResult:
    res = run_workspace_tests_sync(workspace, timeout)
    if res is None:
        return GraderResult("tests_pass", False, "no runnable tests detected")
    return GraderResult("tests_pass", res.passed, f"exit={res.return_code} {res.command}")
