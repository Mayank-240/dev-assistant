"""Golden-task eval harness (Tier 5).

Runs the real Engine on each golden task into a fresh temp workspace, then grades the
produced workspace with deterministic graders and emits a Scorecard (pass/fail, cost,
wall-time, subtasks passed). Driven from the CLI via ``ada eval``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..engine import Engine
from .graders import GraderResult, ast_defines, file_exists, tests_pass

Grader = Callable[[Path], GraderResult]


@dataclass
class GoldenTask:
    id: str
    prompt: str
    graders: list[Grader]


# A small starter suite. Extend with SWE-bench-style tasks over real repos.
GOLDEN: list[GoldenTask] = [
    GoldenTask(
        id="reverse_string",
        prompt="Implement a Python function reverse_string(s) in reverse_string.py with a pytest test file.",
        graders=[lambda ws: file_exists(ws, "reverse_string.py"),
                 lambda ws: ast_defines(ws, "reverse_string"),
                 lambda ws: tests_pass(ws)],
    ),
    GoldenTask(
        id="is_prime",
        prompt="Implement is_prime(n) in is_prime.py using trial division, plus a pytest suite.",
        graders=[lambda ws: ast_defines(ws, "is_prime"),
                 lambda ws: tests_pass(ws)],
    ),
]


@dataclass
class Scorecard:
    task_id: str
    passed: bool
    graders: list[GraderResult]
    cost_usd: float = 0.0
    wall_s: float = 0.0
    subtasks_passed: int = 0
    subtasks_total: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {**dataclasses.asdict(self),
                "graders": [dataclasses.asdict(g) for g in self.graders]}


@dataclass
class EvalReport:
    cards: list[Scorecard] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cards if c.passed)

    def summary(self) -> str:
        lines = [f"Eval: {self.passed}/{len(self.cards)} golden tasks passed", ""]
        for c in self.cards:
            mark = "✅" if c.passed else "❌"
            lines.append(f"{mark} {c.task_id}  (${c.cost_usd:.3f}, {c.wall_s:.1f}s, "
                         f"{c.subtasks_passed}/{c.subtasks_total} subtasks)")
            for g in c.graders:
                lines.append(f"     {'·' if g.passed else '✗'} {g.name}: {g.detail}")
            if c.error:
                lines.append(f"     error: {c.error}")
        return "\n".join(lines)


async def _run_one(base: Settings, task: GoldenTask) -> Scorecard:
    tmp = Path(tempfile.mkdtemp(prefix=f"ada-eval-{task.id}-"))
    settings = dataclasses.replace(
        base, data_dir=tmp / "data", docs_dir=tmp / "docs", workspace_dir=tmp / "ws",
        git_finalize=False)
    engine = Engine(settings)
    start = time.monotonic()
    card = Scorecard(task_id=task.id, passed=False, graders=[])
    try:
        run, _brief, _out = await engine.run(task.prompt)
        ws = settings.run_workspace(run.id)
        card.graders = [g(ws) for g in task.graders]
        card.passed = all(g.passed for g in card.graders)
        card.subtasks_total = len(run.subtasks)
        from ..orchestration.task import RunStatus
        card.subtasks_passed = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
        usage = getattr(engine.provider, "usage", None)
        card.cost_usd = float(usage.to_dict().get("cost_usd", 0.0)) if usage else 0.0
    except Exception as exc:  # noqa: BLE001
        card.error = str(exc)
    finally:
        card.wall_s = time.monotonic() - start
        await engine.aclose()
    return card


async def run_eval(base: Settings, only: list[str] | None = None) -> EvalReport:
    tasks = [t for t in GOLDEN if not only or t.id in only]
    report = EvalReport()
    for t in tasks:  # serial: keeps cost/observability simple and deterministic
        report.cards.append(await _run_one(base, t))
    return report


def run_eval_sync(base: Settings, only: list[str] | None = None) -> EvalReport:
    return asyncio.run(run_eval(base, only))
