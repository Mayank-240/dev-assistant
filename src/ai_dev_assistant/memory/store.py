"""SQLite-backed memory + shared blackboard.

- Memory entries are scoped (e.g. per task or "longterm") and embedded for semantic
  recall via the VectorStore.
- The blackboard is a small shared key/value space agents use to hand intermediate
  facts to one another.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from .embeddings import _tokenize, get_embedder
from .vector import VectorStore

_HALF_LIFE_S = 14 * 24 * 3600.0  # recall relevance halves after ~two weeks
_DEFAULT_MAX_ENTRIES = 5000      # growth cap per store; override via ADA_MEMORY_MAX_ENTRIES


def _max_entries() -> int:
    # Read at call time (not import time) so tests/ops can adjust without a restart.
    # ADA_-prefixed knob with a safe default; 0 or negative disables the cap.
    try:
        return int(os.getenv("ADA_MEMORY_MAX_ENTRIES", "") or _DEFAULT_MAX_ENTRIES)
    except ValueError:
        return _DEFAULT_MAX_ENTRIES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    key TEXT,
    content TEXT NOT NULL,
    metadata TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(scope);

CREATE TABLE IF NOT EXISTS blackboard (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    author TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vectors (
    namespace TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    text TEXT,
    PRIMARY KEY (namespace, ref_id)
);
"""


@dataclass
class MemoryEntry:
    id: int
    scope: str
    key: str | None
    content: str
    metadata: dict[str, Any]
    created_at: float
    score: float | None = None


class MemoryStore:
    def __init__(self, settings: Settings, db_path: Path | None = None) -> None:
        settings.ensure_dirs()
        path = Path(db_path) if db_path is not None else settings.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            # WAL lets concurrent runs read while another writes; busy_timeout waits out
            # a sibling's write lock instead of raising "database is locked".
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.DatabaseError:
            pass  # e.g. read-only or exotic filesystems — degrade to defaults
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()
        self.vectors = VectorStore(self._conn, get_embedder(settings), self._lock)

    # ---- construction helpers (handy for tests) ----
    @classmethod
    def in_memory(cls, embedder_backend: str = "hash") -> "MemoryStore":
        # Build a settings object pointed at an on-disk temp is overkill for tests;
        # use a private in-memory DB instead.
        obj = cls.__new__(cls)
        obj._conn = sqlite3.connect(":memory:", check_same_thread=False)
        obj._conn.row_factory = sqlite3.Row
        obj._conn.executescript(_SCHEMA)
        obj._conn.commit()
        from .embeddings import HashingEmbedder

        obj._lock = threading.RLock()
        obj.vectors = VectorStore(obj._conn, HashingEmbedder(), obj._lock)
        return obj

    # ---- memory ----
    def remember(
        self,
        scope: str,
        content: str,
        *,
        key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory(scope, key, content, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                (scope, key, content, json.dumps(metadata or {}), time.time()),
            )
            mem_id = int(cur.lastrowid)
            self._enforce_cap_locked()
            self._conn.commit()
        self.vectors.add(f"memory:{scope}", str(mem_id), content)
        return mem_id

    def _enforce_cap_locked(self) -> None:
        """Keep the store under ADA_MEMORY_MAX_ENTRIES by evicting overflow.

        Eviction order is oldest-first — with the recency half-life applied at recall
        time, the oldest entries are exactly the lowest decayed-relevance ones. Their
        vectors are dropped too so search never returns a dangling ref.
        """
        cap = _max_entries()
        if cap <= 0:
            return
        n = int(self._conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
        if n <= cap:
            return
        rows = self._conn.execute(
            "SELECT id, scope FROM memory ORDER BY created_at ASC, id ASC LIMIT ?",
            (n - cap,),
        ).fetchall()
        for row in rows:
            self._conn.execute("DELETE FROM memory WHERE id = ?", (row["id"],))
            self._conn.execute(
                "DELETE FROM vectors WHERE namespace = ? AND ref_id = ?",
                (f"memory:{row['scope']}", str(row["id"])),
            )

    def remember_unique(
        self,
        scope: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        dup_threshold: float = 0.92,
    ) -> int:
        """Like ``remember`` but skips writing a near-duplicate (cosine ≥ threshold).

        Keeps the long-term pile from filling with the same lesson every run. Returns the
        existing entry's id when a duplicate is found, else the new id.
        """
        hits = self.vectors.search(f"memory:{scope}", content, top_k=1, min_score=dup_threshold)
        if hits:
            return int(hits[0][0])
        return self.remember(scope, content, metadata=metadata)

    def recall(self, scope: str, query: str, top_k: int = 5,
               min_score: float = 0.0, decay: bool = False) -> list[MemoryEntry]:
        # Hybrid (cosine + lexical) retrieval; over-fetch so re-ranking by recency
        # has something to work with.
        hits = self.vectors.search_hybrid(f"memory:{scope}", query, top_k=top_k * 3, min_score=min_score)
        entries: list[MemoryEntry] = []
        for ref_id, score, _text in hits:
            row = self._conn.execute("SELECT * FROM memory WHERE id = ?", (int(ref_id),)).fetchone()
            if row:
                entries.append(self._row_to_entry(row, score))
        if decay:
            now = time.time()
            entries.sort(
                key=lambda e: (e.score or 0.0) * math.exp(-(now - e.created_at) / _HALF_LIFE_S),
                reverse=True,
            )
        return entries[:top_k]

    def recent(self, scope: str, limit: int = 10) -> list[MemoryEntry]:
        rows = self._conn.execute(
            "SELECT * FROM memory WHERE scope = ? ORDER BY id DESC LIMIT ?", (scope, limit)
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # ---- blackboard ----
    def blackboard_put(self, key: str, value: str, author: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO blackboard(key, value, author, updated_at) VALUES (?, ?, ?, ?)",
                (key, value, author, time.time()),
            )
            self._conn.commit()

    def blackboard_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM blackboard WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def blackboard_all(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM blackboard").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row, score: float | None = None) -> MemoryEntry:
        return MemoryEntry(
            id=int(row["id"]),
            scope=row["scope"],
            key=row["key"],
            content=row["content"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=float(row["created_at"]),
            score=score,
        )


def _near_duplicate(a: set[str], b: set[str], threshold: float = 0.9) -> bool:
    """Near-identical by normalized-token Jaccard (casing/punctuation-insensitive)."""
    if not a or not b:
        return a == b
    union = len(a | b)
    return union > 0 and len(a & b) / union >= threshold


class ScopedMemory:
    """Memory view over a project store + the shared global store.

    Recall reads from BOTH (project + global) and merges by score. New memories are
    written to a single chosen scope ("project" or "global"). Blackboard and vectors
    delegate to the project store (run-local working state lives with the project).
    """

    def __init__(self, project: MemoryStore, glob: MemoryStore, write_scope: str = "project") -> None:
        self.project = project
        self.glob = glob
        self.write_scope = "global" if write_scope == "global" else "project"

    def remember(
        self,
        scope: str,
        content: str,
        *,
        key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        target = self.glob if self.write_scope == "global" else self.project
        md = dict(metadata or {})
        md.setdefault("mem_scope", self.write_scope)
        return target.remember(scope, content, key=key, metadata=md)

    def remember_unique(self, scope: str, content: str, *, metadata: dict[str, Any] | None = None) -> int:
        target = self.glob if self.write_scope == "global" else self.project
        md = dict(metadata or {})
        md.setdefault("mem_scope", self.write_scope)
        return target.remember_unique(scope, content, metadata=md)

    def recall(self, scope: str, query: str, top_k: int = 5,
               min_score: float = 0.0, decay: bool = False) -> list[MemoryEntry]:
        merged = (self.project.recall(scope, query, top_k=top_k, min_score=min_score, decay=decay)
                  + self.glob.recall(scope, query, top_k=top_k, min_score=min_score, decay=decay))
        merged.sort(key=lambda e: (e.score if e.score is not None else 0.0), reverse=True)
        # Dedupe near-identical lessons across the two stores before filling top_k —
        # otherwise a lesson duplicated project+global eats two of the k slots.
        kept: list[MemoryEntry] = []
        kept_tokens: list[set[str]] = []
        for entry in merged:
            tokens = set(_tokenize(entry.content))
            if any(_near_duplicate(tokens, seen) for seen in kept_tokens):
                continue
            kept.append(entry)
            kept_tokens.append(tokens)
            if len(kept) >= top_k:
                break
        return kept

    def recent(self, scope: str, limit: int = 10) -> list[MemoryEntry]:
        return self.project.recent(scope, limit)

    # ---- blackboard + vectors delegate to the project store ----
    def blackboard_put(self, key: str, value: str, author: str = "") -> None:
        self.project.blackboard_put(key, value, author)

    def blackboard_get(self, key: str) -> str | None:
        return self.project.blackboard_get(key)

    def blackboard_all(self) -> dict[str, str]:
        return self.project.blackboard_all()

    @property
    def vectors(self) -> VectorStore:
        return self.project.vectors

    def close(self) -> None:
        self.project.close()
        self.glob.close()
