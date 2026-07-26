"""Objective signals for the reviewer (Tier 3): ground a verdict in reality, not just the
agent's self-reported result text.

Gathers, for a subtask, the actual file contents the run produced, the result of running
tests scoped to the subtask's changes, and a lint pass — then post-processes the LLM
Verdict against a pre-run baseline so that:
- NEW test failures hard-fail (pre-existing red tests are noted, not blamed on the subtask),
- a green-tests override of an LLM failure only applies when the tests actually relate to
  the subtask's changed files (and caps, not floors, the score),
- new lint errors reduce the score instead of being decorative.

Extra signals (V5) come from a small ``SignalProvider`` registry — typecheck
(mypy/pyright/tsc), build (py_compile sweep / ``npm run build``), coverage
(pytest-cov total) — each of which degrades silently when its tool is absent.
A typecheck or build that regressed versus the baseline subtracts from the score;
it never hard-fails on its own. Per-provider runtime is bounded by
``ADA_SIGNAL_TIMEOUT`` (seconds, default 60).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from .execution import ExecutionResult, run_command_sync, run_workspace_tests_sync
from .llm.schemas import Verdict
from .static_checks import Check, detect_static_checks, run_static_checks, static_delta_note

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


def _lint_error_count(res: ExecutionResult | None) -> int:
    """Rough count of reported problems — enough to compare against a baseline."""
    if res is None or res.passed:
        return 0
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
    return max(1, len(lines))


def lint_workspace(workspace: Path, timeout: float = 30.0) -> ExecutionResult | None:
    """Run ruff over the workspace if available; None if there's nothing to lint / ruff absent."""
    if not workspace.is_dir() or not any(workspace.rglob("*.py")):
        return None
    if shutil.which("ruff") is None:
        # fall back to pyflakes-via-compile: a syntax check is better than nothing
        return run_command_sync([sys.executable, "-m", "compileall", "-q", "."], workspace, timeout)
    return run_command_sync(["ruff", "check", "."], workspace, timeout)


# ---------------------------------------------------------------------------
# SignalProvider registry (V5): pluggable objective signals beyond tests + lint.
# Every provider must degrade silently when its tool is absent — CI has none of them.
# ---------------------------------------------------------------------------


class SignalProvider:
    """One extra objective signal (typecheck, build, coverage, …).

    ``available`` decides cheaply whether the signal applies to this workspace;
    ``run`` produces an ExecutionResult (or None to skip after all). Providers must
    never raise out of ``run`` with anything the caller can't swallow — gather/baseline
    wrap calls defensively anyway.
    """

    name: str = "signal"

    def available(self, workspace: Path) -> bool:  # pragma: no cover - interface default
        return False

    def run(self, workspace: Path, changed_files: list[str],
            timeout: float) -> ExecutionResult | None:  # pragma: no cover - interface default
        return None


def _has_python(workspace: Path) -> bool:
    return any(workspace.rglob("*.py"))


class TypecheckProvider(SignalProvider):
    """mypy or pyright for Python; tsc for TypeScript-only workspaces; else skip."""

    name = "typecheck"

    def available(self, workspace: Path) -> bool:
        if not workspace.is_dir():
            return False
        if _has_python(workspace):
            return shutil.which("mypy") is not None or shutil.which("pyright") is not None
        if any(workspace.rglob("*.ts")):
            return shutil.which("tsc") is not None
        return False

    def run(self, workspace: Path, changed_files: list[str],
            timeout: float) -> ExecutionResult | None:
        if _has_python(workspace):
            targets = [f for f in changed_files if f.endswith(".py")
                       and (workspace / f).is_file()] or ["."]
            if shutil.which("mypy") is not None:
                return run_command_sync(["mypy", "--ignore-missing-imports",
                                         "--no-error-summary", *targets], workspace, timeout)
            return run_command_sync(["pyright", *targets], workspace, timeout)
        return run_command_sync(["tsc", "--noEmit"], workspace, timeout)


class BuildProvider(SignalProvider):
    """py_compile sweep for Python; ``npm run build`` when package.json defines one."""

    name = "build"

    def available(self, workspace: Path) -> bool:
        if not workspace.is_dir():
            return False
        return _has_python(workspace) or self._npm_build_script(workspace)

    @staticmethod
    def _npm_build_script(workspace: Path) -> bool:
        pkg = workspace / "package.json"
        if not pkg.is_file():
            return False
        try:
            data = json.loads(pkg.read_text())
            return isinstance(data.get("scripts"), dict) and "build" in data["scripts"]
        except Exception:
            return False

    def run(self, workspace: Path, changed_files: list[str],
            timeout: float) -> ExecutionResult | None:
        if _has_python(workspace):
            return run_command_sync([sys.executable, "-m", "compileall", "-q", "."],
                                    workspace, timeout)
        if self._npm_build_script(workspace):
            return run_command_sync(["npm", "run", "build", "--if-present", "--silent"],
                                    workspace, timeout)
        return None


_COVERAGE_TOTAL = re.compile(r"^TOTAL\s+.*?(\d+(?:\.\d+)?)%\s*$", re.MULTILINE)


class CoverageProvider(SignalProvider):
    """Total coverage of a scoped test run — only when pytest-cov is importable."""

    name = "coverage"

    def available(self, workspace: Path) -> bool:
        if not workspace.is_dir() or importlib.util.find_spec("pytest_cov") is None:
            return False
        return any(workspace.rglob("test_*.py")) or any(workspace.rglob("*_test.py"))

    def run(self, workspace: Path, changed_files: list[str],
            timeout: float) -> ExecutionResult | None:
        paths = [f for f in changed_files
                 if (workspace / f).is_file() and Path(f).name.startswith("test_")] or None
        cmd = [sys.executable, "-m", "pytest", "-q", "--cov=.", "--cov-report=term"]
        if paths:
            cmd += paths
        res = run_command_sync(cmd, workspace, timeout, sandbox=True)
        match = _COVERAGE_TOTAL.search(res.stdout or "")
        if match:
            res.stdout = f"coverage total: {match.group(1)}%\n" + res.stdout
        return res


SIGNAL_PROVIDERS: list[SignalProvider] = [
    TypecheckProvider(),
    BuildProvider(),
    CoverageProvider(),
]


def register_signal_provider(provider: SignalProvider) -> None:
    SIGNAL_PROVIDERS.append(provider)


def _provider_timeout(timeout: float) -> float:
    try:
        cap = float(os.environ.get("ADA_SIGNAL_TIMEOUT", "60"))
    except ValueError:
        cap = 60.0
    return max(1.0, min(timeout, cap))


def run_signal_providers(workspace: Path, changed_files: list[str],
                         timeout: float) -> dict[str, ExecutionResult]:
    """Run every available registered provider, bounded and failure-tolerant."""
    extra: dict[str, ExecutionResult] = {}
    for provider in list(SIGNAL_PROVIDERS):
        try:
            if not provider.available(workspace):
                continue
            res = provider.run(workspace, changed_files, _provider_timeout(timeout))
            if res is not None:
                extra[provider.name] = res
        except Exception as exc:  # a broken provider must never abort verification
            logger.debug("signal provider %r failed: %s", getattr(provider, "name", "?"), exc)
    return extra


def capture_baseline(workspace: Path, *, run_tests: bool, lint: bool, timeout: float,
                     static: bool = False) -> dict:
    """Snapshot test + lint state BEFORE agents touch anything, so gating works on the delta.

    For a repo-backed run this answers "did we break something" — tests that were already
    red are not attributed to a subtask. With ``static`` on, detected static checks
    (ruff/eslint/tsc) are counted once here too; per-subtask verification re-counts and
    gates on the delta. No tools detected → no keys added (byte-identical behavior).
    """
    baseline: dict = {}
    try:
        if run_tests:
            baseline["tests"] = run_workspace_tests_sync(workspace, timeout)
        if lint:
            baseline["lint"] = lint_workspace(workspace)
            baseline["lint_errors"] = _lint_error_count(baseline.get("lint"))
        if static:
            checks = detect_static_checks(workspace)
            if checks:
                baseline["static_checks"] = checks
                baseline["static"] = run_static_checks(workspace, checks)
        extra = run_signal_providers(workspace, [], timeout)
        if extra:
            baseline["extra"] = extra
    except Exception as exc:  # a baseline hiccup must never abort the run
        logger.warning("baseline capture failed: %s", exc)
    return baseline


def gather_signals(workspace: Path, files: list[str], *, run_tests: bool, lint: bool,
                   timeout: float, test_paths: list[str] | None = None,
                   static_checks: list[Check] | None = None) -> dict:
    """Collect objective signals for one subtask.

    ``test_paths`` scopes the test run to the subtask's changed files (V2): passing it
    avoids re-running the whole workspace suite per subtask and stops sibling subtasks
    from failing each other. ``static_checks`` is the run-baseline's detected check list;
    when given, each check is re-counted post-change so the gate sees the delta.
    """
    signals: dict = {"files": collect_file_contents(workspace, files)}
    if run_tests:
        signals["tests"] = run_workspace_tests_sync(workspace, timeout, paths=test_paths)
        signals["scoped"] = bool(test_paths)
    if lint:
        signals["lint"] = lint_workspace(workspace)
    if static_checks:
        counts = run_static_checks(workspace, static_checks)
        if counts:
            signals["static"] = counts
    extra = run_signal_providers(workspace, files, timeout)
    if extra:
        signals["extra"] = extra
    return signals


def apply_objective_gate(verdict: Verdict, signals: dict, *, baseline: dict | None = None) -> Verdict:
    """Fold objective signals into the LLM verdict, gated on the pre-run baseline.

    - tests newly failing (baseline was green or absent) → hard fail, regardless of the LLM.
    - tests failing but they were ALREADY failing at baseline → note, don't blame the subtask.
    - LLM-failed but scoped tests pass → soft pass; the override requires that the tests were
      scoped to this subtask's changes, and caps the score at 70 instead of flooring it.
    - new lint errors versus baseline → score penalty, not just a note.
    - a typecheck/build signal that was PASSING at baseline and now FAILS → -15 score and a
      note (never a hard fail on its own).
    - static checks (ruff/eslint/tsc counts) with a positive delta versus the run baseline →
      same demotion as newly-failing tests ("tests pass but the diff added 40 lint errors"
      fails honestly); zero/negative deltas add the compact note only.
    """
    baseline = baseline or {}
    tests: ExecutionResult | None = signals.get("tests")
    base_tests: ExecutionResult | None = baseline.get("tests")
    notes: list[str] = []
    updates: dict = {}

    # Static-check delta first so every early-return path carries the note. Detection-empty
    # runs put no "static" key in signals — nothing is appended anywhere.
    static_note, static_regressed = "", False
    post_static: dict = signals.get("static") or {}
    if post_static:
        static_note, static_regressed = static_delta_note(post_static,
                                                          baseline.get("static") or {})
        if static_note:
            notes.append(static_note)

    if tests is not None:
        notes.append(f"tests: {'PASS' if tests.passed else 'FAIL'} (exit {tests.return_code})")
        if not tests.passed:
            if base_tests is not None and not base_tests.passed:
                # Pre-existing red suite: don't attribute it to this subtask (V3).
                notes.append("failures pre-date this run (baseline was already failing)")
            else:
                return verdict.model_copy(update={
                    "passed": False,
                    "score": min(verdict.score, 20),
                    "reasons": [*verdict.reasons, "Objective: tests failed (they passed at baseline)."],
                    "objective_note": "; ".join(notes),
                })
        elif not verdict.passed:
            if signals.get("scoped") and not static_regressed:
                # Green tests that exercise this subtask's changes — soft pass, capped (V1).
                notes.append("LLM flagged issues but the subtask's tests pass → soft pass (capped)")
                return verdict.model_copy(update={
                    "passed": True,
                    "score": min(70, max(verdict.score, 50)),
                    "reasons": [*verdict.reasons,
                                "Objective override: the subtask's own tests pass; treated as a soft pass."],
                    "objective_note": "; ".join(notes),
                })
            if signals.get("scoped"):
                notes.append("subtask tests pass but static checks regressed → no soft-pass override")
            else:
                # Unscoped green tests prove nothing about THIS subtask's criteria — no override.
                notes.append("tests pass but are not scoped to this subtask → no override of the review")

    if static_regressed:
        # Same demotion as the newly-failing-test path: the diff objectively made the
        # workspace worse, whatever the LLM thought of the prose.
        return verdict.model_copy(update={
            "passed": False,
            "score": min(verdict.score, 20),
            "reasons": [*verdict.reasons,
                        f"Objective: static checks regressed vs the pre-run baseline ({static_note})."],
            "objective_note": "; ".join(notes),
        })

    penalty = 0
    lint: ExecutionResult | None = signals.get("lint")
    if lint is not None and not lint.passed:
        new_errors = _lint_error_count(lint) - int(baseline.get("lint_errors", 0))
        if new_errors > 0:
            notes.append(f"lint: ~{new_errors} new issue(s)")
            penalty += 10
        else:
            notes.append("lint: issues found (pre-existing)")

    # Extra signals (V5): a typecheck/build regression versus baseline costs score.
    extra: dict = signals.get("extra") or {}
    base_extra: dict = baseline.get("extra") or {}
    for name in ("typecheck", "build"):
        cur: ExecutionResult | None = extra.get(name)
        if cur is None or cur.passed:
            continue
        base: ExecutionResult | None = base_extra.get(name)
        if base is not None and base.passed:
            notes.append(f"{name}: FAIL (was passing at baseline)")
            penalty += 15
        else:
            notes.append(f"{name}: failing (not attributable — no green baseline)")

    if penalty:
        updates["score"] = max(0, verdict.score - penalty)
    if notes:
        updates["objective_note"] = "; ".join(notes)
    return verdict.model_copy(update=updates) if updates else verdict
