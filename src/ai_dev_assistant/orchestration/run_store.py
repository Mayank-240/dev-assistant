"""SQLite persistence for task runs — survives restarts and powers the run history.

Stores one row per run: status, timings, subtask counts, test outcome, token/cost usage,
session stats, and the brief summary.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    prompt TEXT,
    title TEXT,
    status TEXT,
    created_at REAL,
    ended_at REAL,
    subtasks_total INTEGER,
    subtasks_passed INTEGER,
    tests TEXT,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    sessions_spawned INTEGER,
    sessions_reaped INTEGER,
    kg_nodes INTEGER,
    kg_edges INTEGER,
    memories INTEGER,
    messages INTEGER,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS queue (
    task_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    position INTEGER NOT NULL,
    enqueued_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    run_id TEXT PRIMARY KEY,
    rating INTEGER,
    accepted INTEGER,
    comment TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subtask_states (
    run_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    status TEXT,
    attempts INTEGER,
    result TEXT,
    verdict TEXT,
    error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, subtask_id)
);

CREATE TABLE IF NOT EXISTS subtask_reviews (
    run_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, subtask_id)
);

CREATE TABLE IF NOT EXISTS agent_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    agent TEXT NOT NULL,
    passed INTEGER NOT NULL,
    score INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_agent ON agent_outcomes(agent);
"""

_TERMINAL = {"completed", "partial", "failed", "cancelled", "over_budget", "interrupted"}


def derive_title(prompt: str) -> str:
    """A short human-readable title from the task prompt (no LLM)."""
    p = " ".join((prompt or "").split())
    if not p:
        return "Untitled task"
    p = p[0].upper() + p[1:]
    if len(p) <= 56:
        return p
    return p[:56].rsplit(" ", 1)[0] + "…"


class RunStore:
    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Concurrent runs / web workers share this file — WAL + a busy timeout stop
        # "database is locked" races (R4).
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.OperationalError:
            pass
        self._conn.executescript(_SCHEMA)
        # migrate older DBs that predate newer columns
        for col, decl in (("title", "TEXT"), ("kg_nodes", "INTEGER"), ("kg_edges", "INTEGER"),
                          ("memories", "INTEGER"), ("messages", "INTEGER"),
                          ("quality_score", "REAL"), ("run_status", "TEXT"),
                          ("parent_id", "TEXT"), ("plan_json", "TEXT"),
                          ("project", "TEXT"), ("task_branch", "TEXT"),
                          ("review_target", "TEXT")):
            try:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        for col, decl in (("merge_commit", "TEXT"), ("changed", "TEXT")):
            try:
                self._conn.execute(f"ALTER TABLE subtask_states ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        self._conn.commit()

    def start(self, run_id: str, prompt: str, title: str | None = None,
              project: str | None = None) -> None:
        # UPSERT, never REPLACE: a resume/re-start must not wipe the row's other
        # columns (plan_json, parent_id, task_branch, …) — REPLACE deletes them.
        self._conn.execute(
            "INSERT INTO runs(id, prompt, title, status, created_at, project) "
            "VALUES (?, ?, ?, 'running', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET prompt=excluded.prompt, title=excluded.title, "
            "status='running', created_at=excluded.created_at, project=excluded.project",
            (run_id, prompt, title or derive_title(prompt), time.time(), project or "default"),
        )
        self._conn.commit()

    def interrupt_orphans(self) -> None:
        """On startup, any run still marked 'running' lost its process — mark it interrupted."""
        self._conn.execute(
            "UPDATE runs SET status = 'interrupted', ended_at = ? WHERE status = 'running'",
            (time.time(),),
        )
        self._conn.commit()

    def set_parent(self, run_id: str, parent_id: str) -> None:
        """Link a run to the task it continues (re-engagement / fan-out lineage)."""
        self._conn.execute("UPDATE runs SET parent_id = ? WHERE id = ?", (parent_id, run_id))
        self._conn.commit()

    def children_of(self, parent_id: str) -> list[dict[str, Any]]:
        """Child runs of a cross-project parent (or re-engagement descendants)."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE parent_id = ? ORDER BY created_at ASC",
            (parent_id,)).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, run_id: str, status: str) -> None:
        ended = time.time() if status in _TERMINAL else None
        self._conn.execute(
            "UPDATE runs SET status = ?, ended_at = COALESCE(?, ended_at) WHERE id = ?",
            (status, ended, run_id),
        )
        self._conn.commit()

    def finish(self, run_id: str, **fields: Any) -> None:
        fields.setdefault("status", "completed")
        fields["ended_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
        self._conn.commit()

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 100, project: str | None = None) -> list[dict[str, Any]]:
        if project:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE COALESCE(project,'default') = ? "
                "ORDER BY created_at DESC LIMIT ?", (project, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete(self, run_id: str) -> None:
        self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        self._conn.execute("DELETE FROM queue WHERE task_id = ?", (run_id,))
        self._conn.execute("DELETE FROM feedback WHERE run_id = ?", (run_id,))
        self._conn.execute("DELETE FROM agent_outcomes WHERE run_id = ?", (run_id,))
        self._conn.execute("DELETE FROM subtask_states WHERE run_id = ?", (run_id,))
        self._conn.execute("DELETE FROM subtask_reviews WHERE run_id = ?", (run_id,))
        self._conn.commit()

    # ---- checkpointing (R1): per-subtask state survives interruption ----
    def save_plan(self, run_id: str, plan_json: str) -> None:
        self._conn.execute("UPDATE runs SET plan_json = ? WHERE id = ?", (plan_json, run_id))
        self._conn.commit()

    def get_plan(self, run_id: str) -> str | None:
        row = self._conn.execute("SELECT plan_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row["plan_json"] if row and row["plan_json"] else None

    def checkpoint_subtask(self, run_id: str, subtask_id: str, *, status: str,
                           attempts: int, result: str, verdict_json: str | None,
                           error: str = "", merge_commit: str = "",
                           changed_json: str | None = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO subtask_states"
            "(run_id, subtask_id, status, attempts, result, verdict, error, updated_at, "
            "merge_commit, changed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, subtask_id, status, attempts, result, verdict_json, error, time.time(),
             merge_commit, changed_json),
        )
        self._conn.commit()

    def set_run_branch(self, run_id: str, branch: str, target: str) -> None:
        """Record the task's delivery branch and the branch acceptance merges into."""
        self._conn.execute("UPDATE runs SET task_branch = ?, review_target = ? WHERE id = ?",
                           (branch, target, run_id))
        self._conn.commit()

    # ---- per-subtask review decisions (F4/decision #5) ----
    def set_subtask_review(self, run_id: str, subtask_id: str, decision: str,
                           comment: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO subtask_reviews(run_id, subtask_id, decision, comment, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, subtask_id, decision, comment, time.time()),
        )
        self._conn.commit()

    def get_subtask_reviews(self, run_id: str) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM subtask_reviews WHERE run_id = ?", (run_id,)).fetchall()
        return {r["subtask_id"]: dict(r) for r in rows}

    def get_subtask_states(self, run_id: str) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM subtask_states WHERE run_id = ?", (run_id,)).fetchall()
        return {r["subtask_id"]: dict(r) for r in rows}

    # ---- human feedback (Tier 4) ----
    def set_feedback(self, run_id: str, *, rating: int | None = None,
                     accepted: bool | None = None, comment: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO feedback(run_id, rating, accepted, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, rating, None if accepted is None else int(accepted), comment, time.time()),
        )
        self._conn.commit()

    def get_feedback(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM feedback WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def recent_feedback(self, limit: int = 5) -> list[dict[str, Any]]:
        """Latest human feedback joined with its run — consumed at plan time (M3)."""
        rows = self._conn.execute(
            "SELECT f.run_id, f.rating, f.accepted, f.comment, r.prompt "
            "FROM feedback f LEFT JOIN runs r ON r.id = f.run_id "
            "ORDER BY f.created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- learned routing signal (Tier 4) ----
    def record_agent_outcome(self, run_id: str, agent: str, passed: bool, score: int | None) -> None:
        self._conn.execute(
            "INSERT INTO agent_outcomes(run_id, agent, passed, score, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, agent, int(passed), score, time.time()),
        )
        self._conn.commit()

    def agent_track_record(self) -> dict[str, dict[str, float | int]]:
        rows = self._conn.execute(
            "SELECT agent, COUNT(*) n, SUM(passed) p FROM agent_outcomes GROUP BY agent"
        ).fetchall()
        out: dict[str, dict[str, float | int]] = {}
        for r in rows:
            n, p = int(r["n"]), int(r["p"] or 0)
            out[r["agent"]] = {"n": n, "passed": p, "pass_rate": round(p / n, 3) if n else 0.0}
        return out

    def quality_trend(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, created_at, quality_score, status FROM runs "
            "WHERE quality_score IS NOT NULL ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- task queue ----
    def enqueue(self, task_id: str, prompt: str, title: str | None, payload: dict[str, Any]) -> None:
        """Record a queued run (status 'queued') and append it to the pending queue.

        UPSERT, never REPLACE: re-enqueueing an existing run (resume) must keep its
        plan_json/branch/lineage columns — REPLACE silently wiped them, which made a
        resumed run re-plan from scratch.
        """
        self._conn.execute(
            "INSERT INTO runs(id, prompt, title, status, created_at) "
            "VALUES (?, ?, ?, 'queued', ?) "
            "ON CONFLICT(id) DO UPDATE SET prompt=excluded.prompt, title=excluded.title, "
            "status='queued'",
            (task_id, prompt, title or derive_title(prompt), time.time()),
        )
        nxt = self._conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM queue").fetchone()[0]
        self._conn.execute(
            "INSERT OR REPLACE INTO queue(task_id, payload, position, enqueued_at) VALUES (?, ?, ?, ?)",
            (task_id, json.dumps(payload), nxt, time.time()),
        )
        self._conn.commit()

    def queue_pending(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT q.task_id, q.position, q.payload, r.title, r.prompt "
            "FROM queue q LEFT JOIN runs r ON r.id = q.task_id ORDER BY q.position ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def queue_next(self) -> dict[str, Any] | None:
        """Pop the front of the queue (lowest position) and return its payload."""
        row = self._conn.execute(
            "SELECT task_id, payload FROM queue ORDER BY position ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self._conn.execute("DELETE FROM queue WHERE task_id = ?", (row["task_id"],))
        self._conn.commit()
        return {"task_id": row["task_id"], "payload": json.loads(row["payload"])}

    def queue_remove(self, task_id: str) -> None:
        self._conn.execute("DELETE FROM queue WHERE task_id = ?", (task_id,))
        self._conn.commit()

    def queue_promote(self, task_id: str) -> None:
        front = self._conn.execute("SELECT COALESCE(MIN(position), 1) - 1 FROM queue").fetchone()[0]
        self._conn.execute("UPDATE queue SET position = ? WHERE task_id = ?", (front, task_id))
        self._conn.commit()

    def queue_reorder(self, order: list[str]) -> None:
        for i, tid in enumerate(order):
            self._conn.execute("UPDATE queue SET position = ? WHERE task_id = ?", (i, tid))
        self._conn.commit()

    def queue_positions(self) -> dict[str, int]:
        """task_id -> 1-based display position, in queue order."""
        rows = self._conn.execute(
            "SELECT task_id FROM queue ORDER BY position ASC"
        ).fetchall()
        return {r["task_id"]: i + 1 for i, r in enumerate(rows)}

    def close(self) -> None:
        self._conn.close()
