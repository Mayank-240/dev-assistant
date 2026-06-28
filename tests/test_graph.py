from ai_dev_assistant.knowledge.graph import NetworkXKnowledgeGraph


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
