"""Tests for orchestration/run_store.py — run lifecycle, queue semantics, feedback,
agent track record, and schema-migration idempotence. All offline (sqlite in tmp_path)."""

from __future__ import annotations

import pytest

from ai_dev_assistant.orchestration.run_store import RunStore, derive_title


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs.db")
    yield s
    s.close()


# ---- derive_title ----
def test_derive_title_capitalizes_and_truncates():
    assert derive_title("") == "Untitled task"
    assert derive_title("fix the   bug") == "Fix the bug"
    long = "add " + "word " * 40
    t = derive_title(long)
    assert t.endswith("…") and len(t) <= 60


# ---- start/finish lifecycle ----
def test_start_finish_lifecycle(store):
    store.start("r1", "do a thing")
    row = store.get("r1")
    assert row["status"] == "running"
    assert row["title"] == "Do a thing"
    assert row["created_at"] is not None and row["ended_at"] is None

    store.finish("r1", subtasks_total=3, subtasks_passed=2, cost_usd=0.12, tests="passed")
    row = store.get("r1")
    assert row["status"] == "completed"
    assert row["ended_at"] is not None
    assert row["subtasks_total"] == 3 and row["subtasks_passed"] == 2
    assert row["cost_usd"] == pytest.approx(0.12)
    assert row["tests"] == "passed"


def test_finish_honors_explicit_status(store):
    store.start("r1", "p")
    store.finish("r1", status="failed")
    assert store.get("r1")["status"] == "failed"


def test_set_status_sets_ended_at_only_when_terminal(store):
    store.start("r1", "p")
    store.set_status("r1", "planning")
    assert store.get("r1")["ended_at"] is None
    store.set_status("r1", "cancelled")
    assert store.get("r1")["ended_at"] is not None


def test_get_missing_and_delete(store):
    assert store.get("nope") is None
    store.start("r1", "p")
    store.delete("r1")
    assert store.get("r1") is None


# ---- interrupt_orphans ----
def test_interrupt_orphans_marks_only_running(store):
    store.start("dead", "was running when process died")
    store.start("done", "finished cleanly")
    store.finish("done")
    store.interrupt_orphans()
    assert store.get("dead")["status"] == "interrupted"
    assert store.get("dead")["ended_at"] is not None
    assert store.get("done")["status"] == "completed"


# ---- queue semantics ----
def test_enqueue_records_queued_run_and_fifo_order(store):
    for tid in ("a", "b", "c"):
        store.enqueue(tid, f"prompt {tid}", None, {"prompt": f"prompt {tid}"})
    assert store.get("a")["status"] == "queued"
    pending = store.queue_pending()
    assert [p["task_id"] for p in pending] == ["a", "b", "c"]
    assert store.queue_positions() == {"a": 1, "b": 2, "c": 3}

    nxt = store.queue_next()
    assert nxt["task_id"] == "a" and nxt["payload"] == {"prompt": "prompt a"}
    assert [p["task_id"] for p in store.queue_pending()] == ["b", "c"]


def test_queue_next_empty_returns_none(store):
    assert store.queue_next() is None


def test_queue_promote_moves_task_to_front(store):
    for tid in ("a", "b", "c"):
        store.enqueue(tid, tid, None, {})
    store.queue_promote("c")
    assert [p["task_id"] for p in store.queue_pending()] == ["c", "a", "b"]
    assert store.queue_next()["task_id"] == "c"


def test_queue_reorder(store):
    for tid in ("a", "b", "c"):
        store.enqueue(tid, tid, None, {})
    store.queue_reorder(["b", "c", "a"])
    assert [p["task_id"] for p in store.queue_pending()] == ["b", "c", "a"]
    assert store.queue_positions() == {"b": 1, "c": 2, "a": 3}


def test_queue_remove(store):
    for tid in ("a", "b"):
        store.enqueue(tid, tid, None, {})
    store.queue_remove("a")
    assert [p["task_id"] for p in store.queue_pending()] == ["b"]


# ---- feedback roundtrip ----
def test_feedback_roundtrip_and_replace(store):
    store.start("r1", "p")
    store.set_feedback("r1", rating=4, accepted=True, comment="nice work")
    fb = store.get_feedback("r1")
    assert fb["rating"] == 4 and fb["accepted"] == 1 and fb["comment"] == "nice work"

    # re-submitting replaces the previous feedback for the same run
    store.set_feedback("r1", rating=2, accepted=False, comment="changed my mind")
    fb = store.get_feedback("r1")
    assert fb["rating"] == 2 and fb["accepted"] == 0

    assert store.get_feedback("unknown") is None


def test_feedback_accepts_none_fields(store):
    store.set_feedback("r1", comment="only a comment")
    fb = store.get_feedback("r1")
    assert fb["rating"] is None and fb["accepted"] is None


# ---- agent track record ----
def test_agent_track_record_math(store):
    store.record_agent_outcome("r1", "coder", True, 90)
    store.record_agent_outcome("r1", "coder", True, 85)
    store.record_agent_outcome("r2", "coder", False, 40)
    store.record_agent_outcome("r2", "reviewer", True, None)
    tr = store.agent_track_record()
    assert tr["coder"] == {"n": 3, "passed": 2, "pass_rate": round(2 / 3, 3)}
    assert tr["reviewer"] == {"n": 1, "passed": 1, "pass_rate": 1.0}


def test_agent_track_record_empty(store):
    assert store.agent_track_record() == {}


# ---- agent stats (roster pre-filtering priors) ----
def test_agent_stats_aggregate_and_per_project(store):
    store.start("r1", "p", project="alpha")
    store.start("r2", "p", project="beta")
    store.record_agent_outcome("r1", "coder", True, 90)
    store.record_agent_outcome("r1", "coder", False, 40)
    store.record_agent_outcome("r2", "coder", True, 80)
    store.record_agent_outcome("r2", "devops", True, None)  # score-less outcome

    stats = store.agent_stats()
    assert stats["coder"] == {"n": 3, "passed": 2, "avg_score": 70.0}
    assert stats["devops"] == {"n": 1, "passed": 1, "avg_score": 0.0}

    alpha = store.agent_stats(project="alpha")
    assert alpha["coder"] == {"n": 2, "passed": 1, "avg_score": 65.0}
    assert "devops" not in alpha           # beta's outcomes don't leak in
    assert store.agent_stats(project="nope") == {}
    assert store.agent_stats() != {}       # aggregate default unaffected


def test_agent_stats_exact_signature_for_getattr_callers(store):
    """A concurrent server calls this via getattr with a fallback — the exact
    signature (project=None keyword) is load-bearing."""
    import inspect

    fn = getattr(store, "agent_stats", None)
    assert callable(fn)
    sig = inspect.signature(RunStore.agent_stats)
    assert list(sig.parameters) == ["self", "project"]
    assert sig.parameters["project"].default is None
    assert fn() == {}  # empty store: still a dict


def test_agent_recent_project_outcomes_newest_first_and_scoped(store):
    store.start("r1", "p", project="alpha")
    store.start("r2", "p", project="beta")
    for passed in (True, False, False, False):  # oldest -> newest
        store.record_agent_outcome("r1", "coder", passed, 50)
    store.record_agent_outcome("r2", "coder", True, 90)  # other project: ignored

    recent = store.agent_recent_project_outcomes("alpha")
    assert recent["coder"] == [False, False, False]  # last 3, newest first
    assert store.agent_recent_project_outcomes("beta")["coder"] == [True]
    assert store.agent_recent_project_outcomes("empty") == {}


# ---- schema migration idempotence ----
def test_reopening_same_db_is_idempotent_and_preserves_data(tmp_path):
    path = tmp_path / "runs.db"
    s1 = RunStore(path)
    s1.start("r1", "persisted run")
    s1.enqueue("q1", "queued", None, {"k": "v"})
    s1.set_feedback("r1", rating=5)
    s1.close()

    # opening again re-runs the schema + ALTER-TABLE migrations; must not raise
    s2 = RunStore(path)
    row = s2.get("r1")
    assert row["prompt"] == "persisted run"
    # migrated columns exist on rows read back
    for col in ("title", "quality_score", "run_status", "parent_id"):
        assert col in row
    assert [p["task_id"] for p in s2.queue_pending()] == ["q1"]
    assert s2.get_feedback("r1")["rating"] == 5
    s2.close()

    # and a third open still works
    RunStore(path).close()
