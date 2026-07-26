"""ScheduleStore: CRUD, due() semantics, interval + cron math, and coexistence with
RunStore on the same runs.db. All offline (sqlite in tmp_path); time is passed
explicitly (cron tests build epochs from local datetimes, so they are TZ-independent)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from ai_dev_assistant.orchestration.run_store import RunStore
from ai_dev_assistant.orchestration.schedules import (
    MIN_INTERVAL_HOURS,
    ScheduleStore,
    next_fire,
    next_run_at,
    validate_cron,
)

T0 = 1_750_000_000.0  # fixed epoch base for deterministic due() math


@pytest.fixture
def store(tmp_path):
    s = ScheduleStore(tmp_path / "runs.db")
    yield s
    s.close()


# ---- create / validate ----

def test_create_returns_full_row_with_defaults(store):
    row = store.create(project="webapp", prompt="nightly dependency audit",
                       every_hours=24, budget_usd=2.5)
    assert row["project"] == "webapp"
    assert row["prompt"] == "nightly dependency audit"
    assert row["title"] is None
    assert row["every_hours"] == 24.0
    assert row["budget_usd"] == 2.5
    assert row["enabled"] is True
    assert row["last_run_at"] is None
    assert row["last_task_id"] is None
    assert row["created_at"] > 0


def test_create_id_is_short_slug_plus_random(store):
    a = store.create(project="p", prompt="Nightly Dependency Audit!", every_hours=24)
    b = store.create(project="p", prompt="Nightly Dependency Audit!", every_hours=24)
    assert a["id"].startswith("nightly-dependency-audit")
    assert a["id"] != b["id"]  # random suffix keeps identical prompts distinct


def test_create_slug_prefers_title(store):
    row = store.create(project="p", prompt="run all the things", title="Dep Audit",
                       every_hours=1)
    assert row["id"].startswith("dep-audit-")


def test_create_rejects_interval_below_minimum(store):
    with pytest.raises(ValueError):
        store.create(project="p", prompt="x", every_hours=0.1)
    # boundary is allowed
    row = store.create(project="p", prompt="x", every_hours=MIN_INTERVAL_HOURS)
    assert row["every_hours"] == MIN_INTERVAL_HOURS


def test_create_rejects_empty_prompt(store):
    with pytest.raises(ValueError):
        store.create(project="p", prompt="   ", every_hours=1)


def test_create_allows_unknown_project_string(store):
    # Project resolution is the caller's job — the store accepts any string.
    row = store.create(project="no-such-project", prompt="audit", every_hours=1)
    assert row["project"] == "no-such-project"


# ---- list / filter ----

def test_list_all_and_filter_by_project(store):
    store.create(project="alpha", prompt="a1", every_hours=1)
    store.create(project="alpha", prompt="a2", every_hours=1)
    store.create(project="beta", prompt="b1", every_hours=1)
    assert len(store.list()) == 3
    alpha = store.list(project="alpha")
    assert len(alpha) == 2
    assert {r["prompt"] for r in alpha} == {"a1", "a2"}
    assert store.list(project="gamma") == []


# ---- update / delete ----

def test_update_fields_and_disable(store):
    row = store.create(project="p", prompt="old", every_hours=24)
    out = store.update(row["id"], prompt="new", every_hours=12, enabled=False,
                       title="Renamed")
    assert out["prompt"] == "new"
    assert out["every_hours"] == 12.0
    assert out["enabled"] is False
    assert out["title"] == "Renamed"


def test_update_validates_interval_and_rejects_unknown_fields(store):
    row = store.create(project="p", prompt="x", every_hours=1)
    with pytest.raises(ValueError):
        store.update(row["id"], every_hours=0.01)
    with pytest.raises(ValueError):
        store.update(row["id"], last_run_at=123.0)  # owned by mark_started
    with pytest.raises(KeyError):
        store.update("nope", prompt="y")


def test_delete_removes_row_and_is_idempotent(store):
    row = store.create(project="p", prompt="x", every_hours=1)
    store.delete(row["id"])
    assert store.get(row["id"]) is None
    assert store.list() == []
    store.delete(row["id"])  # deleting again is a no-op


# ---- due() semantics ----

def test_due_includes_never_run_schedule(store):
    row = store.create(project="p", prompt="x", every_hours=24)
    assert [r["id"] for r in store.due(now=T0)] == [row["id"]]


def test_due_excludes_just_run_until_interval_elapses(store):
    row = store.create(project="p", prompt="x", every_hours=1)
    store.mark_started(row["id"], "task-1", now=T0)
    assert store.due(now=T0) == []
    assert store.due(now=T0 + 3599) == []
    # boundary: last_run_at + every_hours*3600 <= now
    assert [r["id"] for r in store.due(now=T0 + 3600)] == [row["id"]]


def test_due_excludes_disabled_even_when_overdue(store):
    row = store.create(project="p", prompt="x", every_hours=1)
    store.update(row["id"], enabled=False)
    assert store.due(now=T0 + 999_999) == []
    store.update(row["id"], enabled=True)
    assert [r["id"] for r in store.due(now=T0 + 999_999)] == [row["id"]]


def test_mark_started_advances_and_records_task(store):
    row = store.create(project="p", prompt="x", every_hours=2)
    store.mark_started(row["id"], "task-a", now=T0)
    got = store.get(row["id"])
    assert got["last_run_at"] == T0
    assert got["last_task_id"] == "task-a"
    # a later run advances the clock again
    store.mark_started(row["id"], "task-b", now=T0 + 7200)
    got = store.get(row["id"])
    assert got["last_run_at"] == T0 + 7200
    assert got["last_task_id"] == "task-b"
    assert store.due(now=T0 + 7200 + 7199) == []
    assert len(store.due(now=T0 + 7200 + 7200)) == 1


def test_mark_started_unknown_schedule_raises(store):
    with pytest.raises(KeyError):
        store.mark_started("nope", "task-1", now=T0)


# ---- next_run_at ----

def test_next_run_at_math(store):
    row = store.create(project="p", prompt="x", every_hours=6)
    # never run: due immediately — next run is creation time
    assert next_run_at(row) == row["created_at"]
    store.mark_started(row["id"], "t1", now=T0)
    assert next_run_at(store.get(row["id"])) == T0 + 6 * 3600
    store.update(row["id"], enabled=False)
    assert next_run_at(store.get(row["id"])) is None


# ---- cron: parser / validation ----

@pytest.mark.parametrize("expr", [
    "* * * * *",
    "*/15 * * * *",
    "0 9 * * 1-5",
    "30 2 1 * *",
    "0 0,12 * * *",
    "0 12 * * 7",        # 7 = Sunday alias
    "0-30/10 * * * *",   # step on a range
    "15 8,18 1-7 */3 1", # everything at once
])
def test_validate_cron_accepts(expr):
    assert validate_cron(expr) == expr


def test_validate_cron_normalizes_whitespace():
    assert validate_cron("  0  9 * *   1-5 ") == "0 9 * * 1-5"


@pytest.mark.parametrize("expr", [
    "",                # no fields
    "* * * *",         # 4 fields
    "* * * * * *",     # 6 fields
    "60 * * * *",      # minute out of range
    "* 24 * * *",      # hour out of range
    "* * 0 * *",       # day-of-month starts at 1
    "* * 32 * *",
    "* * * 13 *",      # month out of range
    "* * * * 8",       # day-of-week tops out at 7
    "a * * * *",       # not a number
    "*/0 * * * *",     # zero step
    "*/x * * * *",     # non-numeric step
    "5/2 * * * *",     # step needs * or a range
    "5-1 * * * *",     # reversed range
    "1- * * * *",      # dangling range
    "1,,2 * * * *",    # empty list item
])
def test_validate_cron_rejects(expr):
    with pytest.raises(ValueError):
        validate_cron(expr)


def test_validate_cron_errors_name_the_field():
    with pytest.raises(ValueError, match="minute"):
        validate_cron("61 * * * *")
    with pytest.raises(ValueError, match="day-of-week"):
        validate_cron("* * * * 8")
    with pytest.raises(ValueError, match="5 fields"):
        validate_cron("* * *")


# ---- cron: next_fire ----

def test_next_fire_quarter_hours_and_strictly_after():
    assert next_fire("*/15 * * * *", datetime(2026, 3, 4, 9, 7)) == datetime(2026, 3, 4, 9, 15)
    # a boundary equal to `after` is skipped: strictly after
    assert next_fire("*/15 * * * *", datetime(2026, 3, 4, 9, 15)) == datetime(2026, 3, 4, 9, 30)
    # seconds are truncated, not rounded past a boundary
    assert next_fire("*/15 * * * *", datetime(2026, 3, 4, 9, 14, 59)) == datetime(2026, 3, 4, 9, 15)


def test_next_fire_weekday_mornings():
    # 2026-03-06 is a Friday: after 09:00 Friday the next weekday 9am is Monday the 9th
    assert next_fire("0 9 * * 1-5", datetime(2026, 3, 6, 10, 0)) == datetime(2026, 3, 9, 9, 0)
    assert next_fire("0 9 * * 1-5", datetime(2026, 3, 6, 8, 59)) == datetime(2026, 3, 6, 9, 0)


def test_next_fire_monthly_and_boundaries():
    assert next_fire("30 2 1 * *", datetime(2026, 1, 15, 12, 0)) == datetime(2026, 2, 1, 2, 30)
    # short-month skip: no Feb 31 — next 31st after Feb 5 is Mar 31
    assert next_fire("0 0 31 * *", datetime(2026, 2, 5, 0, 0)) == datetime(2026, 3, 31, 0, 0)
    # year boundary
    assert next_fire("0 0 1 1 *", datetime(2026, 6, 1, 0, 0)) == datetime(2027, 1, 1, 0, 0)
    # leap day within the 366-day window
    assert next_fire("0 0 29 2 *", datetime(2027, 6, 1, 0, 0)) == datetime(2028, 2, 29, 0, 0)


def test_next_fire_sunday_spelled_0_and_7():
    # 2026-03-07 is a Saturday; the 8th is a Sunday
    assert next_fire("0 12 * * 7", datetime(2026, 3, 7, 13, 0)) == datetime(2026, 3, 8, 12, 0)
    assert next_fire("0 12 * * 0", datetime(2026, 3, 7, 13, 0)) == datetime(2026, 3, 8, 12, 0)


def test_next_fire_dom_and_dow_both_restricted_is_or():
    # classic cron rule: 15th OR Friday. From Wed 2026-03-11, Friday the 13th wins.
    assert next_fire("0 0 15 * 5", datetime(2026, 3, 11, 0, 0)) == datetime(2026, 3, 13, 0, 0)
    # and from the 14th, the 15th (a Sunday) wins over the next Friday
    assert next_fire("0 0 15 * 5", datetime(2026, 3, 14, 0, 0)) == datetime(2026, 3, 15, 0, 0)


def test_next_fire_impossible_date_raises():
    with pytest.raises(ValueError, match="never fires"):
        next_fire("0 0 30 2 *", datetime(2026, 1, 1))


# ---- cron: store rows ----

def test_create_with_cron(store):
    row = store.create(project="p", prompt="daily digest", cron="0 9 * * *")
    assert row["cron"] == "0 9 * * *"
    assert row["every_hours"] is None
    assert row["enabled"] is True
    assert row["last_run_at"] is None


def test_create_normalizes_cron_whitespace(store):
    row = store.create(project="p", prompt="x", cron="  0  9 * * 1-5 ")
    assert row["cron"] == "0 9 * * 1-5"


def test_create_requires_exactly_one_recurrence(store):
    with pytest.raises(ValueError, match="every_hours or a cron"):
        store.create(project="p", prompt="x")
    with pytest.raises(ValueError, match="not both"):
        store.create(project="p", prompt="x", every_hours=1, cron="0 9 * * *")


def test_create_rejects_invalid_cron_with_clear_message(store):
    with pytest.raises(ValueError, match="minute"):
        store.create(project="p", prompt="x", cron="61 * * * *")
    with pytest.raises(ValueError, match="5 fields"):
        store.create(project="p", prompt="x", cron="* * *")


def test_update_switches_between_cron_and_interval(store):
    row = store.create(project="p", prompt="x", every_hours=1)
    out = store.update(row["id"], cron="*/30 * * * *")
    assert out["cron"] == "*/30 * * * *"
    assert out["every_hours"] == 1.0  # kept; cron governs while present
    out = store.update(row["id"], cron=None)  # revert to the interval
    assert out["cron"] is None
    assert out["every_hours"] == 1.0


def test_update_rejects_invalid_cron_and_clearing_both(store):
    row = store.create(project="p", prompt="x", cron="0 9 * * *")
    with pytest.raises(ValueError, match="minute"):
        store.update(row["id"], cron="61 * * * *")
    with pytest.raises(ValueError, match="every_hours or a cron"):
        store.update(row["id"], cron=None)  # no interval to fall back to
    assert store.get(row["id"])["cron"] == "0 9 * * *"  # row untouched


# ---- cron: due() semantics ----

def test_due_cron_row_is_not_due_at_creation(store):
    row = store.create(project="p", prompt="x", cron="* * * * *")
    # the first boundary is strictly after created_at, so never due immediately
    assert store.due(now=row["created_at"]) == []


def test_due_cron_fires_once_a_boundary_passes(store):
    row = store.create(project="p", prompt="x", cron="*/15 * * * *")
    boundary = next_fire("*/15 * * * *",
                         datetime.fromtimestamp(row["created_at"])).timestamp()
    assert store.due(now=boundary - 1) == []
    assert [r["id"] for r in store.due(now=boundary)] == [row["id"]]
    # still due until someone marks it started
    assert [r["id"] for r in store.due(now=boundary + 300)] == [row["id"]]


def test_due_cron_never_double_fires_within_the_same_minute(store):
    row = store.create(project="p", prompt="x", cron="* * * * *")
    fire = next_fire("* * * * *",
                     datetime.fromtimestamp(row["created_at"])).timestamp()
    store.mark_started(row["id"], "t1", now=fire + 5.0)  # started mid-minute
    assert store.due(now=fire + 5.0) == []
    assert store.due(now=fire + 59.0) == []   # same cron minute: not due again
    assert [r["id"] for r in store.due(now=fire + 65.0)] == [row["id"]]  # next minute


def test_due_cron_daily_boundary(store):
    row = store.create(project="p", prompt="x", cron="0 9 * * *")
    last = datetime(2026, 3, 4, 9, 0).timestamp()
    store.mark_started(row["id"], "t1", now=last)
    next_9am = datetime(2026, 3, 5, 9, 0).timestamp()
    assert store.due(now=next_9am - 60) == []
    assert [r["id"] for r in store.due(now=next_9am)] == [row["id"]]


def test_due_mixes_cron_and_interval_rows(store):
    interval = store.create(project="p", prompt="interval", every_hours=1)
    cron = store.create(project="p", prompt="cron", cron="0 9 * * *")
    store.mark_started(interval["id"], "t1", now=datetime(2026, 3, 4, 9, 30).timestamp())
    store.mark_started(cron["id"], "t2", now=datetime(2026, 3, 4, 9, 30).timestamp())
    at_11 = datetime(2026, 3, 4, 11, 0).timestamp()
    assert {r["id"] for r in store.due(now=at_11)} == {interval["id"]}
    next_day = datetime(2026, 3, 5, 9, 0).timestamp()
    assert {r["id"] for r in store.due(now=next_day)} == {interval["id"], cron["id"]}


def test_next_run_at_cron(store):
    row = store.create(project="p", prompt="x", cron="0 9 * * *")
    store.mark_started(row["id"], "t", now=datetime(2026, 3, 4, 10, 0).timestamp())
    assert next_run_at(store.get(row["id"])) == datetime(2026, 3, 5, 9, 0).timestamp()
    store.update(row["id"], enabled=False)
    assert next_run_at(store.get(row["id"])) is None


# ---- cron: additive migration ----

_OLD_SCHEMA = """
CREATE TABLE schedules (
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


def test_migration_adds_cron_column_and_old_rows_still_work(tmp_path):
    path = tmp_path / "runs.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO schedules VALUES ('old-1', 'p', 'audit', NULL, 1.0, 0.0, 1, "
        "NULL, NULL, ?)", (T0,))
    conn.commit()
    conn.close()

    store = ScheduleStore(path)
    try:
        got = store.get("old-1")
        assert got["cron"] is None          # migrated column, NULL for old rows
        assert got["every_hours"] == 1.0
        # the old interval row still schedules exactly as before
        assert [r["id"] for r in store.due(now=T0 + 10)] == ["old-1"]
        store.mark_started("old-1", "t1", now=T0 + 10)
        assert store.due(now=T0 + 10 + 3599) == []
        assert len(store.due(now=T0 + 10 + 3600)) == 1
        # and a cron row can live alongside it in the migrated table
        row = store.create(project="p", prompt="digest", cron="0 9 * * *")
        assert store.get(row["id"])["cron"] == "0 9 * * *"
    finally:
        store.close()


def test_cron_persists_across_reopen(tmp_path):
    path = tmp_path / "runs.db"
    s1 = ScheduleStore(path)
    row = s1.create(project="p", prompt="digest", cron="0 9 * * 1-5")
    s1.close()
    s2 = ScheduleStore(path)
    try:
        got = s2.get(row["id"])
        assert got["cron"] == "0 9 * * 1-5"
        assert got["every_hours"] is None
    finally:
        s2.close()


# ---- coexistence with RunStore on the same db file ----

def test_coexists_with_runstore_on_same_db(tmp_path):
    path = tmp_path / "runs.db"
    runs = RunStore(path)
    sched = ScheduleStore(path)
    try:
        runs.start("run-1", "do a thing", project="p")
        row = sched.create(project="p", prompt="nightly audit", every_hours=24)
        sched.mark_started(row["id"], "run-1", now=T0)
        # both stores read back their own data with no clashes
        assert runs.get("run-1")["status"] == "running"
        assert sched.get(row["id"])["last_task_id"] == "run-1"
        # RunStore's queue still works alongside the schedules table
        runs.enqueue("run-2", "queued thing", None, {"prompt": "queued thing"})
        assert runs.queue_next()["task_id"] == "run-2"
    finally:
        sched.close()
        runs.close()


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "runs.db"
    s1 = ScheduleStore(path)
    row = s1.create(project="p", prompt="audit", title="Audit", every_hours=12,
                    budget_usd=1.0)
    s1.mark_started(row["id"], "task-9", now=T0)
    s1.close()

    s2 = ScheduleStore(path)
    try:
        got = s2.get(row["id"])
        assert got is not None
        assert got["prompt"] == "audit"
        assert got["title"] == "Audit"
        assert got["every_hours"] == 12.0
        assert got["budget_usd"] == 1.0
        assert got["enabled"] is True
        assert got["last_run_at"] == T0
        assert got["last_task_id"] == "task-9"
        assert next_run_at(got) == T0 + 12 * 3600
    finally:
        s2.close()
