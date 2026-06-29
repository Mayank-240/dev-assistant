from ai_dev_assistant.knowledge.extract import enrich_kg_from_workspace
from ai_dev_assistant.knowledge.graph import NetworkXKnowledgeGraph
from ai_dev_assistant.memory.store import MemoryStore


def test_extract_python_entities(tmp_path):
    (tmp_path / "app.py").write_text(
        "import os\nfrom collections import deque\n\n"
        "def add(a, b):\n    return a + b\n\nclass Foo:\n    pass\n"
    )
    kg = NetworkXKnowledgeGraph()
    n = enrich_kg_from_workspace(kg, tmp_path, "task1")
    assert n == 1
    triples = {(t.subject, t.relation, t.object) for t in kg.all_triples()}
    # symbols are qualified by file (app.py::add) so same-named symbols across files don't collide
    assert ("app.py", "defines", "app.py::add") in triples
    assert ("app.py", "defines", "app.py::Foo") in triples
    assert ("app.py", "imports", "os") in triples
    assert ("app.py", "imports", "collections") in triples
    assert ("task1", "produced_file", "app.py") in triples
    # the defined symbol carries the right node type (not the default "concept")
    assert kg.node_types().get("app.py::add") == "function"
    assert kg.node_types().get("app.py::Foo") == "class"


def test_extract_skips_pycache(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_text("junk")
    (tmp_path / "main.py").write_text("def go():\n    pass\n")
    kg = NetworkXKnowledgeGraph()
    assert enrich_kg_from_workspace(kg, tmp_path, "t") == 1  # only main.py


def test_longterm_memory_recall():
    mem = MemoryStore.in_memory()
    mem.remember("longterm", "Use Docker multi-stage builds to keep images small")
    mem.remember("longterm", "Prefer pytest fixtures for database setup")
    hits = mem.recall("longterm", "docker build optimization", top_k=2)
    assert hits and "Docker" in hits[0].content
