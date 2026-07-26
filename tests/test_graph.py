import json

from ai_dev_assistant.knowledge.graph import NetworkXKnowledgeGraph, canonical_id


def test_add_fact_and_query():
    kg = NetworkXKnowledgeGraph()
    kg.add_fact("auth.py", "imports", "jwt")
    kg.add_fact("auth.py", "defines", "login")
    facts = kg.facts_about("auth.py")
    relations = {(t.subject, t.relation, t.object) for t in facts}
    assert ("auth.py", "imports", "jwt") in relations
    assert ("auth.py", "defines", "login") in relations


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "kg.json"
    kg = NetworkXKnowledgeGraph(path)
    kg.add_fact("taskA", "has_subtask", "s1")
    kg.save()

    reloaded = NetworkXKnowledgeGraph(path)
    facts = reloaded.facts_about("taskA")
    assert any(t.object == "s1" and t.relation == "has_subtask" for t in facts)
    assert reloaded.num_edges == 1


def test_case_insensitive_resolution():
    kg = NetworkXKnowledgeGraph()
    kg.add_fact("Kubernetes", "is_a", "orchestrator")
    assert kg.facts_about("kubernetes")  # resolves despite case


# ---- normalization ----

def test_canonical_id_rules():
    assert canonical_id("Roadmap Architecture") == "roadmap-architecture"
    assert canonical_id("roadmap_architecture") == "roadmap-architecture"
    assert canonical_id("  Roadmap   Architecture  ") == "roadmap-architecture"
    assert canonical_id("src/auth.py") == "src/auth.py"  # / and . survive
    assert canonical_id("(JWT tokens!)") == "jwt-tokens"
    assert canonical_id("auth.py::login") == "auth.py::login"  # : kept for file::symbol ids
    # idempotent
    for raw in ("Roadmap Architecture", "src/auth.py", "(JWT tokens!)"):
        assert canonical_id(canonical_id(raw)) == canonical_id(raw)


def test_near_duplicates_merge_in_memory():
    kg = NetworkXKnowledgeGraph()
    kg.add_fact("Roadmap Architecture", "uses", "JWT")
    kg.add_fact("roadmap_architecture", "part_of", "Backend")
    assert kg.num_nodes == 3  # roadmap-architecture, jwt, backend
    rels = {(t.relation, t.object) for t in kg.facts_about("Roadmap Architecture")}
    assert ("uses", "jwt") in rels and ("part_of", "backend") in rels
    # first-seen label wins
    view = kg.export_view()
    labels = {n["id"]: n["label"] for n in view["nodes"]}
    assert labels["roadmap-architecture"] == "Roadmap Architecture"


def test_normalization_merge_on_load(tmp_path):
    """Old-format files with near-duplicate raw ids load as one node with union edges."""
    path = tmp_path / "kg.json"
    path.write_text(json.dumps({
        "nodes": [
            {"id": "Roadmap Architecture", "attrs": {"node_type": "concept"}},
            {"id": "roadmap-architecture", "attrs": {"node_type": "concept"}},
            {"id": "JWT", "attrs": {"node_type": "concept"}},
            {"id": "Backend", "attrs": {"node_type": "concept"}},
        ],
        "edges": [
            {"src": "Roadmap Architecture", "dst": "JWT", "relation": "uses", "attrs": {}},
            {"src": "roadmap-architecture", "dst": "Backend", "relation": "part_of", "attrs": {}},
        ],
    }))
    kg = NetworkXKnowledgeGraph(path)
    assert kg.num_nodes == 3
    assert kg.num_edges == 2
    rels = {(t.relation, t.object) for t in kg.facts_about("roadmap-architecture")}
    assert rels == {("uses", "jwt"), ("part_of", "backend")}
    labels = {n["id"]: n["label"] for n in kg.export_view()["nodes"]}
    assert labels["roadmap-architecture"] == "Roadmap Architecture"  # earliest label kept


# ---- layers ----

def test_layer_default_and_kwarg():
    kg = NetworkXKnowledgeGraph()
    kg.add_fact("auth.py", "imports", "jwt")                      # default -> domain
    kg.add_fact("run-1", "has_subtask", "s1", layer="run")
    assert kg.stats()["by_layer"] == {"domain": 1, "run": 1}


def test_legacy_relation_layer_inference_on_load(tmp_path):
    """Old edges without a layer attr: known plumbing relations -> run, rest -> domain."""
    path = tmp_path / "kg.json"
    path.write_text(json.dumps({
        "nodes": [],
        "edges": [
            {"src": "taskA", "dst": "s1", "relation": "has_subtask", "attrs": {}},
            {"src": "s1", "dst": "coder", "relation": "assigned_to", "attrs": {}},
            {"src": "s1", "dst": "passed", "relation": "review_status", "attrs": {}},
            {"src": "taskA", "dst": "passed", "relation": "tests", "attrs": {}},
            {"src": "s1", "dst": "coder", "relation": "produced_result_by", "attrs": {}},
            {"src": "auth.py", "dst": "jwt", "relation": "uses", "attrs": {}},
        ],
    }))
    kg = NetworkXKnowledgeGraph(path)
    assert kg.stats()["by_layer"] == {"run": 5, "domain": 1}
    domain = kg.export_view(layer="domain")
    assert [e["relation"] for e in domain["edges"]] == ["uses"]


# ---- weight + provenance ----

def test_weight_increment_and_provenance_merge():
    kg = NetworkXKnowledgeGraph()
    kg.add_fact("auth.py", "uses", "jwt", source="coder", run_id="r1")
    kg.add_fact("auth.py", "uses", "jwt", source="architect", run_id="r2")
    assert kg.num_edges == 1  # repeat assertion merges, no parallel edge
    [edge] = kg.export_view()["edges"]
    assert edge["weight"] == 2
    data = kg._g["auth.py"]["jwt"]["uses"]
    assert data["sources"] == ["coder", "architect"]
    assert data["run_ids"] == ["r1", "r2"]
    assert data["first_ts"] <= data["last_ts"]


def test_distinct_relations_stay_parallel_edges():
    kg = NetworkXKnowledgeGraph()
    kg.add_fact("auth.py", "uses", "jwt")
    kg.add_fact("auth.py", "wraps", "jwt")
    assert kg.num_edges == 2


def test_provenance_capped_at_ten():
    kg = NetworkXKnowledgeGraph()
    for i in range(15):
        kg.add_fact("a", "uses", "b", source=f"agent{i}", run_id=f"r{i}")
    data = kg._g["a"]["b"]["uses"]
    assert len(data["sources"]) == 10
    assert len(data["run_ids"]) == 10
    assert data["weight"] == 15


# ---- type inference ----

def test_type_inference_table():
    kg = NetworkXKnowledgeGraph(roster={"coder", "reviewer"})
    cases = {
        "src/auth.py": "file",
        "notes.md": "file",
        "app.ts": "file",
        "config.json": "file",
        "20260726-142530-a1b2c3": "task",   # run-id shape (orchestration/task.py)
        "s1": "task",
        "s3-fix": "task",
        "coder": "agent",
        "jwt tokens": "concept",
    }
    for raw in cases:
        kg.add_node(raw)
    types = kg.node_types()
    for raw, expected in cases.items():
        assert types[canonical_id(raw)] == expected, raw


def test_explicit_type_wins_over_inference():
    kg = NetworkXKnowledgeGraph()
    kg.add_node("s1", "subtask")
    assert kg.node_types()["s1"] == "subtask"
    kg.add_node("s1")  # defaulted re-add must not downgrade
    assert kg.node_types()["s1"] == "subtask"


# ---- query API ----

def _demo_graph() -> NetworkXKnowledgeGraph:
    kg = NetworkXKnowledgeGraph()
    kg.add_fact("auth.py", "uses", "jwt")
    kg.add_fact("jwt", "expires_in", "1h")
    kg.add_fact("run-x", "has_subtask", "s1", layer="run")
    kg.add_fact("s1", "assigned_to", "coder", layer="run")
    return kg


def test_neighborhood_depth():
    kg = _demo_graph()
    n1 = kg.neighborhood("auth.py", depth=1)
    assert {n["id"] for n in n1["nodes"]} == {"auth.py", "jwt"}
    n2 = kg.neighborhood("auth.py", depth=2)
    assert {n["id"] for n in n2["nodes"]} == {"auth.py", "jwt", "1h"}
    assert {(e["src"], e["relation"], e["dst"]) for e in n2["edges"]} == {
        ("auth.py", "uses", "jwt"), ("jwt", "expires_in", "1h")}
    for node in n2["nodes"]:
        assert set(node) == {"id", "label", "type", "degree"}


def test_neighborhood_layer_filter():
    kg = _demo_graph()
    kg.add_fact("s1", "touches", "auth.py")  # domain edge into the run cluster
    run_view = kg.neighborhood("s1", depth=2, layer="run")
    assert {n["id"] for n in run_view["nodes"]} == {"s1", "run-x", "coder"}
    assert all(e["layer"] == "run" for e in run_view["edges"])


def test_top_nodes_and_search_and_stats():
    kg = _demo_graph()
    kg.add_fact("auth.py", "uses", "jwt")  # weight 2 now
    top = kg.top_nodes(limit=2, layer="domain")
    assert [n["id"] for n in top] == ["jwt", "auth.py"]  # jwt: 2+1 vs auth.py: 2
    assert top[0]["weight"] == 3

    hits = kg.search_nodes("auth")
    assert [h["id"] for h in hits] == ["auth.py"]

    st = kg.stats()
    assert st["nodes"] == kg.num_nodes and st["edges"] == kg.num_edges
    assert st["by_layer"] == {"domain": 2, "run": 2}
    assert st["by_type"]["file"] == 1


def test_export_view_cap_and_shape():
    kg = NetworkXKnowledgeGraph()
    for i in range(30):
        kg.add_fact("hub", "links", f"leaf {i}")
    view = kg.export_view(limit_nodes=5)
    assert len(view["nodes"]) == 5
    assert view["nodes"][0]["id"] == "hub"  # highest weighted degree first
    assert set(view["nodes"][0]) == {"id", "label", "type", "degree", "weight"}
    ids = {n["id"] for n in view["nodes"]}
    for e in view["edges"]:  # induced edges only
        assert e["src"] in ids and e["dst"] in ids
        assert set(e) == {"src", "dst", "relation", "weight", "layer"}


def test_export_view_layer_excludes_run_only_nodes():
    kg = _demo_graph()
    view = kg.export_view(layer="domain")
    ids = {n["id"] for n in view["nodes"]}
    assert ids == {"auth.py", "jwt", "1h"}  # run-x/s1/coder stay out of the domain view


# ---- persistence / migration ----

def test_old_file_roundtrip(tmp_path):
    """Old-shape files load; a save keeps them loadable with facts intact."""
    path = tmp_path / "kg.json"
    path.write_text(json.dumps({
        "nodes": [{"id": "auth.py", "attrs": {"node_type": "file"}}],
        "edges": [{"src": "auth.py", "dst": "jwt", "relation": "imports",
                   "attrs": {"source": "coder"}}],
    }))
    kg = NetworkXKnowledgeGraph(path)
    kg.add_fact("auth.py", "defines", "login")
    kg.save()

    reloaded = NetworkXKnowledgeGraph(path)
    rels = {(t.relation, t.object) for t in reloaded.facts_about("auth.py")}
    assert rels == {("imports", "jwt"), ("defines", "login")}
    assert reloaded.node_types()["auth.py"] == "file"
    # legacy single `source` attr migrated into the sources list
    assert reloaded._g["auth.py"]["jwt"]["imports"]["sources"] == ["coder"]


def test_save_merges_concurrent_writer(tmp_path):
    """Merge-on-save still unions another writer's facts instead of clobbering them."""
    path = tmp_path / "kg.json"
    a = NetworkXKnowledgeGraph(path)
    b = NetworkXKnowledgeGraph(path)
    a.add_fact("auth.py", "uses", "jwt")
    a.save()
    b.add_fact("api.py", "uses", "flask")
    b.save()
    merged = NetworkXKnowledgeGraph(path)
    triples = {(t.subject, t.relation, t.object) for t in merged.all_triples()}
    assert ("auth.py", "uses", "jwt") in triples
    assert ("api.py", "uses", "flask") in triples


def test_repeated_save_does_not_inflate_weight(tmp_path):
    path = tmp_path / "kg.json"
    kg = NetworkXKnowledgeGraph(path)
    kg.add_fact("a", "uses", "b")
    kg.save()
    kg.save()  # merge-from-disk must not double-count the same assertion
    reloaded = NetworkXKnowledgeGraph(path)
    assert reloaded._g["a"]["b"]["uses"]["weight"] == 1


# ---- contracts with the rest of the system ----

def test_kg_write_tool_definition_unchanged():
    """Cassette rule: the LLM-visible kg_write tool definition is frozen byte-for-byte."""
    from ai_dev_assistant.tools.registry import _TOOL_DEFS
    defn = next(d for d in _TOOL_DEFS if d["name"] == "kg_write")
    assert defn == {
        "name": "kg_write",
        "description": "Record a fact in the knowledge graph as a "
                       "(subject, relation, object) triple.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "relation": {"type": "string"},
                "object": {"type": "string"},
            },
            "required": ["subject", "relation", "object"],
        },
    }


def test_kg_write_handler_records_domain_layer_with_provenance(tmp_path):
    from ai_dev_assistant.tools.registry import ToolBox, ToolContext
    kg = NetworkXKnowledgeGraph()
    ctx = ToolContext(memory=None, kb=None, kg=kg, bus=None, agent_name="architect",
                      task_scope="20260726-120000-abcdef", base_dir=tmp_path)
    out = ToolBox(ctx)._kg_write(
        {"subject": "Auth Module", "relation": "uses", "object": "JWT"})
    assert "Recorded" in out
    data = kg._g["auth-module"]["jwt"]["uses"]
    assert data["layer"] == "domain"
    assert data["sources"] == ["architect"]
    assert data["run_ids"] == ["20260726-120000-abcdef"]
