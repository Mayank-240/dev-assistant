"""Golden-task eval harness (Tier 5).

Runs the real Engine on each golden task into a fresh temp workspace, then grades the
produced workspace with deterministic graders and emits a Scorecard (pass/fail, cost,
wall-time, subtasks passed). Driven from the CLI via ``ada eval``.

What the harness measures beyond the toy tasks:
  - E2  repo-level golden tasks: fixture repos under ``evals/fixtures/<task>/repo`` are
    bound via ``Settings.repo_path`` so the flagship materialize → edit → verify path is
    graded end to end.
  - E3  held-out grading: ``evals/fixtures/<task>/heldout`` tests live OUTSIDE the
    fixture repo; the agent never sees them. After the run they are copied into the
    finished workspace and run alone (``heldout_tests_pass``).
  - E5  variance + metrics: ``repeat`` (env ``ADA_EVAL_REPEAT``) samples each task N
    times; per-task aggregates report pass_rate, mean/min quality, mean cost, wall time,
    subtask parallelism achieved (from run events), and routing accuracy against
    ``expected_agents``. Each attempt is bounded by a wall-clock timeout
    (``ADA_EVAL_TASK_TIMEOUT``, default 600s).
  - E4  offline replay: see ``evals/replay_eval.py`` — the same harness run over
    committed cassettes in ``evals/cassettes/`` with a ReplayProvider.
  - Cross-language fixtures (JS/npm, Go, Rust, Ruby) exercise non-pytest verification;
    each declares ``required_tools`` and is SKIPPED (``Scorecard.skipped``) rather than
    failed when its toolchain is absent, and skips never count against the pass rate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..engine import Engine
from ..orchestration.events import Event
from .graders import GraderResult, ast_defines, file_exists, heldout_tests_pass, tests_pass

Grader = Callable[[Path], GraderResult]
# Builds the provider each attempt runs against (record/replay/scripted); None = the
# provider the Engine constructs from settings (the real backend, or ReplayProvider
# when settings.replay_dir is set — see llm/factory.get_provider).
ProviderFactory = Callable[[], Any]

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CASSETTES_DIR = Path(__file__).resolve().parent / "cassettes"

_DEFAULT_TASK_TIMEOUT = 600.0


def _env_repeat() -> int:
    try:
        return max(1, int(os.getenv("ADA_EVAL_REPEAT", "") or 1))
    except ValueError:
        return 1


def _env_task_timeout() -> float:
    try:
        return float(os.getenv("ADA_EVAL_TASK_TIMEOUT", "") or _DEFAULT_TASK_TIMEOUT)
    except ValueError:
        return _DEFAULT_TASK_TIMEOUT


@dataclass
class GoldenTask:
    id: str
    prompt: str
    graders: list[Grader]
    # E2: fixture dir name under evals/fixtures/<name>/ — its repo/ subdir is bound as
    # settings.repo_path so the run materializes and edits a real checkout.
    repo_fixture: str = ""
    # E5: agents the plan is expected to route to; grades routing accuracy when set.
    expected_agents: tuple[str, ...] = ()
    # Executables the task's toolchain needs (e.g. ("go",), ("node", "npm")). When any
    # is missing from PATH the task is SKIPPED (Scorecard.skipped) instead of failed.
    required_tools: tuple[str, ...] = ()

    @property
    def fixture_repo(self) -> Path | None:
        return FIXTURES_DIR / self.repo_fixture / "repo" if self.repo_fixture else None

    @property
    def heldout_dir(self) -> Path | None:
        """E3: held-out tests live OUTSIDE the fixture repo, so the agent never sees them."""
        if not self.repo_fixture:
            return None
        d = FIXTURES_DIR / self.repo_fixture / "heldout"
        return d if d.is_dir() else None


# A small starter suite: two greenfield toys plus repo-level tasks over pinned fixture
# repos (E2). Extend with SWE-bench-style tasks over larger real repos.
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
    # ---- Repo-level golden tasks (E2): materialize → edit → verify ----
    GoldenTask(
        id="fix_calculator_subtract",
        prompt=("The calculator module has a bug: subtract(a, b) returns the wrong value, so "
                "test_calculator.py currently fails. Fix the bug in calculator.py so the "
                "existing pytest suite passes. Do NOT change or delete the tests."),
        graders=[lambda ws: file_exists(ws, "calculator.py"),
                 lambda ws: tests_pass(ws)],
        repo_fixture="bugfix_calculator",
        expected_agents=("debugger", "coder"),
    ),
    GoldenTask(
        id="add_slugify",
        prompt=("Add a function slugify(text) to textutils/core.py and export it from the "
                "textutils package (__init__.py). It must lowercase the text, replace every "
                "run of non-alphanumeric characters with a single hyphen, and strip leading/"
                "trailing hyphens (empty or symbol-only input returns an empty string). Add "
                "pytest tests for it and document it in README.md's Functions section."),
        graders=[lambda ws: ast_defines(ws, "slugify"),
                 lambda ws: file_exists(ws, "README.md"),
                 lambda ws: tests_pass(ws)],
        repo_fixture="add_slugify",
        expected_agents=("coder", "test_engineer", "documenter"),
    ),
    GoldenTask(
        id="refactor_shapes_common",
        prompt=("shapes/circle.py and shapes/rectangle.py duplicate the same private "
                "_validate_positive helper. Refactor across the files: create shapes/common.py "
                "exposing validate_positive(name, value) (raises ValueError naming the "
                "offending parameter when value <= 0), make both modules use it, and delete "
                "the duplicated private helpers. Public behavior (area, perimeter, describe) "
                "must not change and the existing tests must keep passing."),
        graders=[lambda ws: file_exists(ws, "common.py"),
                 lambda ws: ast_defines(ws, "validate_positive"),
                 lambda ws: tests_pass(ws)],
        repo_fixture="refactor_shapes",
        expected_agents=("refactorer", "coder"),
    ),
    GoldenTask(
        id="fix_pagination_total_pages",
        prompt=("The pagination helper has an off-by-one bug: total_pages(total_items, "
                "page_size) drops the final partial page, so test_pagination.py currently "
                "fails. Fix the bug in pagination.py so the existing pytest suite passes. "
                "Do NOT change or delete the tests."),
        graders=[lambda ws: file_exists(ws, "pagination.py"),
                 lambda ws: tests_pass(ws)],
        repo_fixture="bugfix_pagination",
        expected_agents=("debugger", "coder"),
    ),
    # ---- Cross-language repo tasks: skipped (not failed) when the toolchain is absent.
    # Their held-out grading merges fixtures/<task>/heldout into the workspace and runs
    # the language's own test command (see graders.heldout_tests_pass).
    GoldenTask(
        id="fix_js_clamp",
        prompt=("This is a Node.js package; `npm test` runs the node:test suite. "
                "clamp(value, min, max) in clamp.js has a bug: the bounds are swapped, so "
                "out-of-range values are pinned to the WRONG end and clamp.test.js fails. "
                "Fix clamp.js so `npm test` passes. Do NOT change the tests or package.json."),
        graders=[lambda ws: file_exists(ws, "clamp.js"),
                 lambda ws: tests_pass(ws)],
        repo_fixture="bugfix_js_clamp",
        expected_agents=("debugger", "coder"),
        required_tools=("node", "npm"),
    ),
    GoldenTask(
        id="fix_go_leap_year",
        prompt=("This is a Go module. IsLeapYear in leapyear.go ignores the Gregorian "
                "century rules (1900 is NOT a leap year, 2000 is), so `go test` fails. "
                "Fix leapyear.go so the existing tests pass. Do NOT change the tests."),
        graders=[lambda ws: file_exists(ws, "leapyear.go"),
                 lambda ws: tests_pass(ws)],
        repo_fixture="bugfix_go_leapyear",
        expected_agents=("debugger", "coder"),
        required_tools=("go",),
    ),
    GoldenTask(
        id="fix_rust_temp_conversion",
        prompt=("This is a Rust crate. fahrenheit_to_celsius in src/lib.rs uses an "
                "inverted conversion factor (it multiplies by 9/5 instead of 5/9), so "
                "`cargo test` fails. Fix src/lib.rs so the tests pass. Do NOT change the "
                "tests."),
        graders=[lambda ws: file_exists(ws, "lib.rs"),
                 lambda ws: tests_pass(ws)],
        repo_fixture="bugfix_rust_tempconv",
        expected_agents=("debugger", "coder"),
        required_tools=("cargo",),
    ),
    GoldenTask(
        id="fix_ruby_titlecase",
        prompt=("This is a Ruby project tested with `rake test` (minitest). "
                "StrUtils.titlecase in strutils.rb only capitalizes the FIRST word — every "
                "word should be capitalized — so test_strutils.rb fails. Fix strutils.rb "
                "so `rake test` passes. Do NOT change the tests or the Rakefile."),
        graders=[lambda ws: file_exists(ws, "strutils.rb"),
                 lambda ws: tests_pass(ws)],
        repo_fixture="bugfix_ruby_titlecase",
        expected_agents=("debugger", "coder"),
        required_tools=("ruby", "rake"),
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
    quality: float = 0.0          # the engine's 0-100 quality score for the run
    parallelism: int = 0          # max subtasks concurrently in flight (from run events)
    routing_accuracy: float | None = None  # share of subtasks routed to expected_agents
    run_status: str = ""          # engine rollup: completed | partial | failed | ...
    error: str = ""
    # Toolchain gating: True when the task's required_tools are missing on this machine.
    # Skipped cards never count as failures (excluded from the pass/fail denominator).
    # to_dict stays backward compatible — the new key is purely additive.
    skipped: bool = False

    def to_dict(self) -> dict:
        return {**dataclasses.asdict(self),
                "graders": [dataclasses.asdict(g) for g in self.graders]}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


@dataclass
class TaskSummary:
    """Aggregate over the N repeats of one golden task (E5)."""

    task_id: str
    runs: list[Scorecard] = field(default_factory=list)

    @property
    def skipped_runs(self) -> int:
        return sum(1 for c in self.runs if c.skipped)

    @property
    def pass_rate(self) -> float:
        # Skipped runs (missing toolchain) are excluded from the denominator.
        return _mean([1.0 if c.passed else 0.0 for c in self.runs if not c.skipped])

    @property
    def mean_quality(self) -> float:
        return _mean([c.quality for c in self.runs])

    @property
    def min_quality(self) -> float:
        return min((c.quality for c in self.runs), default=0.0)

    @property
    def mean_cost_usd(self) -> float:
        return _mean([c.cost_usd for c in self.runs])

    @property
    def mean_wall_s(self) -> float:
        return _mean([c.wall_s for c in self.runs])

    @property
    def parallelism(self) -> int:
        return max((c.parallelism for c in self.runs), default=0)

    @property
    def routing_accuracy(self) -> float | None:
        vals = [c.routing_accuracy for c in self.runs if c.routing_accuracy is not None]
        return _mean(vals) if vals else None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "repeats": len(self.runs),
            "skipped": self.skipped_runs,
            "pass_rate": round(self.pass_rate, 3),
            "mean_quality": round(self.mean_quality, 1),
            "min_quality": round(self.min_quality, 1),
            "mean_cost_usd": round(self.mean_cost_usd, 4),
            "mean_wall_s": round(self.mean_wall_s, 1),
            "parallelism": self.parallelism,
            "routing_accuracy": (round(self.routing_accuracy, 3)
                                 if self.routing_accuracy is not None else None),
            "runs": [c.to_dict() for c in self.runs],
        }


@dataclass
class EvalReport:
    cards: list[Scorecard] = field(default_factory=list)   # one per attempt (task × repeat)
    tasks: list[TaskSummary] = field(default_factory=list)  # one per task

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.cards if c.skipped)

    @property
    def attempted(self) -> int:
        return len(self.cards) - self.skipped

    @property
    def passed(self) -> int:
        """Count of runs that did not fail: passing runs plus skipped ones.

        Skipped cards (missing toolchain) count here so the callers' green condition
        ``report.passed == len(report.cards)`` still holds when every *attempted* run
        passed — i.e. skips are excluded from the pass/fail denominator.
        """
        return sum(1 for c in self.cards if c.passed or c.skipped)

    def summary(self) -> str:
        passed_runs = sum(1 for c in self.cards if c.passed)
        head = (f"Eval: {passed_runs}/{self.attempted} golden runs passed "
                f"({len(self.tasks)} tasks")
        if self.skipped:
            head += f", {self.skipped} run(s) skipped: toolchain missing"
        lines = [head + ")", ""]
        for t in self.tasks:
            if t.skipped_runs == len(t.runs):
                mark = "⏭️"
            else:
                mark = "✅" if t.pass_rate == 1.0 else ("❌" if t.pass_rate == 0.0 else "⚠️")
            routing = (f", routing {t.routing_accuracy:.0%}"
                       if t.routing_accuracy is not None else "")
            lines.append(
                f"{mark} {t.task_id}  pass_rate {t.pass_rate:.0%} over {len(t.runs)} run(s)  "
                f"(quality mean {t.mean_quality:.0f} / min {t.min_quality:.0f}, "
                f"${t.mean_cost_usd:.3f}, {t.mean_wall_s:.1f}s, "
                f"parallelism {t.parallelism}{routing})")
            for c in t.runs:
                state = "SKIPPED" if c.skipped else ("pass" if c.passed else "FAIL")
                lines.append(f"   run: {state}  "
                             f"{c.subtasks_passed}/{c.subtasks_total} subtasks, "
                             f"status {c.run_status or 'n/a'}")
                for g in c.graders:
                    lines.append(f"     {'·' if g.passed else '✗'} {g.name}: {g.detail}")
                if c.error:
                    lines.append(f"     error: {c.error}")
        return "\n".join(lines)


def _parallelism_from_events(events: list[Event], fallback: int) -> int:
    """Max subtasks concurrently in flight, from subtask_start/subtask_review pairs."""
    active = 0
    peak = 0
    saw = False
    for e in events:
        if e.type == "subtask_start":
            saw = True
            active += 1
            peak = max(peak, active)
        elif e.type == "subtask_review":
            active = max(0, active - 1)
    return peak if saw else fallback


async def _run_one(
    base: Settings,
    task: GoldenTask,
    *,
    task_timeout: float,
    provider_factory: ProviderFactory | None = None,
) -> Scorecard:
    tmp = Path(tempfile.mkdtemp(prefix=f"ada-eval-{task.id}-"))
    overrides: dict[str, Any] = dict(
        data_dir=tmp / "data", docs_dir=tmp / "docs", workspace_dir=tmp / "ws",
        git_finalize=False)
    if task.repo_fixture:  # E2: bind the fixture repo for materialize → edit → verify
        overrides.update(repo_path=str(task.fixture_repo), repo_url="", repo_ref="")
    settings = dataclasses.replace(base, **overrides)
    engine = Engine(settings)
    if provider_factory is not None:
        provider = provider_factory()
        engine.provider = provider
        engine.orchestrator._provider = provider
        engine.reviewer._provider = provider
        engine.reflector._provider = provider
    events: list[Event] = []
    start = time.monotonic()
    card = Scorecard(task_id=task.id, passed=False, graders=[])
    try:
        run, _brief, _out = await asyncio.wait_for(
            engine.run(task.prompt, on_event=events.append),
            timeout=task_timeout if task_timeout > 0 else None)
        ws = settings.run_workspace(run.id)
        card.graders = [g(ws) for g in task.graders]
        if task.heldout_dir is not None:  # E3: grade on tests the agent never saw
            card.graders.append(
                heldout_tests_pass(ws, task.heldout_dir, timeout=settings.verify_timeout))
        card.subtasks_total = len(run.subtasks)
        from ..orchestration.task import RunStatus
        card.subtasks_passed = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
        card.run_status = run.rollup_status()
        # A graderless task (e.g. the replay smoke task) passes on the run's own rollup.
        card.passed = (all(g.passed for g in card.graders) if card.graders
                       else card.run_status == "completed")
        card.parallelism = _parallelism_from_events(events, len(run.subtasks))
        if task.expected_agents:
            agents_used = [s.agent for s in run.subtasks.values()]
            expected = set(task.expected_agents)
            card.routing_accuracy = (
                sum(1 for a in agents_used if a in expected) / len(agents_used)
                if agents_used else 0.0)
        try:
            row = engine.runs.get(run.id) or {}
            card.quality = float(row.get("quality_score") or 0.0)
        except Exception:
            card.quality = 0.0
        usage = getattr(engine.provider, "usage", None)
        card.cost_usd = float(usage.to_dict().get("cost_usd", 0.0)) if usage else 0.0
    except asyncio.TimeoutError:
        card.error = (f"task timed out after {task_timeout:.0f}s "
                      "(raise ADA_EVAL_TASK_TIMEOUT if it legitimately needs longer)")
    except Exception as exc:  # noqa: BLE001
        card.error = str(exc)
    finally:
        card.wall_s = time.monotonic() - start
        await engine.aclose()
    return card


async def run_eval(
    base: Settings,
    only: list[str] | None = None,
    *,
    repeat: int | None = None,
    task_timeout: float | None = None,
    provider_factory: ProviderFactory | None = None,
    tasks: list[GoldenTask] | None = None,
) -> EvalReport:
    """Run the golden suite; each task is sampled ``repeat`` times (E5).

    Defaults come from ADA_EVAL_REPEAT / ADA_EVAL_TASK_TIMEOUT so the CLI and CI can
    tune them without new flags; explicit arguments win over the environment.
    """
    chosen = [t for t in (tasks if tasks is not None else GOLDEN) if not only or t.id in only]
    n = repeat if repeat is not None and repeat >= 1 else _env_repeat()
    timeout = task_timeout if task_timeout is not None else _env_task_timeout()
    report = EvalReport()
    for t in chosen:  # serial: keeps cost/observability simple and deterministic
        summary = TaskSummary(task_id=t.id)
        missing = tuple(tool for tool in t.required_tools if shutil.which(tool) is None)
        if missing:  # toolchain gating: skip (never fail) — one card regardless of repeat
            card = Scorecard(
                task_id=t.id, passed=False, graders=[], skipped=True,
                run_status="skipped",
                error=f"skipped: required tool(s) not installed: {', '.join(missing)}")
            summary.runs.append(card)
            report.cards.append(card)
            report.tasks.append(summary)
            continue
        for _ in range(n):
            card = await _run_one(base, t, task_timeout=timeout,
                                  provider_factory=provider_factory)
            summary.runs.append(card)
            report.cards.append(card)
        report.tasks.append(summary)
    return report


def run_eval_sync(
    base: Settings,
    only: list[str] | None = None,
    *,
    repeat: int | None = None,
    task_timeout: float | None = None,
    provider_factory: ProviderFactory | None = None,
    tasks: list[GoldenTask] | None = None,
) -> EvalReport:
    return asyncio.run(run_eval(base, only, repeat=repeat, task_timeout=task_timeout,
                                provider_factory=provider_factory, tasks=tasks))
