"""Global search: one query, ranked hits across all projects and four modalities.

Modalities:
- ``task``:   run rows in ``data_dir/runs.db`` — substring/token match over
              prompt + title + summary, recency-boosted;
- ``memory``: each project's memory store, via the same hybrid recall a single
              project uses (``knowledge.combine.combined_memory_search``) —
              semantic cosine blended with lexical scoring;
- ``kb``:     the "kb" namespace of each project's vectors table, via the same
              hybrid cosine+lexical search (the query is embedded ONCE and
              reused across projects — chunk vectors are persisted at ingest);
- ``file``:   file NAME/PATH matches (never content — too slow) over each
              project's checkout and its task worktrees
              (``workspace/<slug>/worktrees/<task-id>``).

For both vector-backed modalities the reported score is ``max(cosine, lexical)``,
so with the hash embeddings backend (or when the embedder fails — hybrid search
degrades to its lexical leg instead of raising) results never rank worse than
pure lexical scoring. Hits whose score came from semantic similarity rather than
token overlap carry an optional ``"semantic": True`` flag.

Strictly read-only everywhere: every database is opened with SQLite ``mode=ro``
(the knowledge/combine.py technique), the project registry is read WITHOUT the
ensure-default write that ``projects.list_projects`` performs, and the
filesystem is only walked. Missing databases, unknown slugs and unreadable
projects are silently skipped, never raised.

Ranking model: each modality produces a raw score clamped to [0, 1], which is
multiplied by a slight kind weight (task 1.0, memory 0.9, kb 0.85, file 0.8)
and merged with a deterministic sort (score desc, then kind/project/title/ref
for stability). Near-identical snippets within a kind are deduped — files are
additionally scoped per project, since two repos legitimately share layouts.
Candidates are capped per modality before ranking, and the file walk has a
per-project breadth budget, so one query stays fast across several projects.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import projects as _projects
from .config import Settings
from .knowledge import combine as _combine
from .knowledge.base import _KB_NAMESPACE
from .knowledge.combine import _connect_ro, _open_store_ro
from .memory.embeddings import _tokenize, get_embedder
from .memory.store import _near_duplicate

# Slight kind weights: an equally strong match is worth a bit more as a task
# than as a memory, KB chunk, or file-name hit.
_KIND_WEIGHTS = {"task": 1.0, "memory": 0.9, "kb": 0.85, "file": 0.8}
_KIND_ORDER = {"task": 0, "memory": 1, "kb": 2, "file": 3}

_HALF_LIFE_S = 14 * 24 * 3600.0  # task recency boost halves after ~two weeks
_RECENCY_BOOST = 0.1             # max additive boost for a just-created task
_MIN_VEC_SCORE = 0.05            # drop noise-level cosine/lexical matches
_TASK_ROW_SCAN = 500             # newest runs considered (recency-first scan cap)
_MAX_FILES_SCANNED = 4000        # per-project file-walk breadth budget
_MAX_WORKTREES = 50              # per-project worktree dirs considered
# Hidden/internal dirs the rest of the codebase also excludes (projects._SKIP_PARTS
# / web _WS_HIDDEN): .git, inner .ada_worktrees, node_modules, __pycache__, .venv, …
_SKIP_DIRS = {".git", ".ada_worktrees", ".ada_deps", "node_modules", "__pycache__",
              ".venv", ".pytest_cache", ".ada_data", "dist", "build"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def global_search(settings: Settings, query: str, *,
                  kinds: tuple[str, ...] = ("task", "memory", "kb", "file"),
                  projects_filter: list[str] | None = None,
                  limit: int = 30) -> list[dict]:
    """One query -> ranked hits across projects and modalities.

    Each hit: ``{kind, project, title, snippet, score, ref}`` where ``ref``
    carries enough for a UI to open it: task -> {task_id}, memory -> {id},
    kb -> {ref_id}, file -> {task | None, path} (path relative to the checkout
    or worktree root; ``task`` names the worktree's task, None for the checkout).
    Vector-backed hits (kb) additionally carry ``semantic: True`` when semantic
    similarity, not lexical overlap, produced the score.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    limit = max(1, int(limit))
    cap = max(30, limit)  # per-modality candidate cap before ranking
    qtokens = _tokenize(q)
    entries = _selected_entries(settings, projects_filter)
    slugs = [e["slug"] for e in entries]

    hits: list[dict[str, Any]] = []
    for kind in dict.fromkeys(kinds):  # order-preserving dedupe of kinds
        if kind == "task":
            mod = _search_tasks(settings, q, qtokens, slugs)
        elif kind == "memory":
            mod = _search_memories(settings, query, slugs, cap)
        elif kind == "kb":
            mod = _search_kb(settings, query, slugs, cap)
        elif kind == "file":
            mod = _search_files(settings, q, qtokens, entries, cap)
        else:
            continue  # unknown kind: ignore
        mod.sort(key=lambda h: -h["score"])
        hits.extend(mod[:cap])
    return _rank(hits, limit)


# ---------------------------------------------------------------------------
# Project selection (read-only: never triggers list_projects' registry write)
# ---------------------------------------------------------------------------

def _selected_entries(settings: Settings,
                      projects_filter: list[str] | None) -> list[dict[str, Any]]:
    entries = [_projects._normalize(p) for p in _projects._read_registry(settings)]
    if not any(e.get("slug") == _projects.DEFAULT_PROJECT for e in entries):
        # list_projects would persist this; we only add it in memory.
        entries.insert(0, _projects._normalize(
            {"slug": _projects.DEFAULT_PROJECT, "name": "Default"}))
    if projects_filter is None:
        return entries
    wanted = {(s or "").strip() for s in projects_filter if (s or "").strip()}
    return [e for e in entries if e["slug"] in wanted]  # unknown slugs: skipped


# ---------------------------------------------------------------------------
# Modality searches
# ---------------------------------------------------------------------------

def _search_tasks(settings: Settings, q: str, qtokens: list[str],
                  slugs: list[str]) -> list[dict[str, Any]]:
    """Substring/token match over prompt+title+summary in runs.db, recency-boosted."""
    conn = _connect_ro(settings.data_dir / "runs.db")
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id, title, prompt, summary, created_at, "
            "COALESCE(project, 'default') AS project "
            "FROM runs ORDER BY created_at DESC LIMIT ?", (_TASK_ROW_SCAN,)
        ).fetchall()
    except sqlite3.Error:
        return []  # foreign/partial schema — contribute nothing
    finally:
        conn.close()

    allowed = set(slugs)
    now = time.time()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["project"] not in allowed:
            continue
        title = r["title"] or ""
        prompt = r["prompt"] or ""
        summary = r["summary"] or ""
        base = _text_base(q, qtokens, title, f"{title}\n{prompt}\n{summary}".lower())
        if base is None:
            continue
        age = max(0.0, now - float(r["created_at"] or 0.0))
        score = min(1.0, base + _RECENCY_BOOST * math.exp(-age / _HALF_LIFE_S))
        out.append({
            "kind": "task", "project": r["project"],
            "title": title or _clip(prompt, 56),
            "snippet": _clip(summary or prompt, 200),
            "score": score, "ref": {"task_id": r["id"]},
        })
    return out


def _search_memories(settings: Settings, query: str, slugs: list[str],
                     cap: int) -> list[dict[str, Any]]:
    """Hybrid recall per project via combine (read-only stores, deduped, tagged).

    Each hit's score is already ``max(cosine, lexical)`` (VectorStore.search_hybrid),
    so semantic matches surface even with zero token overlap, and a failing embedder
    degrades to lexical scoring inside the hybrid search instead of raising.
    """
    out: list[dict[str, Any]] = []
    for h in _combine.combined_memory_search(settings, slugs, query, top_k=cap):
        score = max(0.0, min(1.0, float(h.get("score") or 0.0)))
        if score < _MIN_VEC_SCORE:
            continue
        content = h.get("content") or ""
        out.append({
            "kind": "memory", "project": h["project"],
            "title": _clip(content, 60), "snippet": _clip(content, 200),
            "score": score, "ref": {"id": h["id"]},
        })
    return out


def _search_kb(settings: Settings, query: str, slugs: list[str],
               cap: int) -> list[dict[str, Any]]:
    """Hybrid (cosine + lexical) search over each project's "kb" namespace.

    The query is embedded ONCE and reused across projects (chunk vectors are
    persisted at ingest time, so nothing else is embedded per query); when the
    embedder is unavailable, ``query_vec`` stays None and each store's hybrid
    search degrades to its lexical leg. Same read-only stores as before.
    """
    query_vec = _embed_query(settings, query)
    out: list[dict[str, Any]] = []
    for slug in slugs:
        store = _open_store_ro(settings, settings.projects_dir / slug / "memory.db")
        if store is None:
            continue  # project without a memory.db yet — skip quietly
        try:
            vec_hits = store.vectors.search_hybrid_detail(
                _KB_NAMESPACE, query, top_k=cap, min_score=_MIN_VEC_SCORE,
                query_vec=query_vec)
        except sqlite3.Error:
            vec_hits = []  # partial/foreign schema — contribute nothing
        finally:
            store.close()
        for ref_id, score, text, semantic in vec_hits:
            hit = {
                "kind": "kb", "project": slug,
                "title": str(ref_id).split("#", 1)[0],
                "snippet": _clip(text or "", 200),
                "score": max(0.0, min(1.0, float(score))),
                "ref": {"ref_id": ref_id},
            }
            if semantic:
                hit["semantic"] = True
            out.append(hit)
    return out


def _embed_query(settings: Settings, query: str) -> np.ndarray | None:
    """The query's embedding, or None when the embedder is unavailable —
    callers then fall back to lexical scoring instead of failing the search."""
    try:
        vec = np.asarray(get_embedder(settings).embed([query])[0], dtype="float32")
    except Exception:  # noqa: BLE001 — embedding must never break search
        return None
    return vec if vec.size else None


def _search_files(settings: Settings, q: str, qtokens: list[str],
                  entries: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """Name/path matching (no content grep) over checkouts + task worktrees."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        budget = _MAX_FILES_SCANNED
        project_hits: list[dict[str, Any]] = []
        for task, root in _file_roots(settings, entry):
            if budget < 0:
                break
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
                for name in sorted(filenames):
                    budget -= 1
                    if budget < 0:
                        break
                    rel = os.path.relpath(os.path.join(dirpath, name), root)
                    base = _file_base(q, qtokens, rel, name)
                    if base is None:
                        continue
                    project_hits.append({
                        "kind": "file", "project": entry["slug"],
                        "title": name, "snippet": rel.replace(os.sep, "/"),
                        "score": base,
                        "ref": {"task": task, "path": rel.replace(os.sep, "/")},
                    })
                if budget < 0:
                    break
        project_hits.sort(key=lambda h: -h["score"])
        out.extend(project_hits[:cap])
    return out


def _file_roots(settings: Settings, entry: dict[str, Any]) -> list[tuple[str | None, str]]:
    """(task_id | None, root) pairs: the project checkout, then its worktrees."""
    roots: list[tuple[str | None, str]] = []
    checkout = entry.get("root") or ""
    if checkout and Path(checkout).is_dir():
        roots.append((None, checkout))
    wt_dir = settings.workspace_dir / entry["slug"] / "worktrees"
    if wt_dir.is_dir():
        children = sorted(p for p in wt_dir.iterdir() if p.is_dir())
        for child in children[:_MAX_WORKTREES]:
            roots.append((child.name, str(child)))
    return roots


# ---------------------------------------------------------------------------
# Scoring / ranking
# ---------------------------------------------------------------------------

def _text_base(q: str, qtokens: list[str], title: str, hay: str) -> float | None:
    """Raw [0,1] score for a text record; None = not a hit."""
    t = " ".join(title.lower().split())
    if t and q == t:
        return 0.95  # exact title
    if t and q in t:
        return 0.8   # title substring
    if q in hay:
        return 0.65  # substring anywhere in title+prompt+summary
    if qtokens:
        toks = set(_tokenize(hay))
        matched = sum(1 for tok in qtokens if tok in toks)
        if matched:
            return 0.25 + 0.35 * (matched / len(qtokens))  # token coverage <= 0.6
    return None


def _file_base(q: str, qtokens: list[str], rel: str, name: str) -> float | None:
    """Raw [0,1] score for a file path; None = not a hit. Names beat paths."""
    n = name.lower()
    stem = n.rsplit(".", 1)[0]
    path = rel.lower().replace(os.sep, "/")
    if q in (n, stem):
        return 0.95  # exact file name
    if q in n:
        return 0.8   # name substring
    if q in path:
        return 0.6   # path substring
    if qtokens:
        toks = set(_tokenize(path))
        if all(t in toks for t in qtokens):
            return 0.55  # every query token appears somewhere in the path
    return None


def _rank(hits: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Clamp, weight by kind, deterministic merge, dedupe near-identical snippets."""
    for h in hits:
        weight = _KIND_WEIGHTS.get(h["kind"], 0.8)
        h["score"] = round(max(0.0, min(1.0, float(h["score"]))) * weight, 4)
    hits.sort(key=lambda h: (
        -h["score"], _KIND_ORDER.get(h["kind"], 9), h["project"], h["title"],
        str(sorted(h["ref"].items(), key=lambda kv: kv[0])),
    ))
    kept: list[dict[str, Any]] = []
    seen: list[tuple[str, str, set[str]]] = []  # (kind, project, snippet tokens)
    for h in hits:
        tokens = set(_tokenize(h.get("snippet") or h["title"]))
        if tokens:
            dup = False
            for kind, project, prior in seen:
                if kind != h["kind"]:
                    continue
                if kind == "file" and project != h["project"]:
                    continue  # same layout in two repos is not a duplicate
                if _near_duplicate(tokens, prior):
                    dup = True
                    break
            if dup:
                continue
        kept.append(h)
        seen.append((h["kind"], h["project"], tokens))
        if len(kept) >= limit:
            break
    return kept


def _clip(text: str, n: int) -> str:
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    return t[:n].rsplit(" ", 1)[0] + "…"
