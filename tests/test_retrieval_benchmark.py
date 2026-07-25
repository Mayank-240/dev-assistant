"""M1: a labeled retrieval benchmark for memory recall.

A fixed set of ~20 realistic lesson sentences and labeled (query -> relevant lessons)
pairs measures precision@5 (and recall@5) for the hash embedder, comparing the hybrid
recall path (cosine + lexical, RRF-fused — what MemoryStore.recall uses) against pure
cosine. The memory APIs are exercised read-only via VectorStore.search / search_hybrid.

Assertions use a small tolerance so the benchmark ranks retrieval quality without being
flaky; the printed numbers are the tuning signal for thresholds like min_score/top_k.
"""

from __future__ import annotations

from ai_dev_assistant.memory.store import MemoryStore

_NS = "memory:longterm"
_K = 5
# Tolerance on the strict threshold assertions: the hash embedder is deterministic, but
# the floor/margin should not flake if the labeled set or scoring is lightly edited.
_TOL = 0.02

# ---- Labeled set: 20 lessons in the shape the reflector actually writes ----
LESSONS: dict[str, str] = {
    "pytest_scope": "[DO] Scope pytest runs to the test files a subtask actually touched instead of the whole suite.",
    "pytest_baseline": "[DO] Capture a baseline test run before editing so failures can be attributed to the change.",
    "flaky_sleep": "[AVOID] Do not use time.sleep in tests to wait for async work; poll with a timeout instead.",
    "docker_cache": "[DO] Order Dockerfile COPY statements so dependency layers cache across builds.",
    "docker_ports": "[AVOID] Never bind the web server to 0.0.0.0 in the Dockerfile without authentication.",
    "secrets_env": "[AVOID] Never pass API keys into subprocess environments; scrub the env before running untrusted code.",
    "secrets_logs": "[DO] Redact secret-shaped strings before writing tool output to logs or docs.",
    "routing_frontend": "[ROUTING] UI styling and component layout subtasks belong to the frontend agent, not the coder.",
    "routing_db": "[ROUTING] Schema migrations and SQL query tuning route best to the database agent.",
    "parallel_split": "[DO] Split independent modules into separate subtasks so the scheduler runs them in parallel.",
    "deps_order": "[DO] Make a documentation subtask depend on implementation subtasks, never on the test subtask.",
    "git_branch": "[DO] Deliver changes on a new git branch instead of committing to main directly.",
    "git_small": "[AVOID] Avoid giant commits mixing refactors with behavior changes; keep diffs reviewable.",
    "embeddings_fallback": "[AVOID] The hash embedding fallback silently weakens recall; surface the active embedder in run events.",
    "memory_dedup": "[DO] Deduplicate near-identical lessons before storing so recall slots are not wasted.",
    "timeout_cap": "[DO] Put a wall-clock cap on every subprocess call so a hung command cannot stall the run.",
    "lint_gate": "[DO] Treat new lint errors relative to the baseline as review failures, not decorative notes.",
    "readme_docs": "[DO] Update README usage examples whenever a public function signature changes.",
    "retry_transient": "[DO] Retry 429 and 5xx LLM errors with jittered backoff without consuming review retries.",
    "path_escape": "[AVOID] Reject file paths that resolve outside the workspace root, including symlinks.",
}

QUERIES: list[tuple[str, set[str]]] = [
    ("how should I run pytest for the files my change touched", {"pytest_scope", "pytest_baseline"}),
    ("tests are flaky because of sleeps waiting for async results", {"flaky_sleep"}),
    ("speed up docker image builds with layer caching", {"docker_cache"}),
    ("prevent leaking api keys to child processes and logs", {"secrets_env", "secrets_logs"}),
    ("which agent should handle css layout for the settings page", {"routing_frontend"}),
    ("who should write the sql schema migration", {"routing_db"}),
    ("how to structure subtasks so they run in parallel", {"parallel_split", "deps_order"}),
    ("deliver the change as a git branch with clean commits", {"git_branch", "git_small"}),
    ("recall quality dropped after the embedding model failed to load", {"embeddings_fallback"}),
    ("guard against a hung command stalling the whole run", {"timeout_cap"}),
    ("keep documentation in sync when function signatures change", {"readme_docs"}),
    ("handle rate limit errors from the llm api", {"retry_transient"}),
]


def _build_store() -> tuple[MemoryStore, dict[str, str]]:
    store = MemoryStore.in_memory()  # hash embedder — the offline/fallback path under test
    id_to_key: dict[str, str] = {}
    for key, text in LESSONS.items():
        mem_id = store.remember("longterm", text, metadata={"kind": "lesson"})
        id_to_key[str(mem_id)] = key
    return store, id_to_key


def _metrics(search_fn, id_to_key) -> tuple[float, float]:
    """Macro-averaged (precision@5, recall@5) over the labeled queries."""
    precisions: list[float] = []
    recalls: list[float] = []
    for query, relevant in QUERIES:
        hits = search_fn(_NS, query, top_k=_K)
        got = {id_to_key.get(ref) for ref, _score, _text in hits}
        found = len(got & relevant)
        precisions.append(found / _K)
        recalls.append(found / len(relevant))
    return sum(precisions) / len(precisions), sum(recalls) / len(recalls)


def test_hybrid_beats_pure_cosine_on_labeled_set():
    store, id_to_key = _build_store()
    try:
        cos_p, cos_r = _metrics(store.vectors.search, id_to_key)
        hyb_p, hyb_r = _metrics(store.vectors.search_hybrid, id_to_key)
    finally:
        store.close()

    print(f"\nretrieval benchmark (hash embedder, {len(LESSONS)} lessons, "
          f"{len(QUERIES)} labeled queries):")
    print(f"  pure cosine : precision@{_K}={cos_p:.3f}  recall@{_K}={cos_r:.3f}")
    print(f"  hybrid (RRF): precision@{_K}={hyb_p:.3f}  recall@{_K}={hyb_r:.3f}")

    # The hybrid path (what MemoryStore.recall uses) must not lose to pure cosine.
    assert hyb_p >= cos_p - _TOL, f"hybrid precision {hyb_p:.3f} < cosine {cos_p:.3f}"
    assert hyb_r >= cos_r - _TOL, f"hybrid recall {hyb_r:.3f} < cosine {cos_r:.3f}"
    # Absolute floors (with tolerance) so silent retrieval regressions fail loudly.
    assert hyb_r >= 0.9 - _TOL, f"hybrid recall@{_K} regressed: {hyb_r:.3f}"
    assert hyb_p >= 0.25 - _TOL, f"hybrid precision@{_K} regressed: {hyb_p:.3f}"


def test_recall_api_returns_relevant_lessons_first():
    """End-to-end through MemoryStore.recall (the hybrid path + recency handling)."""
    store, id_to_key = _build_store()
    try:
        hits = store.recall("longterm", "prevent leaking api keys to child processes", top_k=3)
        keys = [id_to_key[str(e.id)] for e in hits]
        assert "secrets_env" in keys or "secrets_logs" in keys
    finally:
        store.close()
