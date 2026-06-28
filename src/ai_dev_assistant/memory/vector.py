"""A tiny vector store: vectors live in SQLite, similarity is brute-force cosine.

Fine for a local single-user skeleton. Behind this one class so it can be swapped
for sqlite-vec / Qdrant later without touching callers.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from .embeddings import Embedder


class VectorStore:
    def __init__(self, conn: sqlite3.Connection, embedder: Embedder) -> None:
        self._conn = conn
        self._embedder = embedder

    def add(self, namespace: str, ref_id: str, text: str) -> None:
        vector = self._embedder.embed([text])[0]
        self.add_vector(namespace, ref_id, vector, text)

    def add_vector(self, namespace: str, ref_id: str, vector: list[float], text: str = "") -> None:
        arr = np.asarray(vector, dtype="float32")
        self._conn.execute(
            "INSERT OR REPLACE INTO vectors(namespace, ref_id, vector, dim, text) "
            "VALUES (?, ?, ?, ?, ?)",
            (namespace, ref_id, arr.tobytes(), int(arr.shape[0]), text),
        )
        self._conn.commit()

    def search(self, namespace: str, query: str, top_k: int = 5) -> list[tuple[str, float, str]]:
        query_vec = np.asarray(self._embedder.embed([query])[0], dtype="float32")
        return self.search_vector(namespace, query_vec, top_k)

    def search_vector(
        self, namespace: str, query_vec: np.ndarray, top_k: int = 5
    ) -> list[tuple[str, float, str]]:
        rows = self._conn.execute(
            "SELECT ref_id, vector, text FROM vectors WHERE namespace = ?", (namespace,)
        ).fetchall()
        if not rows:
            return []

        q = np.asarray(query_vec, dtype="float32")
        q_norm = float(np.linalg.norm(q)) or 1.0

        scored: list[tuple[str, float, str]] = []
        for row in rows:
            vec = np.frombuffer(row["vector"], dtype="float32")
            denom = (float(np.linalg.norm(vec)) or 1.0) * q_norm
            score = float(np.dot(vec, q) / denom)
            scored.append((row["ref_id"], score, row["text"]))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]
