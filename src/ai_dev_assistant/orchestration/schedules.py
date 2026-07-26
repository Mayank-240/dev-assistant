"""Recurring per-project runs ("nightly dependency audit") with simple interval scheduling.

ScheduleStore keeps its own table (`schedules`) in the same runs.db that RunStore uses,
on its own connection (WAL + busy_timeout, additive CREATE TABLE — RunStore is untouched).
No background thread lives here: the web server's existing periodic loop calls `due()`
and enqueues each due schedule as a normal task, then records it via `mark_started()`.
Everything is deterministic and testable through the explicit `now` parameters.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

MIN_INTERVAL_HOURS = 0.25  # 15 minutes — anything tighter is a polling loop, not a schedule

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    project TEXT,
    prompt TEXT,
    title TEXT,
    every_hours REAL,
    budget_usd REAL,
    enabled INTEGER,
    last_run_at REAL,
    last_task_id TEXT,
    created_at REAL
);
"""

# Fields a caller may change through update(); everything else is immutable or
# owned by mark_started().
_UPDATABLE = {"project", "prompt", "title", "every_hours", "budget_usd", "enabled"}


def _slug(text: str, max_len: int = 24) -> str:
    """Short lowercase slug from a title/prompt for readable schedule ids."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].rstrip("-") or "schedule"


def _validate_interval(every_hours: Any) -> float:
    try:
        hours = float(every_hours)
    except (TypeError, ValueError):
        raise ValueError(f"every_hours must be a number, got {every_hours!r}") from None
    if hours < MIN_INTERVAL_HOURS:
        raise ValueError(
            f"every_hours must be >= {MIN_INTERVAL_HOURS} (15 minutes), got {hours}")
    return hours


def next_run_at(row: dict[str, Any]) -> float | None:
    """When this schedule next fires (epoch seconds) — for UI display.

    None when the schedule is disabled. A never-run enabled schedule is due
    immediately, so its next run is its creation time (already in the past).
    """
    if not row.get("enabled"):
        return None
    last = row.get("last_run_at")
    if last is None:
        return float(row.get("created_at") or 0.0)
    return float(last) + float(row["every_hours"]) * 3600.0


class ScheduleStore:
    """CRUD + due-computation for recurring runs, in its own `schedules` table."""

    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Same file as RunStore, separate connection — WAL + busy timeout keep the
        # two writers from tripping over each other (mirrors RunStore's settings).
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.OperationalError:
            pass
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- CRUD ----
    def create(self, *, project: str, prompt: str, title: str | None = None,
               every_hours: float, budget_usd: float = 0.0) -> dict[str, Any]:
        """Create a schedule. `project` is stored as-is — resolution against the
        configured project list is the caller's job (unknown strings are allowed)."""
        hours = _validate_interval(every_hours)
        if not (prompt or "").strip():
            raise ValueError("prompt must not be empty")
        sid = f"{_slug(title or prompt)}-{secrets.token_hex(3)}"
        self._conn.execute(
            "INSERT INTO schedules(id, project, prompt, title, every_hours, budget_usd, "
            "enabled, last_run_at, last_task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?)",
            (sid, project, prompt, title, hours, float(budget_usd), time.time()),
        )
        self._conn.commit()
        return self.get(sid)  # type: ignore[return-value]

    def get(self, sid: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
        return self._to_dict(row) if row else None

    def list(self, project: str | None = None) -> list[dict[str, Any]]:
        if project is not None:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE project = ? ORDER BY created_at ASC",
                (project,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM schedules ORDER BY created_at ASC").fetchall()
        return [self._to_dict(r) for r in rows]

    def update(self, sid: str, **fields: Any) -> dict[str, Any]:
        """Change updatable fields (enable/disable, prompt, every_hours, ...)."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update field(s): {', '.join(sorted(unknown))}")
        if not fields:
            raise ValueError("no fields to update")
        if "every_hours" in fields:
            fields["every_hours"] = _validate_interval(fields["every_hours"])
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        if "budget_usd" in fields:
            fields["budget_usd"] = float(fields["budget_usd"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = self._conn.execute(
            f"UPDATE schedules SET {cols} WHERE id = ?", (*fields.values(), sid))
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"unknown schedule: {sid}")
        return self.get(sid)  # type: ignore[return-value]

    def delete(self, sid: str) -> None:
        self._conn.execute("DELETE FROM schedules WHERE id = ?", (sid,))
        self._conn.commit()

    # ---- due-computation (polled by the server's existing loop) ----
    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        """Schedules whose interval has elapsed: enabled AND
        (never run, or last_run_at + every_hours*3600 <= now)."""
        ts = time.time() if now is None else now
        rows = self._conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND "
            "(last_run_at IS NULL OR last_run_at + every_hours * 3600.0 <= ?) "
            "ORDER BY created_at ASC", (ts,)).fetchall()
        return [self._to_dict(r) for r in rows]

    def mark_started(self, sid: str, task_id: str, now: float | None = None) -> None:
        """Record that a run was enqueued for this schedule — advances the interval."""
        ts = time.time() if now is None else now
        cur = self._conn.execute(
            "UPDATE schedules SET last_run_at = ?, last_task_id = ? WHERE id = ?",
            (ts, task_id, sid))
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"unknown schedule: {sid}")

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["enabled"] = bool(d.get("enabled"))
        return d
