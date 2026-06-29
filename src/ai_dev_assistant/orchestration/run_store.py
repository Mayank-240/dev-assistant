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
        self._conn.executescript(_SCHEMA)
        # migrate older DBs that predate newer columns
        for col, decl in (("title", "TEXT"), ("kg_nodes", "INTEGER"), ("kg_edges", "INTEGER"),
                          ("memories", "INTEGER"), ("messages", "INTEGER"),
                          ("quality_score", "REAL"), ("run_status", "TEXT"),
                          ("parent_id", "TEXT")):
            try:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        self._conn.commit()

    def start(self, run_id: str, prompt: str, title: str | None = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO runs(id, prompt, title, status, created_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (run_id, prompt, title or derive_title(prompt), time.time()),
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
        """Link a run to the task it continues (re-engagement chain)."""
        self._conn.execute("UPDATE runs SET parent_id = ? WHERE id = ?", (parent_id, run_id))
        self._conn.commit()

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

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, run_id: str) -> None:
        self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        self._conn.execute("DELETE FROM queue WHERE task_id = ?", (run_id,))
        self._conn.execute("DELETE FROM feedback WHERE run_id = ?", (run_id,))
        self._conn.execute("DELETE FROM agent_outcomes WHERE run_id = ?", (run_id,))
        self._conn.commit()

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
        """Record a queued run (status 'queued') and append it to the pending queue."""
        self._conn.execute(
            "INSERT OR REPLACE INTO runs(id, prompt, title, status, created_at) "
            "VALUES (?, ?, ?, 'queued', ?)",
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
