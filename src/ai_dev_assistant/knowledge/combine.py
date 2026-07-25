"""Read-only, on-demand combination of multiple projects' knowledge + memory.

Every project keeps its own knowledge graph and memory store; these helpers build
a merged, tagged view across a chosen set of project slugs WITHOUT ever writing to
any project's data:

- knowledge graphs are loaded from each project's ``knowledge_graph.json``;
- memory databases are opened over read-only SQLite connections (``mode=ro``),
  so even the schema bootstrap in ``MemoryStore.__init__`` never runs.

Defensive by design: unknown slugs and missing files are silently skipped, and a
corrupt/unreadable project contributes nothing rather than raising to the caller.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..config import Settings
from ..memory.embeddings import _tokenize, get_embedder
from ..memory.store import MemoryStore, _near_duplicate
from ..memory.vector import VectorStore
from .graph import NetworkXKnowledgeGraph


def _dedupe_slugs(slugs: list[str] | None) -> list[str]:
    out: list[str] = []
    for s in slugs or []:
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Knowledge graphs
# ---------------------------------------------------------------------------

def combined_triples(settings: Settings, slugs: list[str]) -> dict[str, Any]:
    """Merged nodes/edges across the given projects' knowledge graphs.

    Same shape as the single-project /api/graph payload (``nodes`` of
    ``{id, type}``, ``edges`` of ``{source, target, relation}``), plus:

    - ``projects``: the slugs that actually contributed (missing ones skipped);
    - a ``sources`` list on every node and edge naming the project slug(s) it
      came from, so a UI can color by project.

    Read-only: graphs are loaded, never saved.
    """
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    used: list[str] = []
    for slug in _dedupe_slugs(slugs):
        path = settings.projects_dir / slug / "knowledge_graph.json"
        if not path.is_file():
            continue  # unknown slug or project without a KG yet — skip quietly
        try:
            kg = NetworkXKnowledgeGraph(path)
        except Exception:  # noqa: BLE001 — corrupt JSON etc.: contribute nothing
            continue
        used.append(slug)
        for node_id, node_type in kg.node_types().items():
            entry = nodes.get(node_id)
            if entry is None:
                nodes[node_id] = {"id": node_id, "type": node_type, "sources": [slug]}
            else:
                if slug not in entry["sources"]:
                    entry["sources"].append(slug)
                if entry["type"] == "concept" and node_type != "concept":
                    entry["type"] = node_type  # prefer a specific type over the default
        for tr in kg.all_triples():
            key = (tr.subject, tr.object, tr.relation)
            edge = edges.get(key)
            if edge is None:
                edges[key] = {"source": tr.subject, "target": tr.object,
                              "relation": tr.relation, "sources": [slug]}
            elif slug not in edge["sources"]:
                edge["sources"].append(slug)
    return {"projects": used, "nodes": list(nodes.values()), "edges": list(edges.values())}


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def _connect_ro(path: Path) -> sqlite3.Connection | None:
    """A read-only SQLite connection to ``path``, or None when absent/unopenable."""
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _open_store_ro(settings: Settings, path: Path) -> MemoryStore | None:
    """A MemoryStore over a read-only connection (bypasses __init__: no schema
    bootstrap, no PRAGMA writes, no directory creation)."""
    conn = _connect_ro(path)
    if conn is None:
        return None
    store = MemoryStore.__new__(MemoryStore)
    store._conn = conn
    store._lock = threading.RLock()
    store.vectors = VectorStore(conn, get_embedder(settings), store._lock)
    return store


def combined_memory_search(settings: Settings, slugs: list[str], query: str, *,
                           top_k: int = 10, kind: str | None = None) -> list[dict]:
    """Search the given projects' memory stores and merge the hits.

    Runs the same hybrid recall a single project's memory uses (MemoryStore.recall),
    merges by score descending, dedupes near-identical content across projects
    (first — highest-scoring — copy wins) and tags every hit with ``project``.

    Read-only: stores are opened with SQLite ``mode=ro`` and always closed;
    missing databases and unreadable projects are skipped, never raised.
    """
    top_k = max(1, int(top_k))
    hits: list[dict[str, Any]] = []
    for slug in _dedupe_slugs(slugs):
        store = _open_store_ro(settings, settings.projects_dir / slug / "memory.db")
        if store is None:
            continue
        try:
            if kind:
                scopes = [kind]
            else:
                scopes = [r["scope"] for r in store._conn.execute(
                    "SELECT DISTINCT scope FROM memory").fetchall()]
            for scope in scopes:
                for e in store.recall(scope, query, top_k=top_k):
                    meta = e.metadata or {}
                    hits.append({
                        "id": e.id, "scope": e.scope, "content": e.content,
                        "author": meta.get("author", ""),
                        "subtask": meta.get("subtask", ""),
                        "created_at": e.created_at, "mem_scope": "project",
                        "score": e.score if e.score is not None else 0.0,
                        "project": slug,
                    })
        except sqlite3.Error:
            pass  # partial/foreign schema — this project contributes nothing
        finally:
            store.close()
    hits.sort(key=lambda h: h["score"], reverse=True)
    kept: list[dict[str, Any]] = []
    kept_tokens: list[set[str]] = []
    for h in hits:
        tokens = set(_tokenize(h["content"]))
        if any(_near_duplicate(tokens, seen) for seen in kept_tokens):
            continue  # near-identical lesson already kept from another project
        kept.append(h)
        kept_tokens.append(tokens)
        if len(kept) >= top_k:
            break
    return kept


def combined_memory_recent(settings: Settings, slugs: list[str], *,
                           limit: int = 300, kind: str | None = None) -> list[dict]:
    """Merged recent-memories listing across projects (the no-query counterpart of
    ``combined_memory_search``): the same item shape /api/memory serves today,
    plus ``project`` on every row. Newest first, capped at ``limit``. Read-only.
    """
    limit = max(1, int(limit))
    out: list[dict[str, Any]] = []
    for slug in _dedupe_slugs(slugs):
        conn = _connect_ro(settings.projects_dir / slug / "memory.db")
        if conn is None:
            continue
        try:
            sql = "SELECT id, scope, key, content, metadata, created_at FROM memory"
            args: tuple[Any, ...] = ()
            if kind:
                sql += " WHERE scope = ?"
                args = (kind,)
            sql += " ORDER BY id DESC LIMIT ?"
            rows = conn.execute(sql, (*args, limit)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for r in rows:
            try:
                meta = json.loads(r["metadata"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            out.append({
                "id": r["id"], "scope": r["scope"], "content": r["content"],
                "author": meta.get("author", ""), "subtask": meta.get("subtask", ""),
                "created_at": r["created_at"], "mem_scope": "project",
                "project": slug,
            })
    out.sort(key=lambda m: m["created_at"], reverse=True)
    return out[:limit]
