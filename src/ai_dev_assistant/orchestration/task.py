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
    FAILED = "failed"
    BLOCKED = "blocked"  # a dependency failed, so this can never run


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
        """Pending subtasks whose dependencies have all passed."""
        out: list[SubTaskState] = []
        for state in self.subtasks.values():
            if state.status is not RunStatus.PENDING:
                continue
            deps = state.spec.depends_on
            if all(self.subtasks.get(d) and self.subtasks[d].status is RunStatus.PASSED for d in deps):
                out.append(state)
        return out

    def block_unreachable(self) -> None:
        """Mark any still-pending subtask whose deps failed/blocked as BLOCKED."""
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
        return {
            d: self.subtasks[d].result
            for d in state.spec.depends_on
            if d in self.subtasks and self.subtasks[d].result
        }

    @property
    def all_passed(self) -> bool:
        return all(s.status is RunStatus.PASSED for s in self.subtasks.values())
