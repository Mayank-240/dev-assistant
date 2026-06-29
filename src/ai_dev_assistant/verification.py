"""Objective signals for the reviewer (Tier 3): ground a verdict in reality, not just the
agent's self-reported result text.

Gathers, for a subtask, the actual file contents the run produced, the result of running
the workspace's tests, and a lint pass — then post-processes the LLM Verdict so that
objective failures hard-fail and purely-stylistic LLM failures (with green tests) are
downgraded to a soft success instead of cascading the DAG.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .execution import ExecutionResult, run_command_sync, run_workspace_tests_sync
from .llm.schemas import Verdict

logger = logging.getLogger("ada.verify")

_MAX_FILE = 4000
_MAX_TOTAL = 16000


def collect_file_contents(workspace: Path, files: list[str]) -> str:
    """Bounded dump of the actual produced source for the reviewer to read (not just names)."""
    if not workspace.is_dir() or not files:
        return ""
    out: list[str] = []
    total = 0
    for rel in files:
        p = (workspace / rel)
        if not p.is_file() or p.suffix in (".pyc",):
            continue
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        snippet = text[:_MAX_FILE] + ("\n…(truncated)" if len(text) > _MAX_FILE else "")
        block = f"--- {rel} ---\n{snippet}"
        total += len(block)
        if total > _MAX_TOTAL:
            out.append("…(remaining files omitted)")
            break
        out.append(block)
    return "\n\n".join(out)


def lint_workspace(workspace: Path, timeout: float = 30.0) -> ExecutionResult | None:
    """Run ruff over the workspace if available; None if there's nothing to lint / ruff absent."""
    if not workspace.is_dir() or not any(workspace.rglob("*.py")):
        return None
    if shutil.which("ruff") is None:
        # fall back to pyflakes-via-compile: a syntax check is better than nothing
        import sys
        return run_command_sync([sys.executable, "-m", "compileall", "-q", "."], workspace, timeout)
    return run_command_sync(["ruff", "check", "."], workspace, timeout)


def gather_signals(workspace: Path, files: list[str], *, run_tests: bool, lint: bool,
                   timeout: float) -> dict:
    signals: dict = {"files": collect_file_contents(workspace, files)}
    if run_tests:
        signals["tests"] = run_workspace_tests_sync(workspace, timeout)
    if lint:
        signals["lint"] = lint_workspace(workspace)
    return signals


def apply_objective_gate(verdict: Verdict, signals: dict) -> Verdict:
    """Fold objective signals into the LLM verdict.

    - failing tests → hard fail (passed=False), regardless of the LLM's opinion.
    - LLM-failed but tests pass → downgrade to a soft pass so it doesn't cascade BLOCKED.
    """
    tests: ExecutionResult | None = signals.get("tests")
    notes: list[str] = []
    if tests is not None:
        notes.append(f"tests: {'PASS' if tests.passed else 'FAIL'} (exit {tests.return_code})")
        if not tests.passed:
            return verdict.model_copy(update={
                "passed": False,
                "score": min(verdict.score, 20),
                "reasons": [*verdict.reasons, "Objective: the workspace tests failed."],
                "objective_note": "; ".join(notes),
            })
        if not verdict.passed:
            # green tests but the LLM nitpicked — make it a soft pass, not a cascade
            notes.append("LLM flagged issues but objective tests pass → soft pass")
            return verdict.model_copy(update={
                "passed": True,
                "score": max(verdict.score, 70),
                "reasons": [*verdict.reasons, "Objective override: tests pass; treated as a soft pass."],
                "objective_note": "; ".join(notes),
            })
    lint: ExecutionResult | None = signals.get("lint")
    if lint is not None and not lint.passed:
        notes.append("lint: issues found")
    if notes:
        return verdict.model_copy(update={"objective_note": "; ".join(notes)})
    return verdict
