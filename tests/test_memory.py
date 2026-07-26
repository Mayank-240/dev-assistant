import dataclasses

from ai_dev_assistant.config import Settings
from ai_dev_assistant.memory.embeddings import active_embedder_name, get_embedder
from ai_dev_assistant.memory.store import (
    MemoryStore,
    count_project_memories,
    delete_project_memory,
    list_project_memories,
    update_project_memory,
)


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


# ---- curation: list / update / delete / count ----

def test_curation_list_newest_first_paginated_and_counted():
    mem = MemoryStore.in_memory()
    ids = [mem.remember("longterm", f"lesson number {i} about retries") for i in range(3)]
    scoped = mem.remember("task1", "scoped working note")

    rows = mem.list_memories()
    assert [r["id"] for r in rows] == [scoped, ids[2], ids[1], ids[0]]
    assert {"id", "scope", "key", "content", "metadata", "created_at"} <= set(rows[0])
    assert rows[0]["scope"] == "task1" and rows[0]["content"] == "scoped working note"

    assert [r["id"] for r in mem.list_memories(scope="longterm")] == ids[::-1]
    assert [r["id"] for r in mem.list_memories(limit=2, offset=1)] == [ids[2], ids[1]]
    assert mem.count_memories() == 4
    assert mem.count_memories(scope="longterm") == 3
    assert mem.count_memories(scope="ghost") == 0


def test_curation_update_roundtrips_and_reembeds():
    mem = MemoryStore.in_memory()
    mid = mem.remember("longterm", "docker layer caching speeds up builds")
    assert mem.update_memory(mid, "terraform state locking prevents concurrent applies")
    assert not mem.update_memory(9999, "no such id")

    row = mem.list_memories()[0]
    assert row["id"] == mid and "terraform" in row["content"]
    # recall follows the NEW text (vector re-embedded, lexical text rewritten) …
    hits = mem.recall("longterm", "terraform state locking concurrent applies",
                      top_k=3, min_score=0.2)
    assert hits and hits[0].id == mid and "terraform" in hits[0].content
    # … and the old text no longer matches anything.
    assert not mem.recall("longterm", "docker layer caching speeds up builds",
                          top_k=3, min_score=0.2)


def test_curation_update_survives_embedder_failure():
    mem = MemoryStore.in_memory()
    mid = mem.remember("longterm", "docker layer caching speeds up builds")

    class _Boom:
        dim = 256

        def embed(self, texts):
            raise RuntimeError("embedder down")

    real = mem.vectors._embedder
    mem.vectors._embedder = _Boom()
    assert mem.update_memory(mid, "terraform state locking prevents concurrent applies")
    mem.vectors._embedder = real
    # the vector row's text was refreshed, so the lexical leg still finds it
    hits = mem.recall("longterm", "terraform state locking", top_k=3, min_score=0.2)
    assert hits and hits[0].id == mid and "terraform" in hits[0].content


def test_curation_delete_removes_from_recall():
    mem = MemoryStore.in_memory()
    keep = mem.remember("longterm", "pin python dependencies with a lockfile")
    drop = mem.remember("longterm", "docker multi stage builds shrink images")

    assert mem.delete_memory(drop)
    assert not mem.delete_memory(drop)  # already gone
    assert mem.count_memories() == 1
    assert [r["id"] for r in mem.list_memories()] == [keep]
    # the vector went with it — recall can never resurface the deleted entry
    hits = mem.recall("longterm", "docker multi stage builds shrink images", top_k=5)
    assert all(h.id != drop for h in hits)


def test_project_curation_helpers_roundtrip(tmp_path):
    settings = Settings(
        embeddings_backend="hash", data_dir=tmp_path / "data",
        docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    store = MemoryStore(dataclasses.replace(settings, project="proj-x"))
    first = store.remember("longterm", "first lesson about retries")
    second = store.remember("longterm", "second lesson about logging")
    store.close()

    rows = list_project_memories(settings, "proj-x")
    assert [r["id"] for r in rows] == [second, first]
    assert all(r["project"] == "proj-x" for r in rows)
    assert count_project_memories(settings, "proj-x") == 2

    assert update_project_memory(settings, "proj-x", first, "first lesson, now about backoff")
    updated = {r["id"]: r["content"] for r in list_project_memories(settings, "proj-x")}
    assert "backoff" in updated[first]

    assert delete_project_memory(settings, "proj-x", second)
    assert count_project_memories(settings, "proj-x") == 1
    assert [r["id"] for r in list_project_memories(settings, "proj-x")] == [first]

    # unknown slug: empty results, False, and no memory.db created as a side effect
    assert list_project_memories(settings, "ghost") == []
    assert count_project_memories(settings, "ghost") == 0
    assert not update_project_memory(settings, "ghost", 1, "x")
    assert not delete_project_memory(settings, "ghost", 1)
    assert not (settings.projects_dir / "ghost" / "memory.db").exists()
