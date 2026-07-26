"""The orchestrator ("boss"): decomposes a task into a routed DAG of subtasks.

It makes one structured-output call to produce a Plan, then sanitizes it (valid agent
names, valid dependency ids) before the scheduler runs it.

Roster pre-filtering: the PLANNING catalog no longer lists every agent. Each agent is
scored by similarity between the task text and its description+when_to_use (embedding
cosine blended with lexical token overlap, degrading to lexical-only on the hash
backend or any embedding failure — the repo-wide convention), and the catalog keeps a
core set + every custom agent + the top-ranked others up to ``settings.roster_max``
(0 = no filtering). Filtering affects only the catalog TEXT the planner reads: agents
stay fully constructed and routable if a plan names them, and the scheduler is
untouched. Selection is deterministic for identical inputs.
"""

from __future__ import annotations

import logging
import math

from ..config import Settings
from ..llm.provider import LLMProvider
from ..llm.schemas import Plan
from ..memory.embeddings import _tokenize, active_embedder_name, get_embedder
from .base import BaseAgent
from .registry import builtin_agent_names, capability_catalog

logger = logging.getLogger("ada.orchestrator")

# Roles the planning catalog always lists (when present in the roster), no matter how
# the task-similarity ranking scores them — the dev-lifecycle spine planning leans on.
CORE_ROSTER = ("coder", "researcher", "test_engineer", "debugger", "documenter")

# Blend weights: semantic cosine dominates, lexical overlap breaks vocabulary misses.
_SEMANTIC_WEIGHT = 0.6
_LEXICAL_WEIGHT = 0.4

# A role is demoted (per project) after this many consecutive review failures there.
_DEMOTION_STREAK = 3


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _agent_text(agent: BaseAgent) -> str:
    p = agent.profile
    return f"{p.description} {p.when_to_use}"


def score_agents(prompt: str, agents: dict[str, BaseAgent], settings: Settings, *,
                 embedder=None) -> dict[str, float]:
    """Task-relevance score per agent: embedding cosine blended with lexical overlap.

    The query is embedded ONCE (a single batched ``embed`` call covers query + all
    agent texts). On the hash backend / hash fallback / any embedding failure the
    semantic leg is dropped and the lexical overlap alone ranks (the same degradation
    convention as memory/KB/code-index retrieval). Deterministic for identical inputs.
    ``embedder`` is an injection point for tests; None uses the process-wide one.
    """
    texts = {name: _agent_text(agent) for name, agent in agents.items()}
    q_tokens = set(_tokenize(prompt))
    lex: dict[str, float] = {}
    for name, text in texts.items():
        toks = set(_tokenize(text))
        lex[name] = (len(q_tokens & toks) / len(q_tokens)) if q_tokens and toks else 0.0
    emb = embedder
    if emb is None:
        try:
            emb = get_embedder(settings)
            if active_embedder_name().startswith("hash"):
                emb = None  # hash backend/fallback: lexical-only quality by convention
        except Exception:  # noqa: BLE001 — ranking must never break planning
            emb = None
    if emb is None:
        return lex
    try:
        names = list(texts)
        vecs = emb.embed([prompt] + [texts[n] for n in names])
        qv, avs = vecs[0], vecs[1:]
        sem = {n: max(0.0, _cosine(qv, av)) for n, av in zip(names, avs)}
    except Exception:  # noqa: BLE001 — embedder down mid-flight: lexical leg alone
        logger.debug("roster embedding failed; lexical-only ranking", exc_info=True)
        return lex
    return {n: _SEMANTIC_WEIGHT * sem[n] + _LEXICAL_WEIGHT * lex[n] for n in texts}


def select_roster(prompt: str, agents: dict[str, BaseAgent], settings: Settings, *,
                  demoted: frozenset[str] | set[str] = frozenset(),
                  embedder=None) -> dict[str, BaseAgent]:
    """The subset of ``agents`` the planning catalog lists (original roster order).

    Always kept: the core set (CORE_ROSTER) and every custom agent (any name not in
    the built-in registry). Remaining slots up to ``settings.roster_max`` go to the
    top-scoring other agents (ties broken by name — deterministic). ``roster_max=0``
    — or a roster that already fits — disables filtering entirely. ``demoted`` roles
    are excluded from the ranked slots (always-kept roles are never excluded here;
    the catalog builder appends a warning note to them instead).
    """
    limit = int(getattr(settings, "roster_max", 10) or 0)
    if limit <= 0 or len(agents) <= limit:
        return dict(agents)
    builtins = builtin_agent_names()
    always = {n for n in agents if n in CORE_ROSTER or n not in builtins}
    scores = score_agents(prompt, agents, settings, embedder=embedder)
    candidates = [n for n in agents if n not in always and n not in demoted]
    ranked = sorted(candidates, key=lambda n: (-scores.get(n, 0.0), n))
    keep = always | set(ranked[:max(0, limit - len(always))])
    return {n: a for n, a in agents.items() if n in keep}


def demoted_roles(project_recent: dict[str, list[bool]] | None) -> frozenset[str]:
    """Roles whose last ``_DEMOTION_STREAK`` outcomes in this project all failed."""
    recent = project_recent or {}
    return frozenset(
        role for role, seq in recent.items()
        if len(seq) >= _DEMOTION_STREAK and not any(seq[:_DEMOTION_STREAK])
    )

_SYSTEM = (
    "You are the Orchestrator of a team of specialized AI agents. Given a task, you break it "
    "into concrete subtasks and assign each to the single best-suited agent from the roster. "
    "MAXIMIZE PARALLELISM: split independent pieces of work into SEPARATE subtasks so they run "
    "concurrently, instead of bundling everything into one large subtask. In particular, "
    "separate concerns by the agent that fits them best — e.g. core/business logic and "
    "algorithms go to 'coder' (NOT 'frontend'), user interface and styling to 'frontend', "
    "schema/queries to 'database', external integrations to 'integrator' — and split distinct "
    "modules, components, or endpoints into their own subtasks. Aim for the most independent "
    "subtasks the task reasonably supports (typically 3-7) without inventing busywork. Subtasks "
    "that don't depend on each other must NOT list each other in depends_on, so they run at the "
    "same time. "
    "Use depends_on ONLY for genuine ordering/data needs (subtasks share one workspace and each "
    "is reviewed the moment it finishes, so a subtask that builds on another's output MUST "
    "depend on it). A test subtask must depend on the code it tests. A documentation subtask "
    "must depend on the IMPLEMENTATION subtasks it describes, but must NOT depend on the test "
    "subtask — so tests and documentation run IN PARALLEL once the implementation is done (only "
    "make docs depend on tests if the documentation's explicit purpose is to document the test "
    "suite). Give "
    "every subtask explicit, checkable acceptance_criteria. Reference subtasks by short ids like "
    "'s1', 's2'. When the task implies a deliverable that should be written up, add a final "
    "documentation subtask (agent 'documenter') that depends on the implementation it describes. "
    "When the task is to build a new product/app/feature from a high-level or vague idea, start "
    "with a 'product_manager' subtask that decides the concrete feature set and scope, and have "
    "the design/build subtasks depend on it. "
    "Also give the plan a short, human-friendly 'title' (3-7 words, Title Case) that names "
    "the overall deliverable. "
    "All work happens in a dedicated per-task workspace directory: refer to files by RELATIVE "
    "path only (e.g. `gcd.py`, `tests/test_gcd.py`) — NEVER absolute paths and never reference "
    "the host's project directories."
)


_REFINE_SYSTEM = (
    "You are refining an EXISTING plan for a team of specialized AI agents, based on the "
    "user's instruction. Apply the requested change and return the COMPLETE revised plan "
    "(not a diff). Keep everything the user did not ask to change. Preserve subtask ids that "
    "still apply so dependencies stay valid; only add, remove, reorder, re-route, re-scope, "
    "or re-depend subtasks as the instruction implies. Keep the same rules as normal planning: "
    "favor independent subtasks that can run in parallel (split logic/UI/components by best-fit "
    "agent), explicit checkable acceptance_criteria on each, and correct depends_on (tests "
    "depend on the code they test; documentation depends on the implementation, NOT the test "
    "subtask, so docs and tests run in parallel). Use valid agent names from the roster and "
    "refer to files by RELATIVE path only."
)


class Orchestrator:
    def __init__(self, settings: Settings, provider: LLMProvider) -> None:
        self._settings = settings
        self._provider = provider

    def _planning_catalog(
        self, prompt: str, agents: dict[str, BaseAgent], *,
        agent_stats: dict[str, dict] | None = None,
        project_recent: dict[str, list[bool]] | None = None,
    ) -> str:
        """The pre-filtered, track-record-annotated catalog for the PLANNING prompt.

        Per listed agent, one line in the registry's format, plus:
          - " (recent: <passed>/<n> passed)" when its aggregate outcome count n >= 5;
          - a warning note when the role's last 3 outcomes in this project all failed
            but the role is always-kept (core/custom) — ranked roles in that state are
            excluded from the ranked slots by ``select_roster`` instead.
        Formatting is stable and deterministic for identical inputs.
        """
        stats = agent_stats or {}
        demoted = demoted_roles(project_recent)
        selected = select_roster(prompt, agents, self._settings, demoted=demoted)
        lines: list[str] = []
        for name, agent in selected.items():
            line = capability_catalog({name: agent})
            rec = stats.get(name) or {}
            n = int(rec.get("n") or 0)
            if n >= 5:
                line += f" (recent: {int(rec.get('passed') or 0)}/{n} passed)"
            if name in demoted:
                line += (" (warning: this role's last 3 subtasks in this project "
                         "failed review — prefer another agent where reasonable)")
            lines.append(line)
        return "\n".join(lines)

    async def make_plan(
        self, prompt: str, agents: dict[str, BaseAgent], prior_knowledge: str = "",
        repo_context: str = "", track_record: str = "",
        agent_stats: dict[str, dict] | None = None,
        project_recent: dict[str, list[bool]] | None = None,
    ) -> Plan:
        catalog = self._planning_catalog(prompt, agents, agent_stats=agent_stats,
                                         project_recent=project_recent)
        if track_record:
            catalog += f"\n\nAGENT TRACK RECORD (empirical pass rates from past runs):\n{track_record}"
        prior = (
            f"RELEVANT PRIOR KNOWLEDGE (lessons from past runs — use if helpful):\n{prior_knowledge}\n\n"
            if prior_knowledge else ""
        )
        repo = (
            f"EXISTING REPOSITORY (you are modifying this real codebase — reference real files):\n"
            f"{repo_context}\n\n" if repo_context else ""
        )
        user = (
            f"TASK:\n{prompt}\n\n"
            f"{repo}"
            f"{prior}"
            f"AGENT ROSTER (choose 'agent' for each subtask from these names only):\n{catalog}\n\n"
            "Produce the plan."
        )
        plan = await self._provider.structured(
            system=_SYSTEM,
            user=user,
            schema=Plan,
            model=self._settings.orchestrator_model,
            effort=self._settings.orchestrator_effort,
            max_tokens=4000,
        )
        return self._sanitize(plan, agents)

    async def refine_plan(
        self, prompt: str, plan: Plan, instruction: str, agents: dict[str, BaseAgent],
    ) -> Plan:
        """Revise an existing plan per a natural-language instruction (interactive plan mode)."""
        catalog = capability_catalog(agents)
        user = (
            f"ORIGINAL TASK:\n{prompt}\n\n"
            f"CURRENT PLAN:\n{self._plan_text(plan)}\n\n"
            f"USER INSTRUCTION (apply this to the plan):\n{instruction}\n\n"
            f"AGENT ROSTER (choose 'agent' for each subtask from these names only):\n{catalog}\n\n"
            "Return the complete revised plan."
        )
        revised = await self._provider.structured(
            system=_REFINE_SYSTEM, user=user, schema=Plan,
            model=self._settings.orchestrator_model, effort=self._settings.orchestrator_effort,
            max_tokens=4000,
        )
        return self._sanitize(revised, agents)

    @staticmethod
    def _plan_text(plan: Plan) -> str:
        lines = [f"Title: {getattr(plan, 'title', '') or '(none)'}", f"Summary: {plan.summary}", "Subtasks:"]
        for st in plan.subtasks:
            deps = ", ".join(st.depends_on) or "—"
            lines.append(f"- [{st.id}] ({st.agent}) {st.title} | depends_on: {deps}")
            lines.append(f"    description: {st.description}")
            for c in st.acceptance_criteria:
                lines.append(f"    criterion: {c}")
        return "\n".join(lines)

    # Default criteria backfilled when the orchestrator emits a subtask with none — an
    # ungrounded reviewer is what makes verdicts (and the resulting cascades) meaningless.
    _DEFAULT_CRITERIA = [
        "The subtask's stated deliverable is actually produced.",
        "The result is correct, complete, and self-contained.",
    ]

    @classmethod
    def _sanitize(cls, plan: Plan, agents: dict[str, BaseAgent]) -> Plan:
        default_agent = "researcher" if "researcher" in agents else next(iter(agents))
        ids = {st.id for st in plan.subtasks}
        for st in plan.subtasks:
            if st.agent not in agents:
                logger.warning("subtask %s routed to unknown agent '%s' -> '%s'", st.id, st.agent, default_agent)
                st.agent = default_agent
            # drop dangling / self dependencies
            st.depends_on = [d for d in st.depends_on if d in ids and d != st.id]
            # semantic backfill: never leave the reviewer with no criteria to check
            if not [c for c in st.acceptance_criteria if c.strip()]:
                st.acceptance_criteria = list(cls._DEFAULT_CRITERIA)
        return plan
