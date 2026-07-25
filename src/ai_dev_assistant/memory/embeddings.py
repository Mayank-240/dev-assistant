"""Text embedding behind a small interface.

Two implementations:
  - FastEmbedEmbedder: real local semantic embeddings (ONNX, no torch). Downloads a
    small model on first use.
  - HashingEmbedder:   deterministic, dependency-free, offline. Weaker recall, but lets
    tests and air-gapped runs work without any download.

Swap to a hosted embedder (e.g. Voyage) later by adding another class here.
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Protocol

from ..config import Settings

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class Embedder(Protocol):
    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Hashed bag-of-words into a fixed-dim L2-normalized vector. Deterministic."""

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        out: list[list[float]] = []
        for text in texts:
            vec = np.zeros(self._dim, dtype="float32")
            for tok in _tokenize(text):
                digest = hashlib.md5(tok.encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "big") % self._dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec /= norm
            out.append(vec.tolist())
        return out


class FastEmbedEmbedder:
    """Local ONNX embeddings via fastembed."""

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding  # lazy: avoids import cost when unused

        self._model = TextEmbedding(model_name=model_name)
        # Probe dimension once with a throwaway embedding.
        self._dim = len(next(iter(self._model.embed(["dimension probe"]))))

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [list(map(float, v)) for v in self._model.embed(texts)]


# Process-wide embedder cache. Constructing a MemoryStore is cheap; loading the
# FastEmbed ONNX model is not — multiple stores (project + global, engine-per-request
# in the web layer) must share one instance instead of reloading the model each time.
_CACHE_LOCK = threading.Lock()
_EMBEDDER_CACHE: dict[tuple[str, str], tuple[Embedder, str]] = {}
_ACTIVE_NAME = "uninitialized"


def active_embedder_name() -> str:
    """Name of the embedder ``get_embedder`` most recently produced.

    e.g. "fastembed:bge-small-en-v1.5", "hash" (explicitly configured), or
    "hash-fallback" (fastembed failed to load — semantic recall is degraded).
    Lets the engine surface a silent quality cliff as a run event / UI badge.
    """
    return _ACTIVE_NAME


def get_embedder(settings: Settings) -> Embedder:
    """Return a process-wide cached embedder for the configured backend/model."""
    global _ACTIVE_NAME
    key = (settings.embeddings_backend, settings.embed_model)
    # Holding the lock across construction is deliberate: it stops two threads from
    # loading the ONNX model twice. Cache hits are effectively free.
    with _CACHE_LOCK:
        cached = _EMBEDDER_CACHE.get(key)
        if cached is not None:
            _ACTIVE_NAME = cached[1]
            return cached[0]
        embedder, name = _build_embedder(settings)
        _EMBEDDER_CACHE[key] = (embedder, name)
        _ACTIVE_NAME = name
        return embedder


def _build_embedder(settings: Settings) -> tuple[Embedder, str]:
    if settings.embeddings_backend == "hash":
        return HashingEmbedder(), "hash"
    try:
        model = settings.embed_model
        return FastEmbedEmbedder(model), f"fastembed:{model.split('/')[-1]}"
    except Exception as exc:  # missing dep / no network on first download
        import warnings

        warnings.warn(
            f"fastembed unavailable ({exc}); falling back to HashingEmbedder. "
            "Set ADA_EMBEDDINGS_BACKEND=hash to silence this.",
            stacklevel=2,
        )
        return HashingEmbedder(), "hash-fallback"
