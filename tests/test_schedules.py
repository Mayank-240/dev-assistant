"""ScheduleStore: CRUD, due() semantics, interval math, and coexistence with RunStore
on the same runs.db. All offline (sqlite in tmp_path); time is passed explicitly."""

from __future__ import annotations

import pytest

from ai_dev_assistant.orchestration.run_store import RunStore
from ai_dev_assistant.orchestration.schedules import (
    MIN_INTERVAL_HOURS,
    ScheduleStore,
    next_run_at,
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
