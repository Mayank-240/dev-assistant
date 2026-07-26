"""Roster pre-filtering for the orchestrator's PLANNING catalog.

Selection = core set + all customs + top task-relevant others up to
``settings.roster_max`` (0 disables). Scoring blends embedding cosine with lexical
token overlap and degrades to lexical-only on the hash backend (repo convention).
Filtering changes only the catalog TEXT — routing/sanitize still accept every
constructed agent. All offline; the stub embedder is the injection point.
"""

from __future__ import annotations

import dataclasses

from ai_dev_assistant.agents.base import AgentProfile, BaseAgent
from ai_dev_assistant.agents.orchestrator import (
    CORE_ROSTER,
    Orchestrator,
    demoted_roles,
    score_agents,
    select_roster,
)
from ai_dev_assistant.config import Settings


def _settings(**overrides) -> Settings:
    return dataclasses.replace(Settings(embeddings_backend="hash"), **overrides)


def _agent(name: str, description: str, when: str = "") -> BaseAgent:
    return BaseAgent(
        AgentProfile(name=name, description=description, when_to_use=when or description),
        system_prompt="irrelevant", model="m")


# Real built-in role names (custom detection goes through builtin_agent_names), with
# descriptions this test controls so ranking is independent of registry wording.
_OTHERS = {
    "frontend": "Builds the user interface pages: markup, styling, components.",
    "performance": "Profiles bottlenecks and proposes throughput optimizations.",
    "database": "Designs database schema migrations and writes SQL queries.",
    "devops": "Maintains build pipelines, containers, and deploy automation.",
    "security_auditor": "Reviews for vulnerabilities and unsafe patterns.",
    "api_designer": "Designs endpoint contracts and payload versioning.",
    "migrator": "Executes framework and dependency upgrade sequences.",
}


def _roster(extra: dict[str, str] | None = None) -> dict[str, BaseAgent]:
    agents = {name: _agent(name, f"The {name} core specialist.") for name in CORE_ROSTER}
    for name, desc in {**_OTHERS, **(extra or {})}.items():
        agents[name] = _agent(name, desc)
    return agents  # 12 agents (+extras) vs the default roster_max of 10


class _StubEmbedder:
    """Deterministic: texts mentioning 'interface' align with the UI axis."""

    dim = 2

    def embed(self, texts):
        return [[1.0, 0.0] if "interface" in t.lower() else [0.0, 1.0] for t in texts]


# ---- ranking ----

def test_relevant_specialist_ranked_above_irrelevant_with_stub_embedder():
    agents = _roster()
    prompt = "Build the user interface for the dashboard"
    scores = score_agents(prompt, agents, _settings(), embedder=_StubEmbedder())
    assert scores["frontend"] > scores["performance"]

    selected = select_roster(prompt, agents, _settings(), embedder=_StubEmbedder())
    assert "frontend" in selected            # relevant specialist won a ranked slot
    assert "performance" not in selected     # irrelevant one was dropped
    assert len(selected) == 10               # capped at roster_max


def test_lexical_fallback_on_hash_backend_still_ranks():
    # No injected embedder + hash backend -> lexical-only leg (repo convention).
    agents = _roster()
    prompt = "Design the database schema migrations and SQL queries"
    scores = score_agents(prompt, agents, _settings())
    assert scores["database"] > scores["frontend"]
    selected = select_roster(prompt, agents, _settings())
    assert "database" in selected


def test_core_roles_always_kept_even_when_irrelevant():
    agents = _roster()
    selected = select_roster("polish the css animations everywhere", agents, _settings())
    assert set(CORE_ROSTER) <= set(selected)


def test_custom_agents_always_kept():
    # Any name outside builtin_agent_names() is a custom -> always listed.
    agents = _roster(extra={"acme_stylist": "Applies the Acme corporate style guide."})
    selected = select_roster("Design the database schema migrations", agents, _settings())
    assert "acme_stylist" in selected
    assert len(selected) == 10  # customs consume roster slots but are never dropped


def test_roster_max_zero_disables_filtering():
    agents = _roster()
    selected = select_roster("anything at all", agents, _settings(roster_max=0))
    assert list(selected) == list(agents)


def test_selection_is_deterministic_and_order_stable():
    agents = _roster()
    prompt = "Build the user interface for the dashboard"
    runs = [select_roster(prompt, agents, _settings(), embedder=_StubEmbedder())
            for _ in range(3)]
    assert all(list(r) == list(runs[0]) for r in runs)
    # selected agents keep the original roster order (stable catalog formatting)
    order = [n for n in agents if n in runs[0]]
    assert list(runs[0]) == order


# ---- planning catalog: track-record notes + per-project demotion ----

def _catalog(prompt: str, agents, *, stats=None, recent=None) -> str:
    orch = Orchestrator(_settings(), provider=None)
    return orch._planning_catalog(prompt, agents, agent_stats=stats,
                                  project_recent=recent)


def _line_for(catalog: str, name: str) -> str:
    return next(line for line in catalog.splitlines() if line.startswith(f"- {name}:"))


def test_catalog_record_note_only_at_n_ge_5():
    agents = _roster()
    stats = {
        "coder": {"n": 9, "passed": 8, "avg_score": 88.0},
        "researcher": {"n": 4, "passed": 4, "avg_score": 97.0},  # below threshold
    }
    catalog = _catalog("Build the user interface", agents, stats=stats)
    assert _line_for(catalog, "coder").endswith("(recent: 8/9 passed)")
    assert "recent:" not in _line_for(catalog, "researcher")


def test_demotion_after_three_project_failures_excludes_ranked_role():
    agents = _roster()
    recent = {"frontend": [False, False, False],   # 3 straight failures here -> demoted
              "database": [False, False, True]}    # streak broken -> not demoted
    assert demoted_roles(recent) == frozenset({"frontend"})
    # frontend would top the ranking for this prompt, but demotion excludes it
    catalog = _catalog("Build the user interface pages markup styling components",
                       agents, recent=recent)
    assert "- frontend:" not in catalog
    assert "- database:" in catalog


def test_demoted_core_role_gets_warning_note_but_stays_listed():
    agents = _roster()
    recent = {"coder": [False, False, False]}
    catalog = _catalog("Build the user interface", agents, recent=recent)
    line = _line_for(catalog, "coder")   # never excluded
    assert "warning:" in line and "failed review" in line
    # and the warning is per-role, not sprayed across the catalog
    assert "warning:" not in _line_for(catalog, "researcher")
