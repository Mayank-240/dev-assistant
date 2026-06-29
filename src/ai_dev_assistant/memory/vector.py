"""A tiny vector store: vectors live in SQLite, similarity is brute-force cosine.

Fine for a local single-user skeleton. Behind this one class so it can be swapped
for sqlite-vec / Qdrant later without touching callers.

Hardening:
  - writes are guarded by a lock (parallel subtasks share one connection),
  - a dimension mismatch (e.g. a fastembed→hash fallback mid-project) is skipped instead
    of crashing the dot product,
  - ``min_score`` lets callers drop weak matches rather than always returning top_k.
"""

from __future__ import annotations

import sqlite3
import threading

import numpy as np

from .embeddings import Embedder


class VectorStore:
    def __init__(self, conn: sqlite3.Connection, embedder: Embedder,
                 lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._embedder = embedder
        self._lock = lock or threading.Lock()

    def add(self, namespace: str, ref_id: str, text: str) -> None:
        vector = self._embedder.embed([text])[0]
        self.add_vector(namespace, ref_id, vector, text)

    def add_vector(self, namespace: str, ref_id: str, vector: list[float], text: str = "") -> None:
        arr = np.asarray(vector, dtype="float32")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO vectors(namespace, ref_id, vector, dim, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (namespace, ref_id, arr.tobytes(), int(arr.shape[0]), text),
            )
            self._conn.commit()

    def delete_prefix(self, namespace: str, ref_prefix: str) -> None:
        """Drop vectors whose ref_id starts with ``ref_prefix`` (e.g. all chunks of a doc)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM vectors WHERE namespace = ? AND ref_id LIKE ?",
                (namespace, ref_prefix + "%"),
            )
            self._conn.commit()

    def search(self, namespace: str, query: str, top_k: int = 5,
               min_score: float = 0.0) -> list[tuple[str, float, str]]:
        query_vec = np.asarray(self._embedder.embed([query])[0], dtype="float32")
        return self.search_vector(namespace, query_vec, top_k, min_score)

    def search_vector(
        self, namespace: str, query_vec: np.ndarray, top_k: int = 5, min_score: float = 0.0
    ) -> list[tuple[str, float, str]]:
        rows = self._conn.execute(
            "SELECT ref_id, vector, dim, text FROM vectors WHERE namespace = ?", (namespace,)
        ).fetchall()
        if not rows:
            return []

        q = np.asarray(query_vec, dtype="float32")
        q_norm = float(np.linalg.norm(q)) or 1.0

        scored: list[tuple[str, float, str]] = []
        for row in rows:
            if int(row["dim"]) != q.shape[0]:
                continue  # embedding-model mismatch — skip rather than crash the dot product
            vec = np.frombuffer(row["vector"], dtype="float32")
            denom = (float(np.linalg.norm(vec)) or 1.0) * q_norm
            score = float(np.dot(vec, q) / denom)
            if score >= min_score:
                scored.append((row["ref_id"], score, row["text"]))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]
