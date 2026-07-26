"""A tiny vector store: vectors live in SQLite, similarity is brute-force cosine.

Fine for a local single-user skeleton. Behind this one class so it can be swapped
for sqlite-vec / Qdrant later without touching callers.

Hardening:
  - writes are guarded by a lock (parallel subtasks share one connection),
  - a dimension mismatch (e.g. a fastembed→hash fallback mid-project) is skipped instead
    of crashing the dot product,
  - ``min_score`` lets callers drop weak matches rather than always returning top_k,
  - hybrid search never raises on a broken embedder: if the query can't be embedded,
    the cosine leg is skipped and the lexical leg alone ranks.

Retrieval comes in two flavors:
  - ``search``:        pure cosine (kept score-stable for dedup thresholds like 0.92),
  - ``search_hybrid``: cosine + lexical (idf-weighted token overlap) fused with
    reciprocal-rank fusion — dependency-free and useful with both embedder kinds,
    since lessons are short sentences where lexical matching helps a lot.
"""

from __future__ import annotations

import math
import sqlite3
import threading

import numpy as np

from .embeddings import Embedder, _tokenize

_RRF_K = 60  # standard reciprocal-rank-fusion damping constant


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
        q = np.asarray(query_vec, dtype="float32")
        # Filter dimension in SQL: rows written under a different embedding model can
        # never match, so don't even pull them off disk.
        rows = self._conn.execute(
            "SELECT ref_id, vector, text FROM vectors WHERE namespace = ? AND dim = ?",
            (namespace, int(q.shape[0])),
        ).fetchall()
        if not rows:
            return []

        q_norm = float(np.linalg.norm(q)) or 1.0

        scored: list[tuple[str, float, str]] = []
        for row in rows:
            vec = np.frombuffer(row["vector"], dtype="float32")
            denom = (float(np.linalg.norm(vec)) or 1.0) * q_norm
            score = float(np.dot(vec, q) / denom)
            if score >= min_score:
                scored.append((row["ref_id"], score, row["text"]))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def search_hybrid(self, namespace: str, query: str, top_k: int = 5,
                      min_score: float = 0.0,
                      query_vec: np.ndarray | None = None) -> list[tuple[str, float, str]]:
        """Cosine + lexical retrieval fused by reciprocal-rank fusion.

        The lexical leg scores each stored text by idf-weighted coverage of the query's
        tokens (a normalized BM25-ish overlap in [0, 1]). Ranking fuses both legs via
        RRF; the *reported* score is ``max(cosine, lexical)`` so it stays comparable to
        the cosine-scale ``min_score`` thresholds callers already use.

        ``query_vec`` lets a caller sweeping several stores embed the query ONCE and
        reuse it. When the query can't be embedded (embedder down), the cosine leg is
        skipped and the lexical leg alone ranks — retrieval never raises.
        """
        return [(ref, score, text) for ref, score, text, _semantic in
                self.search_hybrid_detail(namespace, query, top_k=top_k,
                                          min_score=min_score, query_vec=query_vec)]

    def search_hybrid_detail(self, namespace: str, query: str, top_k: int = 5,
                             min_score: float = 0.0, query_vec: np.ndarray | None = None,
                             ) -> list[tuple[str, float, str, bool]]:
        """``search_hybrid`` plus a per-hit ``semantic`` flag.

        The flag is True when the cosine leg supplied the reported score (semantic
        similarity strictly beat the lexical evidence) — such a hit may share no
        tokens with the query at all. Same contract otherwise.
        """
        rows = self._conn.execute(
            "SELECT ref_id, vector, dim, text FROM vectors WHERE namespace = ?", (namespace,)
        ).fetchall()
        if not rows:
            return []

        if query_vec is not None:
            q = np.asarray(query_vec, dtype="float32")
        else:
            try:
                q = np.asarray(self._embedder.embed([query])[0], dtype="float32")
            except Exception:  # noqa: BLE001 — embedder down: degrade, don't break search
                q = None
        q_norm = (float(np.linalg.norm(q)) or 1.0) if q is not None else 1.0

        texts: dict[str, str] = {}
        cos: dict[str, float] = {}
        doc_tokens: dict[str, set[str]] = {}
        for row in rows:
            ref = row["ref_id"]
            texts[ref] = row["text"] or ""
            doc_tokens[ref] = set(_tokenize(texts[ref]))
            # No query vector or dim mismatch → lexical leg only for this row.
            if q is not None and int(row["dim"]) == q.shape[0]:
                vec = np.frombuffer(row["vector"], dtype="float32")
                denom = (float(np.linalg.norm(vec)) or 1.0) * q_norm
                cos[ref] = float(np.dot(vec, q) / denom)

        # Lexical leg: idf-weighted share of the query's informative tokens each doc covers.
        q_tokens = set(_tokenize(query))
        n_docs = len(rows)
        lex: dict[str, float] = {}
        if q_tokens:
            idf = {}
            for tok in q_tokens:
                df = sum(1 for toks in doc_tokens.values() if tok in toks)
                idf[tok] = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            denom_idf = sum(idf.values()) or 1.0
            for ref, toks in doc_tokens.items():
                lex[ref] = sum(idf[t] for t in q_tokens & toks) / denom_idf
        else:
            lex = {ref: 0.0 for ref in texts}

        cos_rank = {r: i for i, r in enumerate(sorted(cos, key=lambda r: (-cos[r], r)))}
        lex_hits = [r for r in lex if lex[r] > 0.0]
        lex_rank = {r: i for i, r in enumerate(sorted(lex_hits, key=lambda r: (-lex[r], r)))}

        fused: list[tuple[float, float, str]] = []
        for ref in texts:
            rrf = 0.0
            if ref in cos_rank:
                rrf += 1.0 / (_RRF_K + cos_rank[ref] + 1)
            if ref in lex_rank:
                rrf += 1.0 / (_RRF_K + lex_rank[ref] + 1)
            combined = max(cos.get(ref, 0.0), lex.get(ref, 0.0))
            if combined >= min_score:
                fused.append((rrf, combined, ref))

        fused.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [(ref, combined, texts[ref], cos.get(ref, 0.0) > lex.get(ref, 0.0))
                for _rrf, combined, ref in fused[:top_k]]
