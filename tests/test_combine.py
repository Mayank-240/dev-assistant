"""Unit tests for knowledge/combine.py — read-only, multi-project KG + memory merging."""

from __future__ import annotations

import dataclasses

from ai_dev_assistant.config import Settings
from ai_dev_assistant.knowledge import combine
from ai_dev_assistant.knowledge.graph import NetworkXKnowledgeGraph
from ai_dev_assistant.memory.store import MemoryStore


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "ws",
    )


def _seed(settings: Settings, slug: str, memories=(), facts=()) -> None:
    """Populate a project's stores through the real APIs (MemoryStore / KG)."""
    s = dataclasses.replace(settings, project=slug)
    store = MemoryStore(s)
    for scope, content in memories:
        store.remember(scope, content, metadata={"author": "tester", "subtask": "s1"})
    store.close()
    kg = NetworkXKnowledgeGraph(s.graph_path)
    for subj, rel, obj in facts:
        kg.add_fact(subj, rel, obj)
    kg.save()


def _file_state(path):
    return path.stat().st_mtime_ns, path.read_bytes()


# ---- combined_triples ----

def test_combined_triples_merges_and_tags_sources(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "proj-a",
          facts=[("app.py", "imports", "flask"), ("shared", "uses", "postgres")])
    _seed(settings, "proj-b",
          facts=[("worker.py", "imports", "celery"), ("shared", "uses", "postgres")])
    out = combine.combined_triples(settings, ["proj-a", "proj-b"])
    assert out["projects"] == ["proj-a", "proj-b"]
    nodes = {n["id"]: n for n in out["nodes"]}
    assert nodes["app.py"]["sources"] == ["proj-a"]
    assert nodes["worker.py"]["sources"] == ["proj-b"]
    # a node known to both projects carries both slugs
    assert nodes["shared"]["sources"] == ["proj-a", "proj-b"]
    edges = {(e["source"], e["target"], e["relation"]): e for e in out["edges"]}
    assert edges[("app.py", "flask", "imports")]["sources"] == ["proj-a"]
    assert edges[("worker.py", "celery", "imports")]["sources"] == ["proj-b"]
    assert edges[("shared", "postgres", "uses")]["sources"] == ["proj-a", "proj-b"]
    # same item shape /api/graph serves today, plus the additive "sources"
    for n in out["nodes"]:
        assert {"id", "type", "sources"} <= set(n)
    for e in out["edges"]:
        assert {"source", "target", "relation", "sources"} <= set(e)


def test_combined_triples_skips_missing_projects(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "proj-a", facts=[("a", "rel", "b")])
    out = combine.combined_triples(settings, ["ghost", "proj-a", "phantom"])
    assert out["projects"] == ["proj-a"]
    assert len(out["edges"]) == 1


def test_combined_triples_empty_and_unknown_inputs(tmp_path):
    settings = _settings(tmp_path)
    assert combine.combined_triples(settings, []) == {
        "projects": [], "nodes": [], "edges": []}
    assert combine.combined_triples(settings, ["ghost"]) == {
        "projects": [], "nodes": [], "edges": []}


def test_combined_triples_is_read_only(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "proj-a", facts=[("a", "rel", "b")])
    kg_path = settings.projects_dir / "proj-a" / "knowledge_graph.json"
    before = _file_state(kg_path)
    combine.combined_triples(settings, ["proj-a"])
    assert _file_state(kg_path) == before


# ---- combined_memory_search ----

def test_combined_memory_search_merges_sorts_tags_read_only(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "proj-a", memories=[
        ("longterm", "postgres connection pooling saves latency"),
        ("longterm", "css grid beats floats for layout"),
    ])
    _seed(settings, "proj-b", memories=[
        ("longterm", "postgres partial indexes speed up postgres queries"),
    ])
    db_a = settings.projects_dir / "proj-a" / "memory.db"
    db_b = settings.projects_dir / "proj-b" / "memory.db"
    before = (_file_state(db_a), _file_state(db_b))
    hits = combine.combined_memory_search(
        settings, ["proj-a", "ghost", "proj-b"], "postgres", top_k=10)
    # strictly read-only: neither database's mtime nor bytes changed
    assert (_file_state(db_a), _file_state(db_b)) == before
    # hits from both projects, each tagged with its source project
    assert {h["project"] for h in hits if "postgres" in h["content"]} == {"proj-a", "proj-b"}
    # merged by score, descending
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    for h in hits:
        assert {"id", "scope", "content", "author", "subtask",
                "created_at", "mem_scope", "score", "project"} <= set(h)
        assert h["mem_scope"] == "project"


def test_combined_memory_search_dedupes_across_projects(tmp_path):
    settings = _settings(tmp_path)
    lesson = "always pin dependency versions in ci builds"
    _seed(settings, "proj-a", memories=[("longterm", lesson)])
    _seed(settings, "proj-b", memories=[("longterm", lesson)])
    hits = combine.combined_memory_search(
        settings, ["proj-a", "proj-b"], "pin dependency versions", top_k=10)
    matching = [h for h in hits if h["content"] == lesson]
    assert len(matching) == 1  # near-identical content collapses to one hit
    assert matching[0]["project"] == "proj-a"  # first (stable-sorted) copy wins


def test_combined_memory_search_kind_filters_scope(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "proj-a", memories=[
        ("longterm", "postgres tuning lesson"),
        ("task-1", "postgres scratch note for a task"),
    ])
    hits = combine.combined_memory_search(settings, ["proj-a"], "postgres", kind="longterm")
    assert hits and all(h["scope"] == "longterm" for h in hits)


def test_combined_memory_search_missing_projects_yield_empty(tmp_path):
    settings = _settings(tmp_path)
    assert combine.combined_memory_search(settings, ["ghost", "phantom"], "anything") == []
    assert combine.combined_memory_search(settings, [], "anything") == []


# ---- combined_memory_recent (the no-query listing counterpart) ----

def test_combined_memory_recent_lists_tags_and_sorts(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "proj-a", memories=[("longterm", "lesson from project a")])
    _seed(settings, "proj-b", memories=[("longterm", "lesson from project b")])
    rows = combine.combined_memory_recent(settings, ["proj-a", "ghost", "proj-b"])
    assert {r["project"] for r in rows} == {"proj-a", "proj-b"}
    assert all(r["mem_scope"] == "project" for r in rows)
    created = [r["created_at"] for r in rows]
    assert created == sorted(created, reverse=True)  # newest first
    for r in rows:
        assert {"id", "scope", "content", "author", "subtask",
                "created_at", "mem_scope", "project"} <= set(r)
