"""Recurring per-project runs ("nightly dependency audit") with interval or cron recurrence.

ScheduleStore keeps its own table (`schedules`) in the same runs.db that RunStore uses,
on its own connection (WAL + busy_timeout, additive CREATE TABLE — RunStore is untouched).
No background thread lives here: the web server's existing periodic loop calls `due()`
and enqueues each due schedule as a normal task, then records it via `mark_started()`.
Everything is deterministic and testable through the explicit `now` parameters.

Recurrence is one of two shapes, exactly one per schedule:

* **interval** — `every_hours`: due once the interval has elapsed since the last start;
* **cron** — a classic 5-field expression (minute hour day-of-month month day-of-week)
  parsed by the minimal in-house parser below (no dependency): ``*``, numbers, comma
  lists, ranges ``a-b``, steps ``*/n`` (also on ranges), day-of-week 0-6 with Sunday=0
  and 7 accepted as Sunday. Cron times are LOCAL time. A cron row is due when a
  matching minute boundary has passed since the last start (creation when never run) —
  `mark_started()` advances that reference, so a row can never fire twice within the
  same cron minute.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    cron TEXT,
    budget_usd REAL,
    enabled INTEGER,
    last_run_at REAL,
    last_task_id TEXT,
    created_at REAL
);
"""

# Fields a caller may change through update(); everything else is immutable or
# owned by mark_started().
_UPDATABLE = {"project", "prompt", "title", "every_hours", "cron", "budget_usd", "enabled"}


# ---------------------------------------------------------------- cron parsing

_CRON_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),  # 0-6 Sunday=0; 7 accepted as Sunday and folded to 0
)

_CRON_SEARCH_DAYS = 366  # next_fire gives up past this — catches impossible dates


@dataclass(frozen=True)
class _Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool      # day-of-month field was not the literal "*"
    weekday_restricted: bool  # day-of-week field was not the literal "*"


def _parse_cron_field(spec: str, name: str, lo: int, hi: int) -> frozenset[int]:
    """One cron field -> the set of matching values. Supports "*", numbers, comma
    lists, ranges "a-b", and steps "*/n" (also on ranges). Raises ValueError with a
    message naming the field."""
    values: set[int] = set()
    for item in spec.split(","):
        item = item.strip()
        body, step = item, 1
        if "/" in item:
            body, _, step_raw = item.partition("/")
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise ValueError(f"cron: bad step in {name} field: {item!r} "
                                 "(expected a positive number after '/')")
            step = int(step_raw)
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            a, _, b = body.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"cron: bad range in {name} field: {item!r} "
                                 "(expected 'a-b' with numbers)")
            start, end = int(a), int(b)
            if start > end:
                raise ValueError(f"cron: reversed range in {name} field: {item!r}")
        elif body.isdigit():
            if step != 1:
                raise ValueError(f"cron: a step needs '*' or a range before '/' "
                                 f"in {name} field: {item!r}")
            start = end = int(body)
        else:
            raise ValueError(f"cron: invalid value in {name} field: {item!r} "
                             "(expected '*', a number, 'a-b', or '*/n')")
        if start < lo or end > hi:
            raise ValueError(
                f"cron: {item!r} out of range {lo}-{hi} in {name} field")
        values.update(range(start, end + 1, step))
    if name == "day-of-week":
        values = {0 if v == 7 else v for v in values}
    return frozenset(values)


def _parse_cron(expr: str) -> _Cron:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError(
            "cron expression must have 5 fields "
            f"(minute hour day-of-month month day-of-week), got {len(parts)}: {expr!r}")
    sets = [_parse_cron_field(spec, name, lo, hi)
            for spec, (name, lo, hi) in zip(parts, _CRON_FIELDS)]
    return _Cron(minutes=sets[0], hours=sets[1], days=sets[2], months=sets[3],
                 weekdays=sets[4],
                 day_restricted=parts[2] != "*",
                 weekday_restricted=parts[4] != "*")


def validate_cron(expr: str) -> str:
    """Parse-check a 5-field cron expression; returns the whitespace-normalized
    form, or raises ValueError with a message naming the offending field."""
    _parse_cron(expr)
    return " ".join((expr or "").split())


def _cron_day_matches(c: _Cron, t: datetime) -> bool:
    dom = t.day in c.days
    dow = ((t.weekday() + 1) % 7) in c.weekdays  # python Mon=0 -> cron Sun=0
    if c.day_restricted and c.weekday_restricted:
        return dom or dow  # classic cron rule: both restricted means either matches
    return dom and dow  # an unrestricted field is the full set, so the other governs


def next_fire(cron: str, after: datetime) -> datetime:
    """The first minute boundary STRICTLY after `after` that matches `cron`.

    Iterates minute-by-minute (with day/hour skips) in LOCAL naive time, bounded
    to 366 days — an expression that cannot fire within that window (an impossible
    date like Feb 30) raises ValueError."""
    c = _parse_cron(cron)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = t + timedelta(days=_CRON_SEARCH_DAYS)
    while t <= limit:
        if t.month not in c.months:
            year, month = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
            t = datetime(year, month, 1)
            continue
        if not _cron_day_matches(c, t):
            t = datetime(t.year, t.month, t.day) + timedelta(days=1)
            continue
        if t.hour not in c.hours:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        if t.minute not in c.minutes:
            t += timedelta(minutes=1)
            continue
        return t
    raise ValueError(f"cron expression {cron!r} never fires within "
                     f"{_CRON_SEARCH_DAYS} days (impossible date?)")


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


def _normalize_cron(cron: Any) -> str | None:
    """None/blank -> None; anything else must validate as a cron expression."""
    if cron is None or not str(cron).strip():
        return None
    return validate_cron(str(cron))


def next_run_at(row: dict[str, Any]) -> float | None:
    """When this schedule next fires (epoch seconds) — for UI display.

    None when the schedule is disabled (or its cron can never fire). A cron row's
    next run is the first matching minute after its last start (creation when never
    run). A never-run enabled interval schedule is due immediately, so its next run
    is its creation time (already in the past).
    """
    if not row.get("enabled"):
        return None
    last = row.get("last_run_at")
    if row.get("cron"):
        ref = float(last) if last is not None else float(row.get("created_at") or 0.0)
        try:
            return next_fire(row["cron"], datetime.fromtimestamp(ref)).timestamp()
        except ValueError:
            return None
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
        # Additive migration: databases created before cron support lack the
        # column (CREATE TABLE IF NOT EXISTS leaves an existing table untouched).
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(schedules)")}
        if "cron" not in cols:
            self._conn.execute("ALTER TABLE schedules ADD COLUMN cron TEXT")
        self._conn.commit()

    # ---- CRUD ----
    def create(self, *, project: str, prompt: str, title: str | None = None,
               every_hours: float | None = None, cron: str | None = None,
               budget_usd: float = 0.0) -> dict[str, Any]:
        """Create a schedule. `project` is stored as-is — resolution against the
        configured project list is the caller's job (unknown strings are allowed).
        Recurrence is exactly one of `every_hours` (interval) or `cron` (5-field
        expression, validated here)."""
        cron_norm = _normalize_cron(cron)
        if cron_norm is not None and every_hours is not None:
            raise ValueError("provide either every_hours or cron, not both")
        if cron_norm is None and every_hours is None:
            raise ValueError("provide every_hours or a cron expression")
        hours = None if cron_norm is not None else _validate_interval(every_hours)
        if not (prompt or "").strip():
            raise ValueError("prompt must not be empty")
        sid = f"{_slug(title or prompt)}-{secrets.token_hex(3)}"
        self._conn.execute(
            "INSERT INTO schedules(id, project, prompt, title, every_hours, cron, "
            "budget_usd, enabled, last_run_at, last_task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?)",
            (sid, project, prompt, title, hours, cron_norm, float(budget_usd),
             time.time()),
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
        """Change updatable fields (enable/disable, prompt, every_hours, cron, ...).

        A non-empty `cron` takes over recurrence (validated here); `cron=None`
        reverts to the interval — rejected if the row has no every_hours left."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update field(s): {', '.join(sorted(unknown))}")
        if not fields:
            raise ValueError("no fields to update")
        if "cron" in fields:
            fields["cron"] = _normalize_cron(fields["cron"])
        if "every_hours" in fields and fields["every_hours"] is not None:
            fields["every_hours"] = _validate_interval(fields["every_hours"])
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        if "budget_usd" in fields:
            fields["budget_usd"] = float(fields["budget_usd"])
        if "cron" in fields or "every_hours" in fields:
            current = self.get(sid)
            if current is None:
                raise KeyError(f"unknown schedule: {sid}")
            merged_cron = fields.get("cron", current.get("cron"))
            merged_hours = fields.get("every_hours", current.get("every_hours"))
            if merged_cron is None and merged_hours is None:
                raise ValueError("schedule needs every_hours or a cron expression")
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
        """Schedules ready to fire, in creation order.

        Interval rows: enabled AND (never run, or last_run_at + every_hours*3600
        <= now). Cron rows: enabled AND a matching cron minute boundary has passed
        since last_run_at (created_at when never run) — because mark_started()
        advances last_run_at, a cron row can never fire twice within the same
        minute."""
        ts = time.time() if now is None else now
        rows = self._conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 ORDER BY created_at ASC"
        ).fetchall()
        return [d for r in rows if self._is_due(d := self._to_dict(r), ts)]

    @staticmethod
    def _is_due(row: dict[str, Any], ts: float) -> bool:
        last = row.get("last_run_at")
        if row.get("cron"):
            ref = float(last) if last is not None else float(row.get("created_at") or 0.0)
            try:
                fire = next_fire(row["cron"], datetime.fromtimestamp(ref))
            except ValueError:
                # never-firing expression (impossible date): skip, never crash the poll
                return False
            return fire.timestamp() <= ts
        return last is None or float(last) + float(row["every_hours"]) * 3600.0 <= ts

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
