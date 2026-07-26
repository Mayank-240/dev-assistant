"""Knowledge graph: entities (files, functions, concepts, tasks, decisions) and the
relations between them.

Backed by NetworkX in memory, serialized to JSON on disk (version-proof manual
serialization rather than nx.node_link_data). Behind the KnowledgeGraph Protocol so a
Kuzu/Neo4j backend can replace it later without touching agent code.

Node ids are canonicalized (lowercase, whitespace/underscores -> hyphens, punctuation
collapsed; "/" and "." survive so file paths stay readable) with the original text kept
as a `label` attr. Edges carry a `layer` ("domain" = agent knowledge, "run" = engine
bookkeeping), a `weight` that grows on repeat assertions, and provenance
(sources / run_ids / first_ts / last_ts).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

import networkx as nx

# Relations the engine has historically written for run bookkeeping; edges on disk
# without an explicit layer are classified "run" when their relation is one of these.
PLUMBING_RELATIONS = frozenset(
    {"has_subtask", "assigned_to", "produced_result_by", "review_status", "tests"}
)

_PROVENANCE_CAP = 10

_WS_UNDERSCORE = re.compile(r"[\s_]+")
_PUNCT_RUN = re.compile(r"[^\w/.:\-]+")  # \w is unicode-aware; keep / . : for paths + file::symbol ids
_MULTI_HYPHEN = re.compile(r"-{2,}")

_FILE_EXTS = (
    ".py", ".md", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml",
    ".html", ".css", ".txt", ".sh", ".sql", ".rs", ".go", ".java", ".cfg", ".ini",
)
# run ids look like "20260726-142530-a1b2c3" (orchestration/task.py); subtask ids are
# short "s1" / "s1-fix" shapes (engine appends "-fix" for retry subtasks).
_RUN_ID_RE = re.compile(r"^\d{8,}-[0-9a-f]{4,}")
_SUBTASK_ID_RE = re.compile(r"^s\d+(-fix)?$")

_roster_cache: frozenset[str] | None = None


def _roster_names() -> frozenset[str]:
    """Agent names from the roster, imported lazily (avoids an import cycle)."""
    global _roster_cache
    if _roster_cache is None:
        try:
            from ai_dev_assistant.agents.registry import _SPECS  # noqa: PLC0415
            _roster_cache = frozenset({s.name for s in _SPECS} | {"reviewer", "orchestrator"})
        except Exception:
            _roster_cache = frozenset()
    return _roster_cache


def canonical_id(text: str) -> str:
    """Canonical node id: lowercase, whitespace/underscores collapsed to single
    hyphens, punctuation runs collapsed to a hyphen ("/" and "." kept so file paths
    survive), leading/trailing hyphens trimmed. Idempotent."""
    s = _WS_UNDERSCORE.sub("-", str(text).strip().lower())
    s = _PUNCT_RUN.sub("-", s)
    s = _MULTI_HYPHEN.sub("-", s).strip("-")
    return s or str(text).strip().lower() or str(text)


@dataclass
class Triple:
    subject: str
    relation: str
    object: str


class KnowledgeGraph(Protocol):
    def add_node(self, node_id: str, node_type: str = "concept", **attrs: Any) -> None: ...
    def add_fact(self, subject: str, relation: str, obj: str, **attrs: Any) -> None: ...
    def facts_about(self, node_id: str) -> list[Triple]: ...
    def all_triples(self) -> list[Triple]: ...
    def save(self) -> None: ...


class NetworkXKnowledgeGraph:
    def __init__(self, path: Path | None = None, *,
                 roster: Iterable[str] | None = None) -> None:
        self._path = Path(path) if path else None
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()
        self._lock = threading.RLock()
        self._roster = frozenset(roster) if roster is not None else None
        if self._path and self._path.exists():
            self._load()

    # ---- type inference ----
    def _infer_type(self, canon_id: str) -> str:
        if "/" in canon_id or canon_id.endswith(_FILE_EXTS):
            return "file"
        if _RUN_ID_RE.match(canon_id) or _SUBTASK_ID_RE.match(canon_id):
            return "task"
        roster = self._roster if self._roster is not None else _roster_names()
        if canon_id in roster:
            return "agent"
        return "concept"

    # ---- writes ----
    def add_node(self, node_id: str, node_type: str = "concept", **attrs: Any) -> None:
        with self._lock:
            self._upsert_node(node_id, node_type, attrs)

    def _upsert_node(self, node_id: str, node_type: str, attrs: dict[str, Any]) -> str:
        canon = canonical_id(node_id)
        label = attrs.pop("label", None) or str(node_id).strip() or canon
        if canon in self._g:
            existing = self._g.nodes[canon]
            # don't downgrade a known type back to the default "concept"
            if node_type != "concept":
                existing["node_type"] = node_type
            existing.setdefault("label", label)  # first-seen label wins
            for k, v in attrs.items():
                existing[k] = v
        else:
            if node_type == "concept":  # defaulted -> infer from the id shape
                node_type = self._infer_type(canon)
            self._g.add_node(canon, node_type=node_type, label=label, **attrs)
        return canon

    def add_fact(self, subject: str, relation: str, obj: str, *, obj_type: str = "concept",
                 layer: str = "domain", source: str | None = None,
                 run_id: str | None = None, **attrs: Any) -> None:
        now = time.time()
        with self._lock:
            s = self._upsert_node(subject, "concept", {})
            o = self._upsert_node(obj, obj_type, {})
            if self._g.has_edge(s, o, key=relation):
                # repeat assertion: bump weight + merge provenance, no parallel edge
                data = self._g[s][o][relation]
                data["weight"] = int(data.get("weight", 1)) + 1
                data["last_ts"] = now
                self._merge_provenance(data, [source] if source else [],
                                       [run_id] if run_id else [])
                for k, v in attrs.items():
                    data[k] = v
            else:
                self._g.add_edge(
                    s, o, key=relation, relation=relation, layer=layer, weight=1,
                    first_ts=now, last_ts=now,
                    sources=[source] if source else [],
                    run_ids=[run_id] if run_id else [],
                    **attrs,
                )

    @staticmethod
    def _merge_provenance(data: dict[str, Any], sources: list[str],
                          run_ids: list[str]) -> None:
        for field, incoming in (("sources", sources), ("run_ids", run_ids)):
            current = list(data.get(field) or [])
            for item in incoming:
                if item and item not in current:
                    current.append(item)
            data[field] = current[:_PROVENANCE_CAP]

    # ---- reads ----
    def facts_about(self, node_id: str) -> list[Triple]:
        node = self._resolve(node_id)
        if node is None:
            return []
        triples: list[Triple] = []
        for _s, dst, data in self._g.out_edges(node, data=True):
            triples.append(Triple(node, data.get("relation", "related_to"), dst))
        for src, _d, data in self._g.in_edges(node, data=True):
            triples.append(Triple(src, data.get("relation", "related_to"), node))
        return triples

    def all_triples(self) -> list[Triple]:
        return [
            Triple(s, data.get("relation", "related_to"), d)
            for s, d, data in self._g.edges(data=True)
        ]

    def node_types(self) -> dict[str, str]:
        return {n: data.get("node_type", "concept") for n, data in self._g.nodes(data=True)}

    @property
    def num_nodes(self) -> int:
        return self._g.number_of_nodes()

    @property
    def num_edges(self) -> int:
        return self._g.number_of_edges()

    def _resolve(self, node_id: str) -> str | None:
        canon = canonical_id(node_id)
        return canon if canon in self._g else None

    # ---- query API ----
    def _node_payload(self, n: str) -> dict[str, Any]:
        data = self._g.nodes[n]
        return {"id": n, "label": data.get("label", n),
                "type": data.get("node_type", "concept"), "degree": self._g.degree(n)}

    def _edge_matches(self, data: dict[str, Any], layer: str | None) -> bool:
        return layer is None or data.get("layer", "domain") == layer

    def neighborhood(self, node_id: str, depth: int = 1,
                     layer: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Nodes within `depth` hops (either direction) of node_id, plus the induced
        edges; when `layer` is given only edges of that layer are traversed/returned."""
        with self._lock:
            start = self._resolve(node_id)
            if start is None:
                return {"nodes": [], "edges": []}
            seen: set[str] = {start}
            frontier: set[str] = {start}
            for _ in range(max(0, depth)):
                nxt: set[str] = set()
                for n in frontier:
                    for s, d, data in self._g.out_edges(n, data=True):
                        if self._edge_matches(data, layer) and d not in seen:
                            nxt.add(d)
                    for s, d, data in self._g.in_edges(n, data=True):
                        if self._edge_matches(data, layer) and s not in seen:
                            nxt.add(s)
                seen |= nxt
                frontier = nxt
            edges = [
                {"src": s, "dst": d, "relation": data.get("relation", "related_to"),
                 "weight": data.get("weight", 1), "layer": data.get("layer", "domain"),
                 "last_ts": data.get("last_ts")}
                for s, d, data in self._g.edges(data=True)
                if s in seen and d in seen and self._edge_matches(data, layer)
            ]
            return {"nodes": [self._node_payload(n) for n in sorted(seen)], "edges": edges}

    def _weighted_degrees(self, layer: str | None,
                          min_weight: int = 1) -> tuple[dict[str, int], list[tuple]]:
        """Weighted degree per node over edges matching layer/min_weight, plus the
        matching edge list [(src, dst, data), ...]."""
        wdeg: dict[str, int] = {}
        edges: list[tuple] = []
        for s, d, data in self._g.edges(data=True):
            w = int(data.get("weight", 1))
            if w < min_weight or not self._edge_matches(data, layer):
                continue
            edges.append((s, d, data))
            wdeg[s] = wdeg.get(s, 0) + w
            wdeg[d] = wdeg.get(d, 0) + w
        return wdeg, edges

    def top_nodes(self, limit: int = 25, layer: str | None = "domain") -> list[dict[str, Any]]:
        """Nodes ranked by weighted degree (sum of incident edge weights) within a layer."""
        with self._lock:
            wdeg, _ = self._weighted_degrees(layer)
            ranked = sorted(wdeg, key=lambda n: (-wdeg[n], n))[:max(0, limit)]
            return [{**self._node_payload(n), "weight": wdeg[n]} for n in ranked]

    def search_nodes(self, text: str, limit: int = 20) -> list[dict[str, Any]]:
        """Case-insensitive substring match over node id and label."""
        needle = str(text).lower()
        with self._lock:
            hits = [
                n for n, data in self._g.nodes(data=True)
                if needle in n.lower() or needle in str(data.get("label", "")).lower()
            ]
            return [self._node_payload(n) for n in sorted(hits)[:max(0, limit)]]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_type: dict[str, int] = {}
            for _n, data in self._g.nodes(data=True):
                t = data.get("node_type", "concept")
                by_type[t] = by_type.get(t, 0) + 1
            by_layer: dict[str, int] = {}
            for _s, _d, data in self._g.edges(data=True):
                layer = data.get("layer", "domain")
                by_layer[layer] = by_layer.get(layer, 0) + 1
            return {"nodes": self._g.number_of_nodes(), "edges": self._g.number_of_edges(),
                    "by_type": by_type, "by_layer": by_layer}

    def export_view(self, layer: str | None = None, min_weight: int = 1,
                    limit_nodes: int = 250) -> dict[str, list[dict[str, Any]]]:
        """Force-graph payload: the top `limit_nodes` nodes by weighted degree (over
        edges matching layer/min_weight) and their induced edges."""
        with self._lock:
            wdeg, edges = self._weighted_degrees(layer, min_weight)
            if layer is None and min_weight <= 1:
                candidates = list(self._g.nodes)  # include isolated nodes
            else:
                candidates = list(wdeg)
            ranked = sorted(candidates, key=lambda n: (-wdeg.get(n, 0), n))
            keep = set(ranked[:max(0, limit_nodes)])
            return {
                "nodes": [{**self._node_payload(n), "weight": wdeg.get(n, 0)}
                          for n in ranked[:max(0, limit_nodes)]],
                "edges": [
                    {"src": s, "dst": d, "relation": data.get("relation", "related_to"),
                     "weight": data.get("weight", 1), "layer": data.get("layer", "domain")}
                    for s, d, data in edges if s in keep and d in keep
                ],
            }

    # ---- persistence (manual; version-proof) ----
    def save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Concurrent runs each hold their own in-memory graph of the same project file;
        # merge with what's on disk under an exclusive lock so the last writer no longer
        # silently discards the other run's facts (R4).
        lock_path = self._path.with_suffix(".lock")
        with self._lock, open(lock_path, "w") as lock_fh:
            try:
                import fcntl
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass  # non-POSIX: best effort, single-writer assumption
            self._merge_from_disk()
            self._path.write_text(json.dumps(self._serialize(), indent=2))

    def _ingest_node(self, raw_id: str, attrs: dict[str, Any]) -> str:
        """Load one serialized node, normalizing legacy raw ids and merging
        collisions (earliest label wins)."""
        attrs = dict(attrs)
        node_type = attrs.pop("node_type", "concept")
        attrs.setdefault("label", str(raw_id).strip())
        canon = canonical_id(raw_id)
        if canon in self._g:
            existing = self._g.nodes[canon]
            if node_type != "concept" and existing.get("node_type", "concept") == "concept":
                existing["node_type"] = node_type
            for k, v in attrs.items():
                existing.setdefault(k, v)  # existing attrs (incl. label) win
        else:
            if node_type == "concept":
                node_type = self._infer_type(canon)
            self._g.add_node(canon, node_type=node_type, **attrs)
        return canon

    def _ingest_edge(self, raw_src: str, raw_dst: str, rel: str,
                     attrs: dict[str, Any]) -> None:
        """Load one serialized edge, normalizing endpoints; collisions merge
        (weight = max, ts span union, provenance union) instead of duplicating."""
        attrs = dict(attrs)
        s = self._ingest_node(raw_src, {})
        d = self._ingest_node(raw_dst, {})
        # legacy edges have no layer: known plumbing relations were engine bookkeeping
        layer = attrs.pop("layer", None) or ("run" if rel in PLUMBING_RELATIONS else "domain")
        weight = int(attrs.pop("weight", 1))
        first_ts = attrs.pop("first_ts", None)
        last_ts = attrs.pop("last_ts", None)
        sources = list(attrs.pop("sources", None) or [])
        legacy_source = attrs.pop("source", None)  # pre-rework kg_write passed source=
        if legacy_source:
            sources.append(legacy_source)
        run_ids = list(attrs.pop("run_ids", None) or [])
        if self._g.has_edge(s, d, key=rel):
            data = self._g[s][d][rel]
            data["weight"] = max(int(data.get("weight", 1)), weight)
            if first_ts is not None:
                data["first_ts"] = min(data.get("first_ts") or first_ts, first_ts)
            if last_ts is not None:
                data["last_ts"] = max(data.get("last_ts") or last_ts, last_ts)
            self._merge_provenance(data, sources, run_ids)
            for k, v in attrs.items():
                data.setdefault(k, v)
        else:
            self._g.add_edge(s, d, key=rel, relation=rel, layer=layer, weight=weight,
                             first_ts=first_ts, last_ts=last_ts,
                             sources=sources[:_PROVENANCE_CAP],
                             run_ids=run_ids[:_PROVENANCE_CAP], **attrs)

    def _merge_from_disk(self) -> None:
        """Union the on-disk graph into memory (in-memory attrs win on conflict)."""
        if not (self._path and self._path.exists()):
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            return
        with self._lock:
            for node in data.get("nodes", []):
                self._ingest_node(node["id"], node.get("attrs", {}))
            for edge in data.get("edges", []):
                self._ingest_edge(edge["src"], edge["dst"],
                                  edge.get("relation", "related_to"),
                                  edge.get("attrs", {}))

    def _serialize(self) -> dict:
        return {
            "nodes": [{"id": n, "attrs": dict(d)} for n, d in self._g.nodes(data=True)],
            "edges": [
                {"src": s, "dst": d, "relation": data.get("relation", "related_to"),
                 "attrs": {k: v for k, v in data.items() if k != "relation"}}
                for s, d, data in self._g.edges(data=True)
            ],
        }

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            return
        with self._lock:
            for node in data.get("nodes", []):
                self._ingest_node(node["id"], node.get("attrs", {}))
            for edge in data.get("edges", []):
                self._ingest_edge(edge["src"], edge["dst"],
                                  edge.get("relation", "related_to"),
                                  edge.get("attrs", {}))
