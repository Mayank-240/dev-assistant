"""Knowledge graph: entities (files, functions, concepts, tasks, decisions) and the
relations between them.

Backed by NetworkX in memory, serialized to JSON on disk (version-proof manual
serialization rather than nx.node_link_data). Behind the KnowledgeGraph Protocol so a
Kuzu/Neo4j backend can replace it later without touching agent code.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import networkx as nx


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
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()
        self._lock = threading.RLock()
        if self._path and self._path.exists():
            self._load()

    # ---- writes ----
    def add_node(self, node_id: str, node_type: str = "concept", **attrs: Any) -> None:
        with self._lock:
            # don't downgrade a known type back to the default "concept"
            if node_id in self._g and node_type == "concept":
                node_type = self._g.nodes[node_id].get("node_type", "concept")
            self._g.add_node(node_id, node_type=node_type, **attrs)

    def add_fact(self, subject: str, relation: str, obj: str, *, obj_type: str = "concept",
                 **attrs: Any) -> None:
        with self._lock:
            if subject not in self._g:
                self.add_node(subject)
            if obj not in self._g:
                self.add_node(obj, obj_type)
            self._g.add_edge(subject, obj, key=relation, relation=relation, **attrs)

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
        if node_id in self._g:
            return node_id
        lowered = node_id.lower()
        for n in self._g.nodes:
            if str(n).lower() == lowered:
                return n
        return None

    # ---- persistence (manual; version-proof) ----
    def save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = self._serialize()
        self._path.write_text(json.dumps(data, indent=2))

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
        data = json.loads(self._path.read_text())
        for node in data.get("nodes", []):
            self._g.add_node(node["id"], **node.get("attrs", {}))
        for edge in data.get("edges", []):
            rel = edge.get("relation", "related_to")
            self._g.add_edge(edge["src"], edge["dst"], key=rel, relation=rel, **edge.get("attrs", {}))
