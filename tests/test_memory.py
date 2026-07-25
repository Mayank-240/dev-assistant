from ai_dev_assistant.config import Settings
from ai_dev_assistant.memory.embeddings import active_embedder_name, get_embedder
from ai_dev_assistant.memory.store import MemoryStore


def test_remember_and_semantic_recall():
    mem = MemoryStore.in_memory()
    mem.remember("task1", "The deployment pipeline uses Docker and Kubernetes for container orchestration")
    mem.remember("task1", "User authentication relies on JWT tokens and password hashing")
    mem.remember("task1", "The frontend is built with React and TypeScript components")

    # NOTE: the offline HashingEmbedder is bag-of-words, so this checks the vector
    # plumbing/ranking with overlapping vocabulary (real semantic recall is fastembed's job).
    hits = mem.recall("task1", "docker kubernetes container orchestration pipeline", top_k=3)
    assert hits, "expected at least one recalled memory"
    assert "Kubernetes" in hits[0].content


def test_recent_returns_newest_first():
    mem = MemoryStore.in_memory()
    mem.remember("t", "first")
    mem.remember("t", "second")
    recent = mem.recent("t", limit=2)
    assert [e.content for e in recent] == ["second", "first"]


def test_blackboard_roundtrip():
    mem = MemoryStore.in_memory()
    mem.blackboard_put("k", "v", author="coder")
    assert mem.blackboard_get("k") == "v"
    assert mem.blackboard_all() == {"k": "v"}


def test_get_embedder_is_a_process_wide_singleton():
    settings = Settings(embeddings_backend="hash")
    first = get_embedder(settings)
    second = get_embedder(settings)
    assert first is second, "repeat construction must reuse the cached embedder"


def test_active_embedder_name_reports_backend():
    settings = Settings(embeddings_backend="hash")
    get_embedder(settings)
    assert active_embedder_name() == "hash"
