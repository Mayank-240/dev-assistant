"""Deterministic graders — objective ground truth for the eval harness.

No LLM: these inspect the produced workspace and return pass/fail with a reason. They are
the one objective signal the eval/learning loop can trust.
"""

from __future__ import annotations

import ast
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..execution import run_workspace_tests_sync

# Resource headroom for grader-run test commands. The sandbox default of 1 GiB
# RLIMIT_AS breaks Go/Rust toolchains (their runtimes reserve large address
# spaces), and compiled fixtures need real CPU time for a cold build.
_GRADER_CPU_S = 180
_GRADER_MEM_MB = 4096


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
    res = run_workspace_tests_sync(workspace, timeout,
                                   cpu_seconds=_GRADER_CPU_S, mem_mb=_GRADER_MEM_MB)
    if res is None:
        return GraderResult("tests_pass", False, "no runnable tests detected")
    return GraderResult("tests_pass", res.passed, f"exit={res.return_code} {res.command}")


def _merge_tree(src: Path, dst: Path) -> list[str]:
    """Copy every file under ``src`` into ``dst`` preserving relative paths."""
    rels: list[str] = []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        rels.append(rel.as_posix())
    return rels


def heldout_tests_pass(workspace: Path, heldout_dir: Path, timeout: float = 120.0) -> GraderResult:
    """Non-circular grading (E3): copy held-out tests — which the agent NEVER saw during
    the run — into the finished workspace and run them. Defeats the 'model writes both
    the code and the tests it is graded on' loophole of ``tests_pass``.

    Two modes, chosen by the held-out dir's contents:

    - Python (every file is ``.py``): historical behavior — the held-out test files are
      copied to the workspace root and pytest runs ONLY them (path-narrowed).
    - Any other language: there is no portable per-file selection across runners, so the
      whole held-out tree is merged into the workspace (relative paths preserved — e.g.
      ``tests/heldout.rs`` lands where cargo expects it) and the project's own detected
      test command (``npm test``, ``go test ./...``, ``cargo test``, ``rake test``, …)
      runs over the merged workspace. The in-repo tests necessarily run too, which is
      fine: they must also pass on a correct fix, and the held-out cases still gate.
    """
    heldout_dir = Path(heldout_dir)
    if not heldout_dir.is_dir():
        return GraderResult("heldout_tests_pass", False, f"no held-out dir: {heldout_dir}")
    files = [p for p in sorted(heldout_dir.rglob("*")) if p.is_file()]
    if not files:
        return GraderResult("heldout_tests_pass", False, "held-out dir has no test files")
    if all(p.suffix == ".py" for p in files):
        rels: list[str] = []
        for p in files:
            name = p.name
            if not (name.startswith("test_") or name.endswith("_test.py")):
                continue
            shutil.copy2(p, workspace / name)
            rels.append(name)
        if not rels:
            return GraderResult("heldout_tests_pass", False, "held-out dir has no test files")
        res = run_workspace_tests_sync(workspace, timeout, paths=rels,
                                       cpu_seconds=_GRADER_CPU_S, mem_mb=_GRADER_MEM_MB)
    else:
        _merge_tree(heldout_dir, workspace)
        res = run_workspace_tests_sync(workspace, timeout,
                                       cpu_seconds=_GRADER_CPU_S, mem_mb=_GRADER_MEM_MB)
    if res is None:
        return GraderResult("heldout_tests_pass", False, "no runnable tests detected")
    return GraderResult("heldout_tests_pass", res.passed,
                        f"exit={res.return_code} {res.command}")
