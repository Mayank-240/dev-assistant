"""Runtime task model: the plan plus mutable per-subtask execution state."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from ..execution import ExecutionResult
from ..llm.schemas import Plan, SubTask, Verdict


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    PASSED_WITH_CAVEATS = "passed_with_caveats"  # produced real output but didn't fully pass review
    FAILED = "failed"
    BLOCKED = "blocked"  # a dependency failed, so this can never run


# Statuses that satisfy a dependency (a dependent may proceed) and that are terminal.
SATISFYING = (RunStatus.PASSED, RunStatus.PASSED_WITH_CAVEATS)
TERMINAL = (RunStatus.PASSED, RunStatus.PASSED_WITH_CAVEATS, RunStatus.FAILED, RunStatus.BLOCKED)


class DAGError(ValueError):
    """A plan whose subtask graph is structurally invalid (cycle / dup id / dangling dep)."""


def new_task_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


@dataclass
class SubTaskState:
    spec: SubTask
    status: RunStatus = RunStatus.PENDING
    attempts: int = 0
    result: str = ""
    verdict: Verdict | None = None
    error: str = ""
    started_at: float | None = None
    ended_at: float | None = None

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def agent(self) -> str:
        return self.spec.agent


@dataclass
class TaskRun:
    id: str
    prompt: str
    plan: Plan
    subtasks: dict[str, SubTaskState] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    execution: ExecutionResult | None = None  # objective test run for the whole task

    @classmethod
    def from_plan(cls, prompt: str, plan: Plan, task_id: str | None = None) -> "TaskRun":
        run = cls(id=task_id or new_task_id(), prompt=prompt, plan=plan)
        run.subtasks = {st.id: SubTaskState(spec=st) for st in plan.subtasks}
        return run

    def ready(self) -> list[SubTaskState]:
        """Pending subtasks whose dependencies have all completed satisfyingly.

        A dependency counts as satisfied if it PASSED or PASSED_WITH_CAVEATS — a
        soft-success no longer blocks the subtasks that build on it.
        """
        out: list[SubTaskState] = []
        for state in self.subtasks.values():
            if state.status is not RunStatus.PENDING:
                continue
            deps = state.spec.depends_on
            if all(self.subtasks.get(d) and self.subtasks[d].status in SATISFYING for d in deps):
                out.append(state)
        return out

    def block_unreachable(self) -> None:
        """Mark any still-pending subtask whose deps truly failed/blocked as BLOCKED.

        A PASSED_WITH_CAVEATS dependency does NOT block dependents — only FAILED or
        BLOCKED (or a missing dep) does.
        """
        for state in self.subtasks.values():
            if state.status is not RunStatus.PENDING:
                continue
            for dep in state.spec.depends_on:
                dep_state = self.subtasks.get(dep)
                if dep_state is None or dep_state.status in (RunStatus.FAILED, RunStatus.BLOCKED):
                    state.status = RunStatus.BLOCKED
                    state.error = f"dependency '{dep}' did not pass"
                    break

    def dependency_results(self, state: SubTaskState) -> dict[str, str]:
        out: dict[str, str] = {}
        for d in state.spec.depends_on:
            dep = self.subtasks.get(d)
            if not (dep and dep.result):
                continue
            text = dep.result
            if dep.status is RunStatus.PASSED_WITH_CAVEATS:
                text = ("[caveat: this dependency produced output but did NOT fully pass review; "
                        "verify it before relying on it]\n" + text)
            out[d] = text
        return out

    def validate(self) -> None:
        """Structural DAG validation: duplicate ids, dangling deps, and cycles.

        Raises DAGError. Call before scheduling so a malformed plan fails loudly instead
        of masquerading as a runtime 'dependency did not pass'.
        """
        ids = [st.id for st in self.plan.subtasks]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise DAGError(f"duplicate subtask id(s): {', '.join(sorted(dupes))}")
        idset = set(ids)
        for st in self.plan.subtasks:
            dangling = [d for d in st.depends_on if d not in idset]
            if dangling:
                raise DAGError(f"subtask '{st.id}' depends on unknown id(s): {', '.join(dangling)}")
        # Kahn topological sort to detect cycles.
        indeg = {i: 0 for i in ids}
        adj: dict[str, list[str]] = {i: [] for i in ids}
        for st in self.plan.subtasks:
            for d in st.depends_on:
                adj[d].append(st.id)
                indeg[st.id] += 1
        queue = [i for i in ids if indeg[i] == 0]
        seen = 0
        while queue:
            n = queue.pop()
            seen += 1
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        if seen != len(ids):
            cyclic = [i for i in ids if indeg[i] > 0]
            raise DAGError(f"dependency cycle among subtask(s): {', '.join(cyclic)}")

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.subtasks.values():
            counts[s.status.value] = counts.get(s.status.value, 0) + 1
        return counts

    def rollup_status(self) -> str:
        """Honest terminal status for the whole run, derived from the subtasks.

        completed = every subtask passed; partial = some passed (incl. caveats) but not
        all; failed = nothing passed.
        """
        total = len(self.subtasks)
        if total == 0:
            return "failed"
        passed = sum(1 for s in self.subtasks.values() if s.status is RunStatus.PASSED)
        satisfied = sum(1 for s in self.subtasks.values() if s.status in SATISFYING)
        if passed == total:
            return "completed"
        if satisfied == 0:
            return "failed"
        return "partial"

    @property
    def all_passed(self) -> bool:
        return all(s.status is RunStatus.PASSED for s in self.subtasks.values())
