"""Knowledge-graph distillation: consolidation + pruning that keeps the domain layer
sharp as runs accumulate.

Pure heuristics plus local embeddings — NO LLM calls, so a pass is free and
deterministic. Two entry points, both exported for the CLI today and the web endpoint
in the next phase:

  distill_report(graph, ...) -> dry-run dict of proposed merges / prunes / orphans
  distill(graph, ...)        -> apply a report (recomputed when not given), then save

Merge heuristics (domain layer only):
  - string variants: same canonical id modulo hyphens, plural 's', or stopword tokens
    ("auth-token" / "auth-tokens" / "the-auth-token"). Concepts get the full rule;
    file/task/agent nodes only the hyphen-collapse form (paths like "tests/" vs
    "test/" are genuinely different things).
  - semantic: embedding cosine over node labels >= `similarity`, concept nodes only,
    reusing the memory embeddings machinery. The hash backend (or an embedder
    failure) skips semantic merges gracefully — hash vectors carry no meaning
    between distinct labels.

Prune heuristics: domain edges with weight < `min_keep_weight` whose last_ts is older
than `stale_days`, unless an endpoint is a protected hub (the top ~20 nodes by
weighted domain degree); orphan concept nodes left at degree 0 are dropped.

Safety invariants (tested):
  - run-layer edges are NEVER touched: prunes only select domain edges, and nodes
    with any run-layer edge are engine-owned and excluded from merges;
  - a merge never crosses node types;
  - the pass is idempotent — a second run proposes nothing;
  - an empty/small graph (< 10 domain edges) no-ops with a reason.
"""

from __future__ import annotations

import time
from typing import Any

from .graph import NetworkXKnowledgeGraph

#: Below this many domain edges there is nothing worth distilling.
MIN_DOMAIN_EDGES = 10
#: How many top weighted-degree domain nodes are protected from pruning.
HUB_PROTECT = 20

# Connective tokens that don't change a concept's identity ("the-cache" == "cache").
_STOPWORDS = frozenset({"a", "an", "the", "of", "for", "to", "in", "on", "and", "or", "with"})

_PROVENANCE_FIELDS = ("weight", "first_ts", "last_ts", "sources", "run_ids")


def _depluralize(token: str) -> str:
    """Strip a trailing plural 's' from tokens long enough for it to be a plural
    (not "is"/"as", not "-ss" words like "class")."""
    if len(token) >= 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _variant_key(canon_id: str, node_type: str) -> str:
    """Collapse a canonical id to its variant-insensitive key. Concepts drop
    stopwords and plural 's'; file/task/agent ids only lose hyphens (exact-modulo-
    variants: path/id spelling is meaningful there)."""
    tokens = canon_id.split("-")
    if node_type == "concept":
        tokens = [_depluralize(t) for t in tokens if t not in _STOPWORDS]
    return "".join(tokens)


def _domain_view(g: Any) -> tuple[list[tuple], set[str], dict[str, int]]:
    """(domain_edges [(src, dst, key, data)], nodes touching run edges,
    weighted domain degree per node)."""
    domain_edges: list[tuple] = []
    run_touched: set[str] = set()
    wdeg: dict[str, int] = {}
    for s, d, k, data in g.edges(keys=True, data=True):
        if data.get("layer", "domain") == "run":
            run_touched.add(s)
            run_touched.add(d)
            continue
        domain_edges.append((s, d, k, data))
        w = int(data.get("weight", 1))
        wdeg[s] = wdeg.get(s, 0) + w
        wdeg[d] = wdeg.get(d, 0) + w
    return domain_edges, run_touched, wdeg


def _semantic_pairs(graph: NetworkXKnowledgeGraph, nodes: list[str], embedder: Any,
                    similarity: float) -> list[tuple[str, str, float]]:
    """Concept-node pairs whose label embeddings have cosine >= similarity, sorted
    by descending similarity. Empty when embeddings are unavailable or meaningless
    (hash backend), mirroring how search degrades when its embedder fails."""
    if len(nodes) < 2:
        return []
    if embedder is None:
        try:
            from ..config import Settings  # noqa: PLC0415 — lazy, mirrors search.py
            from ..memory.embeddings import active_embedder_name, get_embedder  # noqa: PLC0415

            embedder = get_embedder(Settings.load())
            if active_embedder_name().startswith("hash"):
                return []  # hash vectors: cosine between distinct labels is noise
        except Exception:  # noqa: BLE001 — embedding must never break distillation
            return []
    labels = [str(graph._g.nodes[n].get("label", n)) for n in nodes]
    try:
        import numpy as np  # noqa: PLC0415

        vecs = np.asarray(embedder.embed(labels), dtype="float32")
    except Exception:  # noqa: BLE001 — a broken embedder just skips semantic merges
        return []
    if vecs.ndim != 2 or vecs.shape[0] != len(nodes):
        return []
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms
    sims = vecs @ vecs.T
    out: list[tuple[str, str, float]] = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            s = float(sims[i, j])
            if s >= similarity:
                out.append((nodes[i], nodes[j], s))
    out.sort(key=lambda t: (-t[2], t[0], t[1]))
    return out


def distill_report(graph: NetworkXKnowledgeGraph, *, embedder: Any = None,
                   similarity: float = 0.92, stale_days: int = 45,
                   min_keep_weight: int = 2) -> dict[str, Any]:
    """DRY RUN: propose merges / prunes / orphan removals without changing anything.

    Shape: {"merges": [{keep, drop, reason, similarity?}],
            "prunes": [{src, dst, relation, weight, age_days}],
            "orphans": [node_id, ...], "stats_before": {...}, "reason"?: str}.
    """
    g = graph._g  # sibling-module access; graph.py itself stays untouched
    with graph._lock:
        report: dict[str, Any] = {"merges": [], "prunes": [], "orphans": [],
                                  "stats_before": graph.stats()}
        domain_edges, run_touched, wdeg = _domain_view(g)
        if len(domain_edges) < MIN_DOMAIN_EDGES:
            report["reason"] = (f"graph too small to distill "
                                f"({len(domain_edges)} domain edges < {MIN_DOMAIN_EDGES})")
            return report

        types = graph.node_types()
        # Merge candidates live on the domain layer; anything touching a run-layer
        # edge is engine bookkeeping and never merged (run edges stay untouched).
        candidates = sorted(n for n in wdeg if n not in run_touched)

        def _rank(n: str) -> tuple:  # keeper = heavier, then shorter, then a-z
            return (-wdeg.get(n, 0), len(n), n)

        merged_away: dict[str, str] = {}  # drop -> keep (chain-resolvable)

        def _resolve(n: str) -> str:
            while n in merged_away:
                n = merged_away[n]
            return n

        merges: list[dict[str, Any]] = []
        # (a) string-level variants, grouped per node type (never cross-type)
        groups: dict[tuple[str, str], list[str]] = {}
        for n in candidates:
            key = _variant_key(n, types.get(n, "concept"))
            if key:
                groups.setdefault((types.get(n, "concept"), key), []).append(n)
        for _gk, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            members.sort(key=_rank)
            keep = members[0]
            for drop in members[1:]:
                merged_away[drop] = keep
                merges.append({"keep": keep, "drop": drop, "reason": "string-variant"})

        # (b) semantic near-duplicates — concept nodes only
        sem_nodes = [n for n in candidates
                     if types.get(n) == "concept" and n not in merged_away]
        for a, b, sim in _semantic_pairs(graph, sem_nodes, embedder, similarity):
            ra, rb = _resolve(a), _resolve(b)
            if ra == rb:
                continue
            keep, drop = sorted((ra, rb), key=_rank)
            merged_away[drop] = keep
            merges.append({"keep": keep, "drop": drop, "reason": "semantic",
                           "similarity": round(sim, 3)})
        report["merges"] = merges

        # Prunes: stale low-weight domain edges away from hubs. Edges whose
        # endpoints are being merged are left alone this pass (they get
        # redirected by the merge; a later pass can still prune them).
        now = time.time()
        hubs = {t["id"] for t in graph.top_nodes(HUB_PROTECT, layer="domain")}
        prunes: list[dict[str, Any]] = []
        prune_keys: set[tuple[str, str, str]] = set()
        for s, d, k, data in domain_edges:
            if int(data.get("weight", 1)) >= min_keep_weight:
                continue
            last_ts = data.get("last_ts")
            if not last_ts:
                continue  # unknown age: can't prove staleness, keep it
            age_days = (now - float(last_ts)) / 86400.0
            if age_days <= stale_days or s in hubs or d in hubs:
                continue
            if _resolve(s) != s or _resolve(d) != d:
                continue
            prunes.append({"src": s, "dst": d, "relation": data.get("relation", k),
                           "weight": int(data.get("weight", 1)),
                           "age_days": round(age_days, 1)})
            prune_keys.add((s, d, k))
        report["prunes"] = prunes

        # Orphans: concept nodes already at degree 0, or left there once every
        # incident edge is pruned. Merge participants are excluded (drops vanish
        # via the merge; keeps gain edges).
        merge_parties = set(merged_away) | set(merged_away.values())
        orphans: list[str] = []
        for n, ndata in g.nodes(data=True):
            if ndata.get("node_type", "concept") != "concept":
                continue
            if n in merge_parties or n in hubs:
                continue
            incident = ({(s, d, k) for s, d, k in g.out_edges(n, keys=True)}
                        | {(s, d, k) for s, d, k in g.in_edges(n, keys=True)})
            if not incident or incident <= prune_keys:
                orphans.append(n)
        report["orphans"] = sorted(orphans)
        return report


def _merge_nodes(g: Any, keep: str, drop: str) -> None:
    """Redirect all of drop's edges onto keep, then remove drop. Colliding edges
    merge exactly like the graph's load-merge (weight = max, ts span union,
    provenance union); edges between keep and drop collapse instead of becoming
    self-loops. keep's own attrs (label included) win by simply staying put."""
    seen: set[tuple[str, str, str]] = set()
    edges: list[tuple[str, str, str, dict[str, Any]]] = []
    for s, d, k, data in list(g.out_edges(drop, keys=True, data=True)) + \
            list(g.in_edges(drop, keys=True, data=True)):
        if (s, d, k) in seen:  # self-loops show up in both directions
            continue
        seen.add((s, d, k))
        edges.append((s, d, k, dict(data)))
    g.remove_node(drop)
    for s, d, k, data in edges:
        ns = keep if s == drop else s
        nd = keep if d == drop else d
        if ns == nd:
            continue  # the keep<->drop edge itself: collapse, don't self-loop
        if g.has_edge(ns, nd, key=k):
            tgt = g[ns][nd][k]
            tgt["weight"] = max(int(tgt.get("weight", 1)), int(data.get("weight", 1)))
            for field, pick in (("first_ts", min), ("last_ts", max)):
                v = data.get(field)
                if v is not None:
                    cur = tgt.get(field)
                    tgt[field] = v if cur is None else pick(cur, v)
            NetworkXKnowledgeGraph._merge_provenance(
                tgt, list(data.get("sources") or []), list(data.get("run_ids") or []))
            for kk, vv in data.items():
                if kk not in _PROVENANCE_FIELDS:
                    tgt.setdefault(kk, vv)
        else:
            g.add_edge(ns, nd, key=k, **data)


def distill(graph: NetworkXKnowledgeGraph, report: dict[str, Any] | None = None, *,
            embedder: Any = None, similarity: float = 0.92, stale_days: int = 45,
            min_keep_weight: int = 2) -> dict[str, Any]:
    """Apply a distillation report (recomputed when not given) and persist.

    Returns {"merged": n, "pruned": n, "orphans_removed": n, "stats_after": {...}}
    plus a "reason" when the graph was too small to touch.
    """
    if report is None:
        report = distill_report(graph, embedder=embedder, similarity=similarity,
                                stale_days=stale_days, min_keep_weight=min_keep_weight)
    g = graph._g
    with graph._lock:
        if report.get("reason"):
            return {"merged": 0, "pruned": 0, "orphans_removed": 0,
                    "stats_after": graph.stats(), "reason": report["reason"]}

        merged = 0
        for m in report.get("merges", []):
            keep, drop = m.get("keep"), m.get("drop")
            if not keep or not drop or keep == drop or keep not in g or drop not in g:
                continue
            if (g.nodes[keep].get("node_type", "concept")
                    != g.nodes[drop].get("node_type", "concept")):
                continue  # safety: a merge never crosses types
            drop_edges = (list(g.out_edges(drop, keys=True, data=True))
                          + list(g.in_edges(drop, keys=True, data=True)))
            if any(edata.get("layer", "domain") == "run" for *_ends, edata in drop_edges):
                continue  # safety: run-layer edges are never redirected
            _merge_nodes(g, keep, drop)
            merged += 1

        pruned = 0
        for p in report.get("prunes", []):
            s, d, rel = p.get("src"), p.get("dst"), p.get("relation")
            data = g.get_edge_data(s, d, rel) if s and d and rel else None
            if not data or data.get("layer", "domain") != "domain":
                continue  # safety: run-layer edges are never pruned
            g.remove_edge(s, d, key=rel)
            pruned += 1

        orphans_removed = 0
        for n in report.get("orphans", []):
            if (n in g and g.degree(n) == 0
                    and g.nodes[n].get("node_type", "concept") == "concept"):
                g.remove_node(n)
                orphans_removed += 1

        # graph.save() unions the on-disk file back into memory (concurrent-writer
        # safety), which would resurrect everything this pass just removed.
        # Distillation is deliberately destructive and manually triggered, so drop
        # the stale on-disk copy first; save() then persists the distilled graph.
        if graph._path and graph._path.exists():
            graph._path.unlink()
        graph.save()
        return {"merged": merged, "pruned": pruned,
                "orphans_removed": orphans_removed, "stats_after": graph.stats()}
