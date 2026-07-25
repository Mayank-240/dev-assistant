"""Retrieval sanity evals (M1, partial): a small labeled fixture of lessons + queries.

Runs against the deterministic HashingEmbedder plus the lexical leg, so results are
stable offline: obviously-related lessons must be recalled, unrelated ones must not,
cross-scope duplicates must collapse, and the store cap must evict.
"""

from ai_dev_assistant.memory.store import MemoryStore, ScopedMemory

LESSONS = [
    "Use Docker multi-stage builds to keep image size small",
    "Prefer pytest fixtures over setup methods for database tests",
    "React components should use hooks for state management",
    "Pin dependency versions in requirements files to avoid breakage",
]


def _seeded_store() -> MemoryStore:
    mem = MemoryStore.in_memory()
    for lesson in LESSONS:
        mem.remember("longterm", lesson)
    return mem


def test_related_lesson_recalled_first():
    mem = _seeded_store()
    hits = mem.recall("longterm", "docker image size optimization", top_k=3, min_score=0.2)
    assert hits, "expected the docker lesson to be recalled"
    assert "Docker" in hits[0].content


def test_unrelated_lessons_not_recalled():
    mem = _seeded_store()
    hits = mem.recall("longterm", "docker image size optimization", top_k=3, min_score=0.2)
    assert all("React" not in h.content for h in hits)
    hits = mem.recall("longterm", "pytest database fixture setup", top_k=3, min_score=0.2)
    assert hits and "pytest" in hits[0].content
    assert all("Docker" not in h.content for h in hits)


def test_cross_scope_duplicates_collapse():
    project, glob = MemoryStore.in_memory(), MemoryStore.in_memory()
    dup = "Always run the linter before committing generated code"
    project.remember("longterm", dup)
    glob.remember("longterm", dup)
    project.remember("longterm", "Cache pip downloads to speed up CI builds")

    scoped = ScopedMemory(project, glob)
    hits = scoped.recall("longterm", "run linter before committing", top_k=5)
    dup_hits = [h for h in hits if h.content == dup]
    assert len(dup_hits) == 1, "duplicate lesson must occupy only one slot"
    # the freed slot goes to the next-best distinct lesson
    assert any("pip" in h.content for h in hits)


def test_near_identical_cross_scope_variants_collapse():
    project, glob = MemoryStore.in_memory(), MemoryStore.in_memory()
    project.remember("longterm", "Use Docker multi-stage builds to keep image size small")
    glob.remember("longterm", "use docker multi-stage builds, to keep image size small!")

    scoped = ScopedMemory(project, glob)
    hits = scoped.recall("longterm", "docker multi-stage builds image size", top_k=5)
    assert len(hits) == 1


def test_cap_evicts_oldest_entries(monkeypatch):
    monkeypatch.setenv("ADA_MEMORY_MAX_ENTRIES", "5")
    mem = MemoryStore.in_memory()
    contents = [f"lesson number {i} about subject-{i}" for i in range(8)]
    for c in contents:
        mem.remember("s", c)

    remaining = [e.content for e in mem.recent("s", limit=20)]
    assert len(remaining) == 5
    assert contents[7] in remaining and contents[0] not in remaining
    # the evicted entry's vector is gone too — recall can't resurrect it
    hits = mem.recall("s", contents[0], top_k=10)
    assert all(h.content != contents[0] for h in hits)


def test_cap_disabled_when_nonpositive(monkeypatch):
    monkeypatch.setenv("ADA_MEMORY_MAX_ENTRIES", "0")
    mem = MemoryStore.in_memory()
    for i in range(12):
        mem.remember("s", f"entry {i}")
    assert len(mem.recent("s", limit=50)) == 12
