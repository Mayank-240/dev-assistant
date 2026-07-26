"""Tests for the read-only spend analytics layer (src/ai_dev_assistant/analytics.py)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ai_dev_assistant.analytics import (
    cost_per_outcome,
    project_spend,
    run_cost_breakdown,
    spend_overview,
)
from ai_dev_assistant.orchestration.run_store import RunStore


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _ts(days_ago: int) -> float:
    """Midday UTC on the calendar day ``days_ago`` days before today (UTC)."""
    d = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc).timestamp()


def _day(days_ago: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path / "data", docs_dir=tmp_path / "docs")


@pytest.fixture
def store(settings):
    s = RunStore(settings.data_dir / "runs.db")
    yield s
    s.close()


def seed(store, rid, *, days_ago=0, project="alpha", status="completed",
         cost=1.0, tin=0, tout=0, quality=None, passed=None, total=None):
    store.start(rid, f"prompt for {rid}", project=project)
    fields = {"status": status, "cost_usd": cost, "input_tokens": tin,
              "output_tokens": tout}
    if quality is not None:
        fields["quality_score"] = quality
    if passed is not None:
        fields["subtasks_passed"] = passed
    if total is not None:
        fields["subtasks_total"] = total
    store.finish(rid, **fields)
    # Backdate created_at to a deterministic UTC calendar day.
    store._conn.execute("UPDATE runs SET created_at = ? WHERE id = ?",
                        (_ts(days_ago), rid))
    store._conn.commit()


def seed_default_runs(store):
    seed(store, "r1", days_ago=0, project="alpha", status="completed",
         cost=2.5, tin=1000, tout=200)
    seed(store, "r2", days_ago=1, project="alpha", status="failed",
         cost=1.5, tin=500, tout=100)
    seed(store, "r3", days_ago=1, project="beta", status="completed",
         cost=3.0, tin=2000, tout=400, quality=80.0)
    seed(store, "r4", days_ago=2, project="beta", status="completed",
         cost=None, tin=100, tout=10, quality=90.0)


def write_events(settings, task_id, lines):
    d = settings.docs_dir / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text(
        "\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n",
        encoding="utf-8")


# ---------------------------------------------------------------------------
# spend_overview
# ---------------------------------------------------------------------------

def test_overview_totals_tokens_and_missing_cost(settings, store):
    seed_default_runs(store)
    ov = spend_overview(settings, days=30)
    assert ov["runs"] == 4
    assert ov["total_usd"] == pytest.approx(7.0)
    assert ov["tokens_in"] == 3600
    assert ov["tokens_out"] == 710
    assert ov["runs_without_cost"] == 1  # r4's NULL cost counted as 0 but flagged
    assert ov["window_days"] == 30


def test_overview_by_day_math_and_order(settings, store):
    seed_default_runs(store)
    ov = spend_overview(settings, days=30)
    assert [d["date"] for d in ov["by_day"]] == [_day(2), _day(1), _day(0)]
    day1 = ov["by_day"][1]
    assert day1 == {"date": _day(1), "usd": pytest.approx(4.5), "runs": 2}
    assert ov["by_day"][0] == {"date": _day(2), "usd": pytest.approx(0.0), "runs": 1}


def test_overview_by_project_with_avg_quality(settings, store):
    seed_default_runs(store)
    ov = spend_overview(settings, days=30)
    projects = {p["project"]: p for p in ov["by_project"]}
    assert projects["alpha"] == {"project": "alpha", "usd": pytest.approx(4.0),
                                 "runs": 2, "avg_quality": None}
    assert projects["beta"]["usd"] == pytest.approx(3.0)
    assert projects["beta"]["runs"] == 2
    assert projects["beta"]["avg_quality"] == pytest.approx(85.0)
    # sorted by spend, descending
    assert [p["project"] for p in ov["by_project"]] == ["alpha", "beta"]


def test_overview_by_status(settings, store):
    seed_default_runs(store)
    ov = spend_overview(settings, days=30)
    assert ov["by_status"]["completed"] == {"usd": pytest.approx(5.5), "runs": 3}
    assert ov["by_status"]["failed"] == {"usd": pytest.approx(1.5), "runs": 1}


def test_overview_window_filtering(settings, store):
    seed(store, "old", days_ago=40, cost=100.0)
    seed(store, "edge_in", days_ago=29, cost=1.0)
    seed(store, "edge_out", days_ago=30, cost=10.0)
    ov30 = spend_overview(settings, days=30)
    assert ov30["runs"] == 1
    assert ov30["total_usd"] == pytest.approx(1.0)  # only the day-29 run
    ov90 = spend_overview(settings, days=90)
    assert ov90["runs"] == 3
    assert ov90["total_usd"] == pytest.approx(111.0)


def test_overview_missing_db_is_empty_and_creates_nothing(settings):
    ov = spend_overview(settings, days=30)
    assert ov["runs"] == 0 and ov["total_usd"] == 0.0
    assert ov["by_day"] == [] and ov["by_project"] == [] and ov["by_status"] == {}
    assert not (settings.data_dir / "runs.db").exists()  # read-only: never created


# ---------------------------------------------------------------------------
# project_spend
# ---------------------------------------------------------------------------

def test_project_spend_scoped(settings, store):
    seed_default_runs(store)
    ps = project_spend(settings, "alpha", days=90)
    assert ps["project"] == "alpha"
    assert ps["runs"] == 2
    assert ps["total_usd"] == pytest.approx(4.0)
    assert ps["tokens_in"] == 1500
    assert [p["project"] for p in ps["by_project"]] == ["alpha"]
    assert set(ps["by_status"]) == {"completed", "failed"}


def test_project_spend_window_and_unknown_slug(settings, store):
    seed(store, "recent", project="gamma", days_ago=1, cost=2.0)
    seed(store, "ancient", project="gamma", days_ago=200, cost=5.0)
    ps = project_spend(settings, "gamma", days=90)
    assert ps["runs"] == 1 and ps["total_usd"] == pytest.approx(2.0)
    none = project_spend(settings, "no-such-project", days=90)
    assert none["runs"] == 0 and none["total_usd"] == 0.0 and none["by_day"] == []


# ---------------------------------------------------------------------------
# cost_per_outcome
# ---------------------------------------------------------------------------

def test_cost_per_outcome_ratios(settings, store):
    seed(store, "r1", status="completed", cost=4.0, passed=2, total=2)
    seed(store, "r2", status="failed", cost=2.0, passed=1, total=3)
    seed(store, "r3", status="completed", cost=3.0, passed=0, total=1)
    store.set_subtask_review("r1", "s1", "accepted")
    store.set_subtask_review("r1", "s2", "rejected")
    store.set_subtask_review("r2", "s1", "accepted")
    cpo = cost_per_outcome(settings, days=90)
    assert cpo["total_usd"] == pytest.approx(9.0)
    assert cpo["completed_runs"] == 2
    assert cpo["passed_subtasks"] == 3
    assert cpo["accepted_subtasks"] == 2  # rejected decision excluded
    assert cpo["usd_per_completed_run"] == pytest.approx(4.5)
    assert cpo["usd_per_passed_subtask"] == pytest.approx(3.0)
    assert cpo["usd_per_accepted_subtask"] == pytest.approx(4.5)


def test_cost_per_outcome_divide_by_zero_is_none(settings, store):
    seed(store, "r1", status="failed", cost=1.0, passed=0)
    cpo = cost_per_outcome(settings, days=90)
    assert cpo["total_usd"] == pytest.approx(1.0)
    assert cpo["usd_per_completed_run"] is None
    assert cpo["usd_per_passed_subtask"] is None
    assert cpo["usd_per_accepted_subtask"] is None


def test_cost_per_outcome_window_excludes_old_acceptances(settings, store):
    seed(store, "new", status="completed", cost=3.0, passed=1, days_ago=1)
    seed(store, "old", status="completed", cost=9.0, passed=4, days_ago=120)
    store.set_subtask_review("old", "s1", "accepted")
    store.set_subtask_review("new", "s1", "accepted")
    cpo = cost_per_outcome(settings, days=90)
    assert cpo["total_usd"] == pytest.approx(3.0)
    assert cpo["completed_runs"] == 1
    assert cpo["passed_subtasks"] == 1
    assert cpo["accepted_subtasks"] == 1  # the old run's acceptance is outside the window


def test_cost_per_outcome_missing_db(settings):
    cpo = cost_per_outcome(settings, days=90)
    assert cpo["total_usd"] == 0.0
    assert cpo["usd_per_completed_run"] is None
    assert not (settings.data_dir / "runs.db").exists()


# ---------------------------------------------------------------------------
# run_cost_breakdown
# ---------------------------------------------------------------------------

def _breakdown_events():
    return [
        {"type": "subtask_start", "data": {"id": "s1", "agent": "coder"}},
        {"type": "subtask_review",
         "data": {"id": "s1", "passed": False,
                  "cost": {"cost_usd": 0.5, "input_tokens": 100, "output_tokens": 20}}},
        # retry of s1 — costs accumulate
        {"type": "subtask_review",
         "data": {"id": "s1", "passed": True,
                  "cost": {"cost_usd": 0.25, "input_tokens": 50, "output_tokens": 10}}},
        {"type": "subtask_start", "data": {"id": "s2", "agent": "test_engineer"}},
        {"type": "subtask_review",
         "data": {"id": "s2", "passed": True,
                  "cost": {"cost_usd": 0.2, "input_tokens": 40, "output_tokens": 5}}},
        # defensive-parsing fodder
        "this is not json {",
        '["a", "list", "not", "a", "dict"]',
        {"type": "subtask_review"},                       # no data
        {"type": "subtask_review", "data": {"passed": True}},  # no id
        {"type": "subtask_review", "data": {"id": "s3", "cost": None}},  # restored, no cost
        {"type": "status", "message": "unrelated"},
    ]


def test_breakdown_parses_events_and_attributes_agents(settings, store):
    seed(store, "t1", cost=1.2)
    write_events(settings, "t1", _breakdown_events())
    bd = run_cost_breakdown(settings, "t1")
    rows = {r["subtask"]: r for r in bd["subtasks"]}
    assert set(rows) == {"s1", "s2", "s3"}
    assert rows["s1"]["agent"] == "coder"
    assert rows["s1"]["usd"] == pytest.approx(0.75)
    assert rows["s1"]["tokens_in"] == 150 and rows["s1"]["tokens_out"] == 30
    assert rows["s2"]["agent"] == "test_engineer"
    assert rows["s2"]["usd"] == pytest.approx(0.2)
    assert rows["s3"]["agent"] is None
    assert rows["s3"]["usd"] == 0.0  # null cost treated as 0
    assert bd["totals"]["usd"] == pytest.approx(0.95)
    assert bd["totals"]["tokens_in"] == 190
    assert bd["totals"]["tokens_out"] == 35
    assert bd["run_total_usd"] == pytest.approx(1.2)
    assert bd["unattributed_usd"] == pytest.approx(0.25)  # planning/orchestration overhead


def test_breakdown_missing_docs_dir_is_empty(settings, store):
    seed(store, "t1", cost=1.2)
    bd = run_cost_breakdown(settings, "gone")  # docs/gone never existed / was deleted
    assert bd["subtasks"] == []
    assert bd["totals"] == {"usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    assert bd["run_total_usd"] is None and bd["unattributed_usd"] is None


def test_breakdown_without_db_still_parses_events(settings):
    write_events(settings, "t9", _breakdown_events())
    bd = run_cost_breakdown(settings, "t9")
    assert bd["totals"]["usd"] == pytest.approx(0.95)
    assert bd["run_total_usd"] is None and bd["unattributed_usd"] is None
    assert not (settings.data_dir / "runs.db").exists()


# ---------------------------------------------------------------------------
# read-only-ness
# ---------------------------------------------------------------------------

def test_all_db_connections_are_read_only(settings, store, monkeypatch):
    seed(store, "r1", cost=1.0)
    write_events(settings, "r1", _breakdown_events())
    calls = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("ai_dev_assistant.analytics.sqlite3.connect", spy)
    spend_overview(settings, days=30)
    project_spend(settings, "alpha", days=90)
    cost_per_outcome(settings, days=90)
    run_cost_breakdown(settings, "r1")
    assert calls, "analytics should have opened the DB"
    for args, kwargs in calls:
        assert "mode=ro" in str(args[0])
        assert kwargs.get("uri") is True
    # and the data is still fully readable afterwards
    assert store.get("r1")["cost_usd"] == pytest.approx(1.0)
