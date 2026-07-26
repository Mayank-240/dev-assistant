"""Read-only spend analytics over the run store and per-run event logs.

Aggregation layer for a cost dashboard. Everything here is strictly read-only
against the run store: the runs database is opened on its own ``mode=ro``
SQLite connection (never the engine's read-write handle, never created if
absent), and per-run ``events.jsonl`` logs are parsed defensively — malformed
lines are skipped, a deleted docs directory yields an empty breakdown. The one
write this module performs is its own alert bookkeeping:
``<data_dir>/spend_alerts.json`` (see :func:`check_spend_alerts`).

Data sources
------------
- ``<data_dir>/runs.db`` ``runs`` table: cost_usd, input_tokens, output_tokens,
  created_at (epoch seconds, UTC), status, project, quality_score,
  subtasks_passed.
- ``<data_dir>/runs.db`` ``subtask_reviews`` table: human accept/reject
  decisions per subtask.
- ``<docs_dir>/<task_id>/events.jsonl``: ``subtask_review`` events carry
  ``data.cost = {cost_usd, input_tokens, output_tokens}`` (per-subtask delta);
  ``subtask_start`` events carry ``data.agent`` for attribution.

Conventions
-----------
- Time windows are calendar UTC days derived from ``created_at``: a window of
  ``days`` covers today (UTC) plus the previous ``days - 1`` days.
- Missing/None costs are treated as 0 in sums but surfaced separately as
  ``runs_without_cost``.
- Ratios with a zero denominator are ``None``, never a ZeroDivisionError.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "spend_overview",
    "project_spend",
    "cost_per_outcome",
    "run_cost_breakdown",
    "check_spend_alerts",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _db_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "runs.db"


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    """A dedicated read-only connection, or None when the DB doesn't exist.

    ``mode=ro`` guarantees SQLite refuses every write; ``query_only`` is belt
    and braces. The file is never created by this module.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=1")
        except sqlite3.Error:
            pass
        return conn
    except sqlite3.Error:
        return None


def _window_start(days: int) -> float:
    """Epoch timestamp of midnight UTC, ``days - 1`` days before today (UTC)."""
    if days <= 0:
        return 0.0
    start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    return datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()


def _utc_day(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _i(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ratio(total: float, count: int) -> float | None:
    return round(total / count, 6) if count else None


# ---------------------------------------------------------------------------
# spend overview (global + per-project)
# ---------------------------------------------------------------------------

def _empty_overview(days: int) -> dict[str, Any]:
    return {
        "window_days": days,
        "total_usd": 0.0,
        "runs": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "runs_without_cost": 0,
        "by_day": [],
        "by_project": [],
        "by_status": {},
    }


def _scoped_overview(settings: Any, *, days: int, project: str | None) -> dict[str, Any]:
    out = _empty_overview(days)
    conn = _connect_ro(_db_path(settings))
    if conn is None:
        return out
    try:
        query = (
            "SELECT created_at, cost_usd, input_tokens, output_tokens, "
            "COALESCE(project, 'default') AS project, "
            "COALESCE(status, 'unknown') AS status, quality_score "
            "FROM runs WHERE created_at >= ?"
        )
        params: list[Any] = [_window_start(days)]
        if project is not None:
            query += " AND COALESCE(project, 'default') = ?"
            params.append(project)
        try:
            rows = conn.execute(query, params).fetchall()
        except sqlite3.Error:
            return out
    finally:
        conn.close()

    by_day: dict[str, dict[str, Any]] = {}
    by_project: dict[str, dict[str, Any]] = {}
    by_status: dict[str, dict[str, Any]] = {}
    for r in rows:
        usd = _f(r["cost_usd"])
        out["total_usd"] += usd
        out["runs"] += 1
        out["tokens_in"] += _i(r["input_tokens"])
        out["tokens_out"] += _i(r["output_tokens"])
        if r["cost_usd"] is None:
            out["runs_without_cost"] += 1

        day = by_day.setdefault(_utc_day(r["created_at"]), {"usd": 0.0, "runs": 0})
        day["usd"] += usd
        day["runs"] += 1

        proj = by_project.setdefault(str(r["project"]),
                                     {"usd": 0.0, "runs": 0, "quality": []})
        proj["usd"] += usd
        proj["runs"] += 1
        if r["quality_score"] is not None:
            proj["quality"].append(_f(r["quality_score"]))

        st = by_status.setdefault(str(r["status"]), {"usd": 0.0, "runs": 0})
        st["usd"] += usd
        st["runs"] += 1

    out["total_usd"] = round(out["total_usd"], 6)
    out["by_day"] = [
        {"date": d, "usd": round(v["usd"], 6), "runs": v["runs"]}
        for d, v in sorted(by_day.items())
    ]
    out["by_project"] = sorted(
        (
            {
                "project": p,
                "usd": round(v["usd"], 6),
                "runs": v["runs"],
                "avg_quality": (round(sum(v["quality"]) / len(v["quality"]), 3)
                                if v["quality"] else None),
            }
            for p, v in by_project.items()
        ),
        key=lambda e: (-e["usd"], e["project"]),
    )
    out["by_status"] = {
        s: {"usd": round(v["usd"], 6), "runs": v["runs"]}
        for s, v in sorted(by_status.items())
    }
    return out


def spend_overview(settings: Any, *, days: int = 30) -> dict[str, Any]:
    """Spend across all projects over the last ``days`` UTC calendar days."""
    return _scoped_overview(settings, days=days, project=None)


def project_spend(settings: Any, slug: str, *, days: int = 90) -> dict[str, Any]:
    """The same shape as :func:`spend_overview`, scoped to one project slug."""
    out = _scoped_overview(settings, days=days, project=slug)
    out["project"] = slug
    return out


# ---------------------------------------------------------------------------
# cost per outcome
# ---------------------------------------------------------------------------

def cost_per_outcome(settings: Any, *, days: int = 90) -> dict[str, Any]:
    """USD per completed run / passed subtask / accepted subtask in the window.

    - completed runs and passed subtasks come from the ``runs`` table;
    - accepted subtasks come from human decisions in ``subtask_reviews``;
    - a zero denominator yields ``None`` for that ratio.
    """
    out: dict[str, Any] = {
        "window_days": days,
        "total_usd": 0.0,
        "completed_runs": 0,
        "passed_subtasks": 0,
        "accepted_subtasks": 0,
        "usd_per_completed_run": None,
        "usd_per_passed_subtask": None,
        "usd_per_accepted_subtask": None,
    }
    conn = _connect_ro(_db_path(settings))
    if conn is None:
        return out
    try:
        cutoff = _window_start(days)
        try:
            rows = conn.execute(
                "SELECT status, cost_usd, subtasks_passed FROM runs "
                "WHERE created_at >= ?", (cutoff,)).fetchall()
        except sqlite3.Error:
            return out
        for r in rows:
            out["total_usd"] += _f(r["cost_usd"])
            if r["status"] == "completed":
                out["completed_runs"] += 1
            out["passed_subtasks"] += max(0, _i(r["subtasks_passed"]))
        try:
            accepted = conn.execute(
                "SELECT COUNT(*) FROM subtask_reviews sr "
                "JOIN runs r ON r.id = sr.run_id "
                "WHERE sr.decision = 'accepted' AND r.created_at >= ?",
                (cutoff,)).fetchone()[0]
            out["accepted_subtasks"] = _i(accepted)
        except sqlite3.Error:
            pass  # table absent in very old DBs — accepted stays 0
    finally:
        conn.close()

    out["total_usd"] = round(out["total_usd"], 6)
    out["usd_per_completed_run"] = _ratio(out["total_usd"], out["completed_runs"])
    out["usd_per_passed_subtask"] = _ratio(out["total_usd"], out["passed_subtasks"])
    out["usd_per_accepted_subtask"] = _ratio(out["total_usd"], out["accepted_subtasks"])
    return out


# ---------------------------------------------------------------------------
# budget alerts (spend vs. monthly cap, fire-once-per-month bookkeeping)
# ---------------------------------------------------------------------------

_ALERTS_FILENAME = "spend_alerts.json"
_ALERT_WINDOW_DAYS = 30


def _alerts_path(settings: Any) -> Path:
    return Path(settings.data_dir) / _ALERTS_FILENAME


def _load_alert_state(settings: Any) -> dict[str, Any]:
    """{"month": "YYYY-MM", "fired": [thresholds]} — {} on missing/corrupt."""
    try:
        data = json.loads(_alerts_path(settings).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_alert_state(settings: Any, state: dict[str, Any]) -> None:
    path = _alerts_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def check_spend_alerts(settings: Any, monthly_cap: float, *,
                       thresholds: tuple[float, ...] = (0.5, 0.8, 1.0),
                       now: float | None = None) -> list[dict[str, Any]]:
    """Alerts for spend thresholds newly crossed this calendar month.

    Compares the 30-day spend (:func:`spend_overview`) against
    ``monthly_cap * threshold`` for each threshold. ``monthly_cap`` is a
    PARAMETER on purpose: this module does not read the settings schema — the
    server passes the cap it derives from its Settings (e.g. ``budget_usd``).
    A cap <= 0 disables alerting entirely.

    Fire-once semantics: which thresholds have already alerted is persisted in
    ``<data_dir>/spend_alerts.json`` keyed by UTC calendar month
    (``{"month": "YYYY-MM", "fired": [...]}``); a threshold alerts at most once
    per month, and the state resets when the month rolls over. ``now`` (epoch
    seconds) exists for deterministic tests and defaults to the current time —
    it selects the bookkeeping month only, not the spend window.

    Returns the NEW alerts, oldest threshold first, each::

        {"type": "spend", "threshold": 0.8, "percent": 80, "spend_usd": ...,
         "monthly_cap": ..., "month": "2026-07", "window_days": 30}
    """
    cap = _f(monthly_cap)
    if cap <= 0:
        return []
    ts = time.time() if now is None else float(now)
    month = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
    spend = _f(spend_overview(settings, days=_ALERT_WINDOW_DAYS)["total_usd"])

    state = _load_alert_state(settings)
    fired: list[float] = ([_f(t) for t in state.get("fired", [])]
                          if state.get("month") == month else [])

    alerts: list[dict[str, Any]] = []
    for th in sorted({_f(t) for t in thresholds}):
        if th <= 0 or th in fired:
            continue
        if spend >= cap * th:
            alerts.append({
                "type": "spend",
                "threshold": th,
                "percent": int(round(th * 100)),
                "spend_usd": round(spend, 6),
                "monthly_cap": round(cap, 6),
                "month": month,
                "window_days": _ALERT_WINDOW_DAYS,
            })
            fired.append(th)
    if alerts or state.get("month") != month:
        _save_alert_state(settings, {"month": month, "fired": sorted(fired)})
    return alerts


# ---------------------------------------------------------------------------
# per-run cost breakdown (events.jsonl)
# ---------------------------------------------------------------------------

def run_cost_breakdown(settings: Any, task_id: str) -> dict[str, Any]:
    """Per-subtask costs for one run, parsed from its ``events.jsonl``.

    Sums every ``subtask_review`` event's ``data.cost`` per subtask id (retries
    accumulate), attributes agents from ``subtask_start`` events, and reports
    the run-level total from ``runs.db`` plus the unattributed remainder
    (planning/orchestration overhead). Malformed lines are skipped; a missing
    events file or docs directory yields an empty breakdown.
    """
    out: dict[str, Any] = {
        "task_id": task_id,
        "subtasks": [],
        "totals": {"usd": 0.0, "tokens_in": 0, "tokens_out": 0},
        "run_total_usd": None,
        "unattributed_usd": None,
    }

    events_path = Path(settings.docs_dir) / str(task_id) / "events.jsonl"
    agents: dict[str, str] = {}
    acc: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        text = events_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # truncated/corrupt line — skip, keep going
        if not isinstance(ev, dict):
            continue
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        sid = data.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        etype = ev.get("type")
        if etype == "subtask_start":
            agent = data.get("agent")
            if isinstance(agent, str) and agent:
                agents.setdefault(sid, agent)
        elif etype == "subtask_review":
            cost = data.get("cost")
            if not isinstance(cost, dict):
                cost = {}
            entry = acc.get(sid)
            if entry is None:
                entry = acc[sid] = {"usd": 0.0, "tokens_in": 0, "tokens_out": 0}
                order.append(sid)
            entry["usd"] += _f(cost.get("cost_usd"))
            entry["tokens_in"] += _i(cost.get("input_tokens"))
            entry["tokens_out"] += _i(cost.get("output_tokens"))

    for sid in order:
        e = acc[sid]
        out["subtasks"].append({
            "subtask": sid,
            "agent": agents.get(sid),
            "usd": round(e["usd"], 6),
            "tokens_in": e["tokens_in"],
            "tokens_out": e["tokens_out"],
        })
        out["totals"]["usd"] += e["usd"]
        out["totals"]["tokens_in"] += e["tokens_in"]
        out["totals"]["tokens_out"] += e["tokens_out"]
    out["totals"]["usd"] = round(out["totals"]["usd"], 6)

    conn = _connect_ro(_db_path(settings))
    if conn is not None:
        try:
            try:
                row = conn.execute(
                    "SELECT cost_usd FROM runs WHERE id = ?", (task_id,)).fetchone()
            except sqlite3.Error:
                row = None
        finally:
            conn.close()
        if row is not None and row["cost_usd"] is not None:
            out["run_total_usd"] = round(_f(row["cost_usd"]), 6)
            out["unattributed_usd"] = round(out["run_total_usd"] - out["totals"]["usd"], 6)
    return out
