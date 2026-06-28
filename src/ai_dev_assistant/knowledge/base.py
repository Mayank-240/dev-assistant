"""Knowledge base: ingest documents/code as embedded chunks, retrieve by similarity.

Reuses the shared VectorStore (same SQLite DB + embedder as memory) under the "kb"
namespace, so there's one embedding model for the whole system.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..memory.vector import VectorStore

_KB_NAMESPACE = "kb"


def _chunk(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return chunks


@dataclass
class KBHit:
    ref_id: str
    score: float
    text: str


class KnowledgeBase:
    def __init__(self, vectors: VectorStore) -> None:
        self._vectors = vectors

    def ingest(self, doc_id: str, text: str) -> int:
        """Chunk + embed a document. Returns the number of chunks stored."""
        chunks = _chunk(text)
        for i, chunk in enumerate(chunks):
            self._vectors.add(_KB_NAMESPACE, f"{doc_id}#{i}", chunk)
        return len(chunks)

    def search(self, query: str, top_k: int = 5) -> list[KBHit]:
        hits = self._vectors.search(_KB_NAMESPACE, query, top_k=top_k)
        return [KBHit(ref_id=r, score=s, text=t) for r, s, t in hits]
