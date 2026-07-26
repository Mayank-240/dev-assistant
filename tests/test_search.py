"""Tests for search.global_search — one query, four modalities, every project.

Seeds two real projects (git checkouts via projects.create_project, real
MemoryStore/KnowledgeBase writes, run rows in runs.db, a task worktree) and
asserts cross-project hits per modality, filters, ranking sanity, read-only
behavior, and skip-of-missing-projects.
"""

from __future__ import annotations

import dataclasses
import subprocess
import time
from pathlib import Path

import pytest

from ai_dev_assistant import projects, search
from ai_dev_assistant.config import Settings
from ai_dev_assistant.knowledge.base import KnowledgeBase
from ai_dev_assistant.memory.store import MemoryStore
from ai_dev_assistant.orchestration.run_store import RunStore


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "ws",
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=Tester", *args],
        cwd=repo, check=True, capture_output=True,
    )


def _seed_project(settings: Settings, slug: str, *, files=None, memories=(),
                  kb_docs=(), worktree_files=()) -> None:
    """Populate a project through the real APIs: git checkout, MemoryStore, KB."""
    entry = projects.create_project(settings, slug)
    root = Path(entry["root"])
    for rel, content in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if files:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "seed files")
    s = dataclasses.replace(settings, project=slug)
    store = MemoryStore(s)
    for scope, content in memories:
        store.remember(scope, content, metadata={"author": "tester"})
    kb = KnowledgeBase(store.vectors)
    for doc_id, text in kb_docs:
        kb.ingest(doc_id, text)
    store.close()
    for task, rel, content in worktree_files:
        p = settings.workspace_dir / slug / "worktrees" / task / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _seed_run(settings: Settings, run_id: str, prompt: str, *, title=None,
              project="default", summary="", created_at=None) -> None:
    rs = RunStore(settings.data_dir / "runs.db")
    rs.start(run_id, prompt, title, project=project)
    rs.finish(run_id, summary=summary)
    if created_at is not None:
        rs._conn.execute("UPDATE runs SET created_at = ? WHERE id = ?",
                         (created_at, run_id))
        rs._conn.commit()
    rs.close()


@pytest.fixture()
def env(tmp_path) -> Settings:
    settings = _settings(tmp_path)
    _seed_project(
        settings, "proj-a",
        files={"src/postgres_utils.py": "def pool():\n    return 1\n",
               "README.md": "# proj a\n"},
        memories=[("longterm", "postgres connection pooling cuts request latency")],
        kb_docs=[("repo:docs/pg.md",
                  "postgres partial indexes speed up dashboard queries")],
        worktree_files=[("task-77", "widget_gadget.py", "x = 1\n")],
    )
    _seed_project(
        settings, "proj-b",
        files={"tools/postgres_backup.sh": "#!/bin/sh\necho backup\n"},
        memories=[("longterm",
                   "restore postgres from nightly dumps before failover drills")],
        kb_docs=[("repo:docs/vacuum.md",
                  "tuning postgres autovacuum thresholds for bulk writes"),
                 ("repo:docs/misc.md",
                  "assorted notes: cache warmup, queue depth alerts, "
                  "login audit trail, deploy cadence")],
    )
    _seed_run(settings, "run-pg-a",
              "Improve postgres query performance in the api layer",
              project="proj-a", summary="Added a covering index and query cache")
    _seed_run(settings, "run-pg-b",
              "Tune postgres autovacuum for the analytics tables",
              project="proj-b", summary="Raised autovacuum thresholds on hot tables")
    _seed_run(settings, "run-login",
              "Fix the login flow redirect loop on session expiry",
              title="Fix login flow", project="proj-a",
              summary="Redirect loop resolved by scoping the session cookie")
    _seed_run(settings, "run-pay-old", "Refactor payment retries backoff",
              title="Refactor payment retries backoff", project="proj-b",
              summary="Initial exponential backoff for the gateway",
              created_at=time.time() - 90 * 86400)
    _seed_run(settings, "run-pay-new", "Refactor payment retries with jitter",
              title="Refactor payment retries with jitter", project="proj-b",
              summary="Added jitter and a max cap to the retry schedule")
    return settings


def test_empty_query_returns_nothing(env):
    assert search.global_search(env, "") == []
    assert search.global_search(env, "   \t ") == []


def test_cross_project_hits_every_modality_with_refs(env):
    hits = search.global_search(env, "postgres")
    assert hits
    by_kind: dict[str, set[str]] = {}
    for h in hits:
        assert {"kind", "project", "title", "snippet", "score", "ref"} <= set(h)
        assert 0.0 <= h["score"] <= 1.0
        by_kind.setdefault(h["kind"], set()).add(h["project"])
    # every modality surfaces hits from BOTH projects
    for kind in ("task", "memory", "kb", "file"):
        assert {"proj-a", "proj-b"} <= by_kind.get(kind, set()), kind
    # refs carry exactly what a UI needs to open each hit
    for h in hits:
        if h["kind"] == "task":
            assert set(h["ref"]) == {"task_id"}
        elif h["kind"] == "memory":
            assert set(h["ref"]) == {"id"} and isinstance(h["ref"]["id"], int)
        elif h["kind"] == "kb":
            assert set(h["ref"]) == {"ref_id"} and "#" in h["ref"]["ref_id"]
        else:
            assert set(h["ref"]) == {"task", "path"}


def test_worktree_file_hit_carries_task_ref(env):
    hits = search.global_search(env, "widget_gadget", kinds=("file",))
    assert hits
    assert hits[0]["project"] == "proj-a"
    assert hits[0]["ref"] == {"task": "task-77", "path": "widget_gadget.py"}
    # checkout files carry task=None
    checkout = search.global_search(env, "postgres_utils", kinds=("file",))
    assert checkout and checkout[0]["ref"] == {"task": None, "path": "src/postgres_utils.py"}


def test_kinds_filter(env):
    hits = search.global_search(env, "postgres", kinds=("memory", "kb"))
    assert hits and {h["kind"] for h in hits} == {"memory", "kb"}
    only_files = search.global_search(env, "postgres", kinds=("file",))
    assert only_files and all(h["kind"] == "file" for h in only_files)


def test_projects_filter(env):
    hits = search.global_search(env, "postgres", projects_filter=["proj-b"])
    assert hits and all(h["project"] == "proj-b" for h in hits)
    assert {h["kind"] for h in hits} == {"task", "memory", "kb", "file"}


def test_missing_project_skipped(env):
    assert search.global_search(env, "postgres", projects_filter=["ghost"]) == []
    hits = search.global_search(env, "postgres", projects_filter=["ghost", "proj-a"])
    assert hits and all(h["project"] == "proj-a" for h in hits)


def test_exact_title_task_outranks_weak_kb_chunk(env):
    hits = search.global_search(env, "Fix login flow")
    assert hits[0]["kind"] == "task"
    assert hits[0]["ref"] == {"task_id": "run-login"}
    # the weak login mention in proj-b's kb still surfaces, but never outranks
    kb_scores = [h["score"] for h in hits if h["kind"] == "kb"]
    assert kb_scores
    assert max(kb_scores) < hits[0]["score"]


def test_recency_boost_orders_tasks(env):
    hits = search.global_search(env, "payment retries", kinds=("task",))
    ids = [h["ref"]["task_id"] for h in hits]
    assert set(ids) == {"run-pay-new", "run-pay-old"}
    assert ids.index("run-pay-new") < ids.index("run-pay-old")


def test_search_is_read_only(env):
    paths = [
        env.data_dir / "runs.db",
        env.projects_dir / "proj-a" / "memory.db",
        env.projects_dir / "proj-b" / "memory.db",
    ]
    before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in paths]
    search.global_search(env, "postgres")
    search.global_search(env, "fix login flow")
    search.global_search(env, "widget_gadget")
    assert [(p.stat().st_mtime_ns, p.read_bytes()) for p in paths] == before


def test_limit_caps_and_scores_descend(env):
    hits = search.global_search(env, "postgres", limit=3)
    assert len(hits) == 3
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Semantic blending (memory + kb) and lexical degradation
# ---------------------------------------------------------------------------

class _SynonymEmbedder:
    """Deterministic stub with real 'semantics': related words share an axis, so
    cosine can match stored text that has zero token overlap with the query."""

    _AXES = {"postgres": 0, "failover": 1, "resilience": 1, "standby": 1}
    dim = 3

    def embed(self, texts):
        from ai_dev_assistant.memory.embeddings import _tokenize

        out = []
        for text in texts:
            vec = [0.0, 0.0, 0.0]
            for tok in _tokenize(text):
                axis = self._AXES.get(tok)
                if axis is not None:
                    vec[axis] += 1.0
            norm = sum(x * x for x in vec) ** 0.5
            out.append([x / norm for x in vec] if norm else [0.0, 0.0, 1.0])
        return out


class _ExplodingEmbedder:
    dim = 256

    def embed(self, texts):
        raise RuntimeError("embedder down")


def _patch_embedder(monkeypatch, embedder) -> None:
    """Swap the embedder at every site global_search reaches (seed + query time)."""
    for site in ("ai_dev_assistant.memory.store.get_embedder",
                 "ai_dev_assistant.knowledge.combine.get_embedder",
                 "ai_dev_assistant.search.get_embedder"):
        monkeypatch.setattr(site, lambda _settings: embedder)


def test_semantic_hit_lexical_scoring_would_miss(tmp_path, monkeypatch):
    _patch_embedder(monkeypatch, _SynonymEmbedder())
    settings = _settings(tmp_path)
    _seed_project(
        settings, "proj-sem",
        memories=[("longterm", "restore postgres from nightly dumps before failover drills")],
        kb_docs=[("repo:docs/dr.md", "practice failover on the postgres standby weekly")],
    )
    # "resilience" appears NOWHERE in the stored text — lexical scoring alone
    # would return nothing; the cosine leg maps it onto the failover axis.
    hits = search.global_search(settings, "resilience", kinds=("memory", "kb"))
    by_kind = {h["kind"]: h for h in hits}
    assert "failover" in by_kind["memory"]["snippet"]
    assert "failover" in by_kind["kb"]["snippet"]
    assert by_kind["kb"].get("semantic") is True  # score came from the cosine leg


def test_lexical_fallback_when_embedder_raises(env, monkeypatch):
    # Seeded vectors exist (env fixture used the real hash embedder); now the
    # query-time embedder is broken — both vector modalities must degrade to
    # their lexical leg instead of raising or going dark.
    _patch_embedder(monkeypatch, _ExplodingEmbedder())
    hits = search.global_search(env, "postgres", kinds=("memory", "kb"))
    by_kind: dict[str, set[str]] = {}
    for h in hits:
        assert {"kind", "project", "title", "snippet", "score", "ref"} <= set(h)
        assert not h.get("semantic")  # no cosine leg -> nothing may claim semantic
        by_kind.setdefault(h["kind"], set()).add(h["project"])
    for kind in ("memory", "kb"):
        assert {"proj-a", "proj-b"} <= by_kind.get(kind, set()), kind
