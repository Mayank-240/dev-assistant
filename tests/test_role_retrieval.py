"""Role-weighted code retrieval: ``retrieval_context(..., role=...)`` applies the
role's path affinities as a score BOOST (never a hard filter); unmapped roles keep
the exact pre-feature behavior. The engine passes each subtask's agent role at its
existing call site (repo-workspace runs only, so the cassette rule is untouched)."""

from __future__ import annotations

from test_code_retrieval_context import _repo_settings, _run

from ai_dev_assistant.config import Settings
from ai_dev_assistant.knowledge.code_index import (
    _ROLE_PATH_AFFINITY,
    CodeIndex,
    _path_matches_role,
)


def _index(tmp_path) -> CodeIndex:
    settings = Settings(embeddings_backend="hash", data_dir=tmp_path / "data")
    idx = CodeIndex(settings, "proj-role")
    hits = [  # best-first, as search() returns them
        {"path": "core/logic.py", "start_line": 1, "end_line": 2,
         "text": "def core():\n    pass", "score": 0.5},
        {"path": "static/app.js", "start_line": 1, "end_line": 2,
         "text": "function ui() {\n}", "score": 0.4},
    ]
    idx.search = lambda query, top_k=8: [dict(h) for h in hits]  # deterministic stub
    return idx


def test_role_boost_reorders_matching_paths(tmp_path):
    idx = _index(tmp_path)
    try:
        out = idx.retrieval_context("query", role="frontend")
        # static/app.js (0.4 * 1.5 = 0.6) overtakes core/logic.py (0.5) …
        assert out.index("--- static/app.js:") < out.index("--- core/logic.py:")
        # … but the non-matching chunk is still present: a boost, never a filter
        assert "--- core/logic.py:" in out
    finally:
        idx.close()


def test_unmapped_or_blank_role_is_byte_identical_no_bias(tmp_path):
    idx = _index(tmp_path)
    try:
        default = idx.retrieval_context("query")
        assert default.index("--- core/logic.py:") < default.index("--- static/app.js:")
        assert idx.retrieval_context("query", role="") == default
        assert "researcher" not in _ROLE_PATH_AFFINITY
        assert idx.retrieval_context("query", role="researcher") == default
    finally:
        idx.close()


def test_affinity_matching_suffix_vs_substring():
    assert _path_matches_role("db/schema.sql", _ROLE_PATH_AFFINITY["database"])
    assert _path_matches_role("ops/Dockerfile", _ROLE_PATH_AFFINITY["devops"])
    assert _path_matches_role("tests/test_x.py", _ROLE_PATH_AFFINITY["test_engineer"])
    assert not _path_matches_role("core/logic.py", _ROLE_PATH_AFFINITY["frontend"])
    # ".js" is a suffix rule: a path merely containing it elsewhere doesn't match
    assert not _path_matches_role("notes/js_history.md", _ROLE_PATH_AFFINITY["frontend"])


async def test_engine_passes_subtask_role_at_call_site(tmp_path, monkeypatch):
    seen: list[str] = []
    orig = CodeIndex.retrieval_context

    def spy(self, query, budget_chars=2000, role=""):
        seen.append(role)
        return orig(self, query, budget_chars, role=role)

    monkeypatch.setattr(CodeIndex, "retrieval_context", spy)
    await _run(_repo_settings(tmp_path))  # repo-backed run -> retrieval per subtask
    assert seen  # the engine actually reached retrieval
    assert set(seen) <= {"researcher", "coder", "documenter"}  # the plan's agent roles
