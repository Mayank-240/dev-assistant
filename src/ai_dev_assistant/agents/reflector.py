"""Outcome-aware post-mortem (Tier 4 learning loop).

The old ``_consolidate_longterm`` wrote the same happy-path lessons for a 0/3 failure as a
3/3 success, so recall fed the planner "success" summaries even when the run failed. The
Reflector makes one structured call over the *actual* subtask outcomes, verdict reasons,
and test result, producing typed lessons tagged with the outcome so future planning learns
what to repeat and what to avoid.
"""

from __future__ import annotations

from ..config import Settings
from ..llm.provider import LLMProvider
from ..llm.schemas import RunLessons
from ..orchestration.task import RunStatus, TaskRun

_SYSTEM = (
    "You are the Reflector for a multi-agent dev system. Given a finished run — its task, "
    "each subtask's agent and outcome, the reviewer's reasons, and the objective test result "
    "— distill concise, outcome-aware lessons that would make the NEXT similar run better. Be "
    "honest about failures: if the run failed, say what went wrong and what to avoid. Keep each "
    "lesson a single concrete sentence."
)


class Reflector:
    def __init__(self, settings: Settings, provider: LLMProvider) -> None:
        self._settings = settings
        self._provider = provider

    async def reflect(self, run: TaskRun) -> RunLessons:
        passed = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
        total = len(run.subtasks)
        lines = [f"TASK: {run.prompt}", f"OUTCOME: {passed}/{total} subtasks passed "
                 f"(rollup: {run.rollup_status()}).", "", "SUBTASKS:"]
        for st in run.subtasks.values():
            lines.append(f"- {st.id} [{st.agent}] {st.spec.title}: {st.status.value}")
            if st.verdict and st.verdict.reasons:
                lines.append(f"    review: {'; '.join(st.verdict.reasons[:3])}")
        if run.execution is not None:
            lines.append(f"\nTESTS: `{run.execution.command}` "
                         f"{'PASSED' if run.execution.passed else 'FAILED'} (exit {run.execution.return_code}).")
        user = "\n".join(lines) + "\n\nProduce the lessons."
        return await self._provider.structured(
            system=_SYSTEM, user=user, schema=RunLessons,
            model=self._settings.orchestrator_model, effort=self._settings.agent_effort,
            max_tokens=1500,
        )
