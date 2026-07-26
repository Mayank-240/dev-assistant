"""Knowledge-graph distillation: merge/prune heuristics, safety invariants, CLI."""

import time

import pytest

from ai_dev_assistant.knowledge.distill import distill, distill_report
from ai_dev_assistant.knowledge.graph import NetworkXKnowledgeGraph


@pytest.fixture(autouse=True)
def _hash_embeddings(monkeypatch):
    """Keep implicit embedder resolution offline/deterministic in every test."""
    monkeypatch.setenv("ADA_EMBEDDINGS_BACKEND", "hash")


def _pad(kg, n=10):
    """n fresh weight-1 domain edges so the small-graph gate passes."""
    for i in range(n):
        kg.add_fact(f"pad-src-{i:02d}", "pads", f"pad-dst-{i:02d}")


def _heavy(kg, n=22):
    """A ring of n heavy nodes that fully occupy the top-20 hub protection."""
    for i in range(n):
        a, b = f"filler-{i:02d}", f"filler-{(i + 1) % n:02d}"
        kg.add_fact(a, "linked", b)
        kg._g[a][b]["linked"]["weight"] = 5


def _backdate(kg, s, d, rel, days=90):
    kg._g[s][d][rel]["last_ts"] = time.time() - days * 86400


class StubEmbedder:
    """Deterministic embedder: labels in the same alias group are identical
    (cosine 1.0); every other label gets its own orthogonal one-hot vector."""

    def __init__(self, aliases=()):
        self._group = {}
        for gi, group in enumerate(aliases):
            for label in group:
                self._group[label] = gi
        self._solo = {}
        self._base = len(aliases)

    @property
    def dim(self):
        return 256

    def embed(self, texts):
        out = []
        for t in texts:
            idx = self._group.get(t)
            if idx is None:
                idx = self._solo.setdefault(t, self._base + len(self._solo))
            vec = [0.0] * 256
            vec[idx] = 1.0
            out.append(vec)
        return out


class BrokenEmbedder:
    dim = 8

    def embed(self, texts):
        raise RuntimeError("boom")


# ---- string-level merges ----

def test_string_variant_plural_merge():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_fact("auth token", "protects", "api-gateway")
    kg.add_fact("Auth Tokens", "issued-by", "identity-service")
    report = distill_report(kg)
    assert {"keep": "auth-token", "drop": "auth-tokens",
            "reason": "string-variant"} in report["merges"]
    res = distill(kg, report=report)
    assert res["merged"] == 1
    assert "auth-tokens" not in kg._g
    rels = {(t.relation, t.object) for t in kg.facts_about("auth-token")}
    assert ("protects", "api-gateway") in rels
    assert ("issued-by", "identity-service") in rels  # drop's edge redirected


def test_string_variant_hyphen_merge():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_fact("ratelimit", "applies-to", "public-api")
    kg.add_fact("rate-limit", "configured-in", "settings-page")
    report = distill_report(kg)
    pairs = {(m["keep"], m["drop"]) for m in report["merges"]}
    assert ("ratelimit", "rate-limit") in pairs  # same weight, shorter id kept


def test_stopword_variant_merge():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_fact("cache", "backs", "sessions-store")
    kg.add_fact("the cache", "invalidated-by", "deploy-hook")
    report = distill_report(kg)
    assert {"keep": "cache", "drop": "the-cache",
            "reason": "string-variant"} in report["merges"]


# ---- semantic merges ----

def test_semantic_merge_with_stub_embedder():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_fact("JWT Auth", "secures", "api")
    kg.add_fact("JWT Authentication", "used-by", "web-ui")
    embedder = StubEmbedder(aliases=[{"JWT Auth", "JWT Authentication"}])
    report = distill_report(kg, embedder=embedder)
    sem = [m for m in report["merges"] if m["reason"] == "semantic"]
    assert sem == [{"keep": "jwt-auth", "drop": "jwt-authentication",
                    "reason": "semantic", "similarity": 1.0}]
    res = distill(kg, report=report)
    assert res["merged"] == 1
    rels = {(t.relation, t.object) for t in kg.facts_about("jwt-auth")}
    assert ("secures", "api") in rels and ("used-by", "web-ui") in rels


def test_semantic_skipped_on_hash_backend():
    # embedder=None resolves via settings; the hash backend yields meaningless
    # cosines, so semantic merging is skipped entirely (env is set by the fixture).
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_fact("user login", "guarded-by", "captcha")
    kg.add_fact("signing in users", "guarded-by", "captcha")
    report = distill_report(kg)
    assert [m for m in report["merges"] if m["reason"] == "semantic"] == []


def test_broken_embedder_skips_semantic_gracefully():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_fact("user login", "guarded-by", "captcha")
    kg.add_fact("signing in users", "guarded-by", "captcha")
    report = distill_report(kg, embedder=BrokenEmbedder())  # must not raise
    assert [m for m in report["merges"] if m["reason"] == "semantic"] == []


# ---- merge safety boundaries ----

def test_merge_never_crosses_types():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_node("token", node_type="agent")
    kg.add_fact("token", "handles", "session-mgmt")
    kg.add_fact("tokens", "expire-after", "ttl-policy")  # concept, same variant key
    kg.add_fact("src/tests.py", "covers", "auth-module")
    kg.add_fact("src/test.py", "covers", "auth-module")  # files: no plural variants
    embedder = StubEmbedder(aliases=[{"token", "tokens"}])
    report = distill_report(kg, embedder=embedder)
    touched = {m["keep"] for m in report["merges"]} | {m["drop"] for m in report["merges"]}
    assert touched.isdisjoint({"token", "tokens", "src/tests.py", "src/test.py"})
    distill(kg, report=report)
    for n in ("token", "tokens", "src/tests.py", "src/test.py"):
        assert n in kg._g


def test_run_touched_nodes_never_merged():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_fact("build-cache", "speeds-up", "ci")
    kg.add_fact("build-caches", "used-in", "deploys")
    kg.add_fact("build-caches", "produced_result_by", "s1", layer="run")
    report = distill_report(kg)
    touched = {m["keep"] for m in report["merges"]} | {m["drop"] for m in report["merges"]}
    assert touched.isdisjoint({"build-cache", "build-caches"})
    distill(kg, report=report)
    assert "build-cache" in kg._g and "build-caches" in kg._g
    assert kg._g.has_edge("build-caches", "s1", key="produced_result_by")


# ---- prunes, hubs, orphans ----

def test_stale_weight_prune_and_persistence(tmp_path):
    path = tmp_path / "kg.json"
    kg = NetworkXKnowledgeGraph(path)
    _heavy(kg)
    kg.add_fact("stale-a", "notes", "stale-b")
    _backdate(kg, "stale-a", "stale-b", "notes", days=90)
    kg.save()
    report = distill_report(kg)
    assert [(p["src"], p["dst"], p["relation"]) for p in report["prunes"]] \
        == [("stale-a", "stale-b", "notes")]
    assert report["prunes"][0]["weight"] == 1
    assert report["prunes"][0]["age_days"] > 45
    assert report["orphans"] == ["stale-a", "stale-b"]
    res = distill(kg, report=report)
    assert res["pruned"] == 1 and res["orphans_removed"] == 2
    # save() must not resurrect the pruned facts from the stale on-disk copy
    reloaded = NetworkXKnowledgeGraph(path)
    assert "stale-a" not in reloaded._g and "stale-b" not in reloaded._g
    assert reloaded.num_edges == 22


def test_hub_protection():
    kg = NetworkXKnowledgeGraph()
    _heavy(kg)
    kg.add_fact("filler-00", "annotated-by", "margin-note")  # touches a hub
    _backdate(kg, "filler-00", "margin-note", "annotated-by", days=90)
    kg.add_fact("stale-a", "notes", "stale-b")  # touches nobody important
    _backdate(kg, "stale-a", "stale-b", "notes", days=90)
    report = distill_report(kg)
    assert [(p["src"], p["dst"]) for p in report["prunes"]] == [("stale-a", "stale-b")]
    distill(kg, report=report)
    assert kg._g.has_edge("filler-00", "margin-note", key="annotated-by")


def test_fresh_and_heavy_edges_kept():
    kg = NetworkXKnowledgeGraph()
    _heavy(kg)
    kg.add_fact("fresh-a", "notes", "fresh-b")  # weight 1 but recent
    kg.add_fact("heavy-a", "notes", "heavy-b")  # old but weight >= min_keep_weight
    kg._g["heavy-a"]["heavy-b"]["notes"]["weight"] = 3
    _backdate(kg, "heavy-a", "heavy-b", "notes", days=90)
    report = distill_report(kg)
    assert report["prunes"] == []
    assert report["orphans"] == []


def test_provenance_survives_merge():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_fact("cache layer", "speeds-up", "api", source="note1", run_id="r1")
    kg.add_fact("cache-layers", "speeds-up", "api", source="note2", run_id="r2")
    res = distill(kg)
    assert res["merged"] == 1
    data = kg._g["cache-layer"]["api"]["speeds-up"]
    assert set(data["sources"]) == {"note1", "note2"}
    assert set(data["run_ids"]) == {"r1", "r2"}
    assert data["weight"] == 1  # load-merge semantics: max, not sum
    assert kg._g.nodes["cache-layer"]["label"] == "cache layer"  # keep's label wins


def test_orphan_concept_removed():
    kg = NetworkXKnowledgeGraph()
    _pad(kg)
    kg.add_node("floating-idea")
    report = distill_report(kg)
    assert report["orphans"] == ["floating-idea"]
    res = distill(kg, report=report)
    assert res["orphans_removed"] == 1
    assert "floating-idea" not in kg._g


# ---- safety invariants ----

def test_run_layer_edges_never_pruned():
    kg = NetworkXKnowledgeGraph()
    _heavy(kg)
    kg.add_fact("stale-a", "notes", "stale-b")
    _backdate(kg, "stale-a", "stale-b", "notes", days=90)
    kg.add_fact("taskx", "has_subtask", "s1", layer="run")  # stale + weight 1 too
    _backdate(kg, "taskx", "s1", "has_subtask", days=90)
    report = distill_report(kg)
    assert [(p["src"], p["dst"]) for p in report["prunes"]] == [("stale-a", "stale-b")]
    distill(kg, report=report)
    assert kg._g.has_edge("taskx", "s1", key="has_subtask")


def test_idempotent(tmp_path):
    kg = NetworkXKnowledgeGraph(tmp_path / "kg.json")
    _heavy(kg)
    kg.add_fact("auth token", "protects", "api-gateway")
    kg.add_fact("Auth Tokens", "issued-by", "identity-service")
    kg.add_fact("stale-a", "notes", "stale-b")
    _backdate(kg, "stale-a", "stale-b", "notes", days=90)
    kg.add_node("floating-idea")
    res1 = distill(kg)
    assert res1["merged"] == 1 and res1["pruned"] == 1 and res1["orphans_removed"] == 3
    report2 = distill_report(kg)
    assert report2["merges"] == [] and report2["prunes"] == [] and report2["orphans"] == []
    res2 = distill(kg)
    assert (res2["merged"], res2["pruned"], res2["orphans_removed"]) == (0, 0, 0)
    assert res2["stats_after"] == res1["stats_after"]


def test_small_graph_noop():
    kg = NetworkXKnowledgeGraph()
    kg.add_fact("a-thing", "relates-to", "b-thing")
    kg.add_fact("a-thing", "relates-to", "c-thing")
    report = distill_report(kg)
    assert "domain edges < 10" in report["reason"]
    assert report["merges"] == [] and report["prunes"] == [] and report["orphans"] == []
    res = distill(kg)
    assert (res["merged"], res["pruned"], res["orphans_removed"]) == (0, 0, 0)
    assert "reason" in res
    assert kg.num_edges == 2


# ---- CLI ----

def _cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADA_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("ADA_DOCS_DIR", str(tmp_path / "docs"))


def _build_project_graph(tmp_path):
    path = tmp_path / "data" / "projects" / "alpha" / "knowledge_graph.json"
    kg = NetworkXKnowledgeGraph(path)
    _heavy(kg)
    kg.add_fact("auth token", "protects", "api-gateway")
    kg.add_fact("Auth Tokens", "issued-by", "identity-service")
    kg.add_fact("stale-a", "notes", "stale-b")
    _backdate(kg, "stale-a", "stale-b", "notes", days=90)
    kg.save()
    return path


def test_cli_distill_dry_run_smoke(tmp_path, monkeypatch, capsys):
    _cli_env(tmp_path, monkeypatch)
    from ai_dev_assistant.cli import main

    assert main(["project", "new", "Alpha"]) == 0
    assert main(["project", "distill", "alpha", "--dry-run"]) == 0  # no graph yet
    out = capsys.readouterr().out
    assert "Nothing to do" in out

    path = _build_project_graph(tmp_path)
    before = path.read_text()
    assert main(["project", "distill", "alpha", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "keep auth-token" in out and "drop auth-tokens" in out
    assert "stale-a" in out
    assert "Dry run: nothing was changed." in out
    assert path.read_text() == before  # dry run never writes

    assert main(["project", "distill", "no-such-project", "--dry-run"]) == 2


def test_cli_distill_apply(tmp_path, monkeypatch, capsys):
    _cli_env(tmp_path, monkeypatch)
    from ai_dev_assistant.cli import main

    assert main(["project", "new", "Alpha"]) == 0
    path = _build_project_graph(tmp_path)
    assert main(["project", "distill", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "Applied: merged 1, pruned 1, removed 2 orphan(s)" in out
    reloaded = NetworkXKnowledgeGraph(path)
    assert "auth-tokens" not in reloaded._g
    assert "stale-a" not in reloaded._g
