"""Static-analysis objective gates (per-run baseline, per-subtask delta).

Detects which linters/typecheckers apply to a workspace (ruff, eslint, tsc — same
detection-pattern spirit as ``execution.detect_test_command``), runs them through the
sandboxed ``run_command_sync`` machinery, and reduces each tool's output to a problem
COUNT. The engine captures counts once per run as a baseline, re-counts per accepted
subtask, and feeds the delta into the reviewer's objective gate: a positive delta
("the diff added lint errors / broke types") demotes the verdict exactly like a
newly-failing test does.

Failure safety: a check that times out, crashes, or whose tool is missing is logged
and SKIPPED — static analysis must never block a run, and when nothing is detected
nothing is appended anywhere (prompts stay byte-identical).
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .execution import ExecutionResult, run_command_sync

logger = logging.getLogger("ada.static")

# Modest per-check budget: these are advisory gates, not the test suite.
STATIC_CHECK_TIMEOUT = 60.0

# "path:line:col:" diagnostic lines — ruff `--output-format concise` and eslint `-f unix`.
_DIAG_LINE = re.compile(r"^.+?:\d+:\d+:")
_TSC_ERROR = re.compile(r"\berror TS\d+")


def _count_diag_lines(res: ExecutionResult) -> int:
    return sum(1 for ln in (res.stdout or "").splitlines() if _DIAG_LINE.match(ln.strip()))


def _count_tsc_errors(res: ExecutionResult) -> int:
    return sum(1 for ln in (res.stdout or "").splitlines() if _TSC_ERROR.search(ln))


@dataclass(frozen=True)
class Check:
    """One detected static check: how to run it and how to count its problems."""

    name: str
    cmd: tuple[str, ...]
    parse: Callable[[ExecutionResult], int]


def _has_ruff_config(workspace: Path) -> bool:
    if (workspace / "ruff.toml").is_file() or (workspace / ".ruff.toml").is_file():
        return True
    pyproject = workspace / "pyproject.toml"
    try:
        return pyproject.is_file() and "[tool.ruff" in pyproject.read_text(errors="replace")
    except OSError:
        return False


_ESLINT_CONFIGS = (
    ".eslintrc", ".eslintrc.json", ".eslintrc.js", ".eslintrc.cjs",
    ".eslintrc.yml", ".eslintrc.yaml",
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
)


def _eslint_bin(workspace: Path) -> str | None:
    local = workspace / "node_modules" / ".bin" / "eslint"
    if local.is_file():
        return str(local)
    return shutil.which("eslint")


def detect_static_checks(workspace: Path) -> list[Check]:
    """Which static checks apply here — tool present AND the workspace opts in.

    ruff:   ruff.toml / .ruff.toml / pyproject [tool.ruff] — or any .py files — plus
            ruff on PATH.
    eslint: an eslint config file plus node_modules/.bin/eslint or eslint on PATH.
    tsc:    tsconfig.json plus tsc on PATH (--noEmit).
    """
    if not workspace.is_dir():
        return []
    checks: list[Check] = []
    if (_has_ruff_config(workspace) or any(workspace.rglob("*.py"))) \
            and shutil.which("ruff") is not None:
        checks.append(Check("ruff", ("ruff", "check", "--output-format", "concise", "."),
                            _count_diag_lines))
    if any((workspace / cfg).is_file() for cfg in _ESLINT_CONFIGS):
        eslint = _eslint_bin(workspace)
        if eslint is not None:
            checks.append(Check("eslint", (eslint, "-f", "unix", "."), _count_diag_lines))
    if (workspace / "tsconfig.json").is_file() and shutil.which("tsc") is not None:
        checks.append(Check("tsc", ("tsc", "--noEmit"), _count_tsc_errors))
    return checks


def run_static_checks(workspace: Path, checks: list[Check],
                      timeout: float = STATIC_CHECK_TIMEOUT) -> dict[str, int]:
    """Problem count per check name. A check that can't produce an honest count
    (timeout, missing binary, tool crash without diagnostics) is skipped entirely —
    it must neither block the run nor masquerade as a zero."""
    counts: dict[str, int] = {}
    for check in checks:
        try:
            res = run_command_sync(list(check.cmd), workspace, timeout, sandbox=True)
        except Exception as exc:  # never let a check abort verification
            logger.warning("static check %s errored (%s); skipped", check.name, exc)
            continue
        if res.timed_out or res.return_code < 0:
            logger.warning("static check %s did not complete (%s); skipped",
                           check.name, "timeout" if res.timed_out else res.stderr.strip()[:200])
            continue
        try:
            count = check.parse(res)
        except Exception as exc:
            logger.warning("static check %s output unparseable (%s); skipped", check.name, exc)
            continue
        if res.return_code != 0 and count == 0:
            # Non-zero exit with no diagnostics = the tool itself failed (config error,
            # crash) — a fake 0 would hide a real baseline, so skip.
            logger.warning("static check %s failed without diagnostics (exit %d); skipped",
                           check.name, res.return_code)
            continue
        counts[check.name] = count
    return counts


def static_delta_note(post: dict[str, int], baseline: dict[str, int]) -> tuple[str, bool]:
    """Compact reviewer-facing line + whether any check regressed.

    e.g. ``("static checks: ruff +3 (baseline 12), tsc +0", True)``. Delta is
    post-change count minus the pre-run baseline; positive = regressions the
    subtask introduced. A check present at baseline but skipped now contributes
    nothing (no honest count to compare).
    """
    parts: list[str] = []
    regressed = False
    for name, count in post.items():
        base = int(baseline.get(name, 0))
        delta = count - base
        part = f"{name} {delta:+d}"
        if base:
            part += f" (baseline {base})"
        parts.append(part)
        regressed = regressed or delta > 0
    return ("static checks: " + ", ".join(parts)) if parts else "", regressed
