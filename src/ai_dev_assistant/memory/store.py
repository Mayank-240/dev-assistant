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
_LIST_CAP = 1000                 # hard page-size ceiling for curation listings


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

    # ---- curation (list / update / delete for a memory-management UI) ----
    def list_memories(self, *, scope: str | None = None, limit: int = 200,
                      offset: int = 0) -> list[dict[str, Any]]:
        """Newest-first listing of raw memory rows for curation.

        Each row: ``{id, scope, key, content, metadata, created_at}`` — ``id`` is
        the SQLite primary key and round-trips through ``update_memory`` /
        ``delete_memory``; ``scope`` is the memory's kind (e.g. "longterm" or a
        task scope). ``scope=None`` lists every scope. ``limit`` (clamped to
        [1, 1000]) and ``offset`` paginate.
        """
        limit = max(1, min(int(limit), _LIST_CAP))
        offset = max(0, int(offset))
        sql = "SELECT id, scope, key, content, metadata, created_at FROM memory"
        args: tuple[Any, ...] = ()
        if scope:
            sql += " WHERE scope = ?"
            args = (scope,)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        rows = self._conn.execute(sql, (*args, limit, offset)).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                meta = json.loads(r["metadata"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            out.append({
                "id": int(r["id"]), "scope": r["scope"], "key": r["key"],
                "content": r["content"], "metadata": meta,
                "created_at": float(r["created_at"]),
            })
        return out

    def update_memory(self, mem_id: int, content: str) -> bool:
        """Rewrite a memory's text by id, re-embedding its stored vector.

        Returns False when the id is unknown. If re-embedding fails (embedder
        down), the text is still updated everywhere it is read — the memory row
        AND the vector row's ``text`` column (which the lexical search leg uses)
        — and the stale vector is kept so the entry stays recallable.
        """
        mem_id = int(mem_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT scope FROM memory WHERE id = ?", (mem_id,)).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE memory SET content = ? WHERE id = ?", (content, mem_id))
            self._conn.commit()
        namespace = f"memory:{row['scope']}"
        try:
            self.vectors.add(namespace, str(mem_id), content)  # re-embed
        except Exception:  # noqa: BLE001 — embedder down: refresh the lexical text only
            with self._lock:
                self._conn.execute(
                    "UPDATE vectors SET text = ? WHERE namespace = ? AND ref_id = ?",
                    (content, namespace, str(mem_id)))
                self._conn.commit()
        return True

    def delete_memory(self, mem_id: int) -> bool:
        """Delete a memory by id, dropping its vector too so recall/search can
        never resurface a dangling ref. Returns False when the id is unknown."""
        mem_id = int(mem_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT scope FROM memory WHERE id = ?", (mem_id,)).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM memory WHERE id = ?", (mem_id,))
            self._conn.execute(
                "DELETE FROM vectors WHERE namespace = ? AND ref_id = ?",
                (f"memory:{row['scope']}", str(mem_id)))
            self._conn.commit()
        return True

    def count_memories(self, scope: str | None = None) -> int:
        """Number of stored memories, optionally restricted to one scope/kind."""
        if scope:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM memory WHERE scope = ?", (scope,)).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM memory").fetchone()
        return int(row[0])

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


# ---------------------------------------------------------------------------
# Per-project curation helpers (module-level, for the web/API layer)
#
# global_search memory hits carry ``{project, ref: {id}}`` — these functions
# accept exactly that pair, opening ``projects_dir/<project>/memory.db``
# writable for the duration of one call. A missing database (unknown slug,
# project without memories yet) is never created as a side effect: list/count
# return empty/zero and update/delete return False. For the shared global
# store, use ``MemoryStore(settings, db_path=settings.global_db_path)`` and
# the store methods directly.
# ---------------------------------------------------------------------------

def _open_project_store(settings: Settings, project: str) -> MemoryStore | None:
    """Writable store over an EXISTING project database, else None."""
    path = settings.projects_dir / (project or "default") / "memory.db"
    if not path.is_file():
        return None
    return MemoryStore(settings, db_path=path)


def list_project_memories(settings: Settings, project: str, *,
                          scope: str | None = None, limit: int = 200,
                          offset: int = 0) -> list[dict[str, Any]]:
    """Newest-first curation listing for one project's memories.

    Row shape is ``MemoryStore.list_memories`` (``{id, scope, key, content,
    metadata, created_at}``) plus ``project`` — the slug, echoed so rows from
    several projects can be concatenated. Missing database -> ``[]``.
    """
    store = _open_project_store(settings, project)
    if store is None:
        return []
    try:
        rows = store.list_memories(scope=scope, limit=limit, offset=offset)
    finally:
        store.close()
    for row in rows:
        row["project"] = project
    return rows


def update_project_memory(settings: Settings, project: str, mem_id: int,
                          content: str) -> bool:
    """Rewrite one memory's text (re-embedding it) in a project's store.

    Returns False for an unknown project or id — see ``MemoryStore.update_memory``.
    """
    store = _open_project_store(settings, project)
    if store is None:
        return False
    try:
        return store.update_memory(mem_id, content)
    finally:
        store.close()


def delete_project_memory(settings: Settings, project: str, mem_id: int) -> bool:
    """Delete one memory (and its vector) from a project's store.

    Returns False for an unknown project or id — see ``MemoryStore.delete_memory``.
    """
    store = _open_project_store(settings, project)
    if store is None:
        return False
    try:
        return store.delete_memory(mem_id)
    finally:
        store.close()


def count_project_memories(settings: Settings, project: str, *,
                           scope: str | None = None) -> int:
    """How many memories a project's store holds (0 for a missing database),
    optionally restricted to one scope/kind."""
    store = _open_project_store(settings, project)
    if store is None:
        return 0
    try:
        return store.count_memories(scope)
    finally:
        store.close()
