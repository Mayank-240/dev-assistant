"""The orchestrator ("boss"): decomposes a task into a routed DAG of subtasks.

It makes one structured-output call to produce a Plan, then sanitizes it (valid agent
names, valid dependency ids) before the scheduler runs it.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..llm.provider import LLMProvider
from ..llm.schemas import Plan
from .base import BaseAgent
from .registry import capability_catalog

logger = logging.getLogger("ada.orchestrator")

_SYSTEM = (
    "You are the Orchestrator of a team of specialized AI agents. Given a task, you break it "
    "into a small number of concrete subtasks (aim for 2-5) and assign each to the single "
    "best-suited agent from the roster. Design for parallelism: subtasks that don't depend on "
    "each other must NOT list each other in depends_on, so they can run at the same time. "
    "BUT subtasks share one workspace and each is reviewed the moment it finishes, so any subtask "
    "that builds on another's output MUST depend on it: a subtask that writes tests for code must "
    "depend on the subtask that writes that code; a documentation subtask must depend on every "
    "subtask it documents. Never schedule a test or documentation subtask in parallel with the "
    "work it covers. Give "
    "every subtask explicit, checkable acceptance_criteria. Reference subtasks by short ids like "
    "'s1', 's2'. When the task implies a deliverable that should be written up, add a final "
    "documentation subtask (agent 'documenter') that depends on the subtasks it describes. "
    "When the task is to build a new product/app/feature from a high-level or vague idea, start "
    "with a 'product_manager' subtask that decides the concrete feature set and scope, and have "
    "the design/build subtasks depend on it. "
    "Also give the plan a short, human-friendly 'title' (3-7 words, Title Case) that names "
    "the overall deliverable. "
    "All work happens in a dedicated per-task workspace directory: refer to files by RELATIVE "
    "path only (e.g. `gcd.py`, `tests/test_gcd.py`) — NEVER absolute paths and never reference "
    "the host's project directories."
)


class Orchestrator:
    def __init__(self, settings: Settings, provider: LLMProvider) -> None:
        self._settings = settings
        self._provider = provider

    async def make_plan(
        self, prompt: str, agents: dict[str, BaseAgent], prior_knowledge: str = "",
        repo_context: str = "", track_record: str = "",
    ) -> Plan:
        catalog = capability_catalog(agents)
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
