"""Benchmark history: track eval scores over time, across commits.

Every eval run can append one JSONL line to ``<data_dir>/benchmarks.jsonl`` (opt-in via
``ada eval --record-history`` or ``python -m ai_dev_assistant.evals.replay_eval
--record-history`` / ``ADA_EVAL_RECORD_HISTORY=1``). Each entry captures the aggregate
scores of an :class:`~ai_dev_assistant.evals.harness.EvalReport` plus the git state it
ran at, so prompt/agent changes can be compared commit-to-commit:

    {ts, git_sha, dirty, suite, pass_rate, quality_mean, quality_min,
     cost_usd, duration_s, runs}

- ``pass_rate`` / ``quality_*`` / ``runs`` aggregate over ATTEMPTED cards only —
  toolchain-skipped cards never dilute the scores (matching the harness's own
  pass/fail denominator).  ``cost_usd`` / ``duration_s`` sum over all cards.
- ``git_sha`` is the short HEAD sha of the process cwd ("" outside a repo); ``dirty``
  flags uncommitted changes.  Git probing is best-effort with a timeout — it never
  raises and never blocks recording.

``load_history`` reads entries back (newest-last, corrupt lines skipped) and
``trend_report`` condenses them into a chartable shape: the latest entry, deltas vs
the previous same-suite entry, and a per-sha latest series.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings

HISTORY_FILENAME = "benchmarks.jsonl"

_GIT_TIMEOUT_S = 5.0
_SERIES_LIMIT = 20


def history_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / HISTORY_FILENAME


def _git(args: list[str], cwd: str | None = None) -> str | None:
    """Run a git command in ``cwd`` (default: process cwd). None on any failure —
    missing binary, not a repo, timeout — never raises."""
    try:
        res = subprocess.run(["git", *args], cwd=cwd or os.getcwd(),
                             capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — recording must never fail on git probing
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _git_state(cwd: str | None = None) -> tuple[str, bool]:
    """(short sha, dirty) for the repo at ``cwd`` — ("", False) when not a repo."""
    sha = _git(["rev-parse", "--short", "HEAD"], cwd)
    if sha is None:
        return "", False
    return sha, bool(_git(["status", "--porcelain"], cwd))


def _summarize(result: Any) -> dict:
    """Aggregate an EvalReport-shaped object (anything with ``.cards`` of
    Scorecard-shaped items) into the recorded score fields."""
    cards = list(getattr(result, "cards", None) or [])
    attempted = [c for c in cards if not getattr(c, "skipped", False)]
    passed = sum(1 for c in attempted if getattr(c, "passed", False))
    qualities = [float(getattr(c, "quality", 0.0) or 0.0) for c in attempted]
    return {
        "pass_rate": round(passed / len(attempted), 4) if attempted else 0.0,
        "quality_mean": round(sum(qualities) / len(qualities), 2) if qualities else 0.0,
        "quality_min": round(min(qualities), 2) if qualities else 0.0,
        "cost_usd": round(sum(float(getattr(c, "cost_usd", 0.0) or 0.0) for c in cards), 6),
        "duration_s": round(sum(float(getattr(c, "wall_s", 0.0) or 0.0) for c in cards), 2),
        "runs": len(attempted),
    }


def record_result(settings: Settings, suite: str, result: Any) -> dict:
    """Append one benchmark entry for ``result`` (an EvalReport) and return it."""
    sha, dirty = _git_state()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": sha,
        "dirty": dirty,
        "suite": str(suite),
        **_summarize(result),
    }
    path = history_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def load_history(settings: Settings, suite: str | None = None,
                 limit: int = 100) -> list[dict]:
    """Read recorded entries, newest-last. Corrupt/non-object lines are skipped.
    ``suite`` filters to one suite; ``limit`` keeps the newest N (<=0 keeps all)."""
    path = history_path(settings)
    if not path.is_file():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # corrupt line — tolerate and move on
        if not isinstance(obj, dict):
            continue
        if suite is not None and obj.get("suite") != suite:
            continue
        entries.append(obj)
    return entries[-limit:] if limit > 0 else entries


def _num(entry: dict, key: str) -> float:
    try:
        return float(entry.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def trend_report(settings: Settings, suite: str | None = None) -> dict:
    """Chartable trend over the recorded history:

    {"latest": {...} | None,
     "delta":  {pass_rate, quality_mean, quality_min, cost_usd} | None,
     "series": [{sha, ts, pass_rate, quality_mean, cost_usd}, ...]}

    ``delta`` compares the latest entry with the previous entry of the SAME suite
    (None when there is no predecessor). ``series`` is the per-sha latest entry for
    the last N shas of that suite, oldest-first — ready for a UI chart.
    """
    entries = load_history(settings, suite=suite, limit=0)
    if not entries:
        return {"latest": None, "delta": None, "series": []}
    latest = entries[-1]
    same_suite = [e for e in entries if e.get("suite") == latest.get("suite")]
    delta = None
    if len(same_suite) >= 2:
        prev = same_suite[-2]
        delta = {k: round(_num(latest, k) - _num(prev, k), 6)
                 for k in ("pass_rate", "quality_mean", "quality_min", "cost_usd")}
    # Per-sha latest, ordered by each sha's most recent appearance.
    per_sha: dict[str, dict] = {}
    order: list[str] = []
    for e in same_suite:
        sha = str(e.get("git_sha") or "")
        if sha in per_sha:
            order.remove(sha)
        order.append(sha)
        per_sha[sha] = e
    series = [{"sha": sha,
               "ts": per_sha[sha].get("ts"),
               "pass_rate": per_sha[sha].get("pass_rate"),
               "quality_mean": per_sha[sha].get("quality_mean"),
               "cost_usd": per_sha[sha].get("cost_usd")}
              for sha in order[-_SERIES_LIMIT:]]
    return {"latest": latest, "delta": delta, "series": series}
