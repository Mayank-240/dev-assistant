"""Eval harness tests: deterministic graders (no LLM), held-out grading (E3), repo-level
golden tasks driven offline by a scripted provider (E2), repeat/metrics aggregation and
per-task timeouts (E5), and the fully-offline replay eval over committed cassettes (E4)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.evals import replay_eval
from ai_dev_assistant.evals.graders import ast_defines, file_exists, heldout_tests_pass
from ai_dev_assistant.evals.graders import tests_pass as grade_tests  # avoid pytest collecting it
from ai_dev_assistant.evals.harness import (
    FIXTURES_DIR,
    GOLDEN,
    GoldenTask,
    _env_repeat,
    _env_task_timeout,
    run_eval,
)
from ai_dev_assistant.llm.schemas import BriefDoc, Plan, RunLessons, SubTask, Verdict
from ai_dev_assistant.orchestration.run_store import RunStore


def _track_attempt_dirs(monkeypatch, tmp_path):
    """Route the harness's per-attempt mkdtemp under tmp_path and record the dirs, so
    tests can inspect the ephemeral project (registry, run store, fixture repo)."""
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        d = real_mkdtemp(*args, **kwargs)
        made.append(d)
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    return made


def _git_out(args: list[str], cwd: Path) -> str:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_graders(tmp_path):
    (tmp_path / "reverse_string.py").write_text("def reverse_string(s):\n    return s[::-1]\n")
    (tmp_path / "test_reverse_string.py").write_text(
        "from reverse_string import reverse_string\n"
        "def test_basic():\n    assert reverse_string('abc') == 'cba'\n"
    )
    assert file_exists(tmp_path, "reverse_string.py").passed
    assert ast_defines(tmp_path, "reverse_string").passed
    assert not ast_defines(tmp_path, "nonexistent").passed
    assert grade_tests(tmp_path, timeout=60).passed


_FIXED_CALCULATOR = '''\
"""A tiny calculator module (fixed)."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''


def test_heldout_grader_fails_on_buggy_repo_and_passes_on_fix(tmp_path):
    """E3: held-out tests live outside the repo, catch the planted bug, and pass once fixed."""
    fixture = FIXTURES_DIR / "bugfix_calculator"
    ws = tmp_path / "ws"
    shutil.copytree(fixture / "repo", ws)

    res = heldout_tests_pass(ws, fixture / "heldout", timeout=60)
    assert not res.passed  # the planted subtract bug is visible to the held-out tests

    (ws / "calculator.py").write_text(_FIXED_CALCULATOR)
    res = heldout_tests_pass(ws, fixture / "heldout", timeout=60)
    assert res.passed
    assert res.name == "heldout_tests_pass"
    # missing held-out dir is a graded failure, not a crash
    assert not heldout_tests_pass(ws, tmp_path / "nope", timeout=60).passed


def _offline_settings() -> Settings:
    return Settings(
        llm_backend="anthropic",  # never called: the provider is injected/replayed
        anthropic_api_key="",
        embeddings_backend="hash",
        verify_run_tests=False, objective_review=False, lint_check=False,
        max_retries=0,
        session_idle_ttl=0.05, reaper_interval=0.02,
    )


class _RepoFixProvider:
    """Scripted provider that 'fixes' a repo fixture by writing ``content`` to ``path``
    via the toolbox (defaults reproduce the calculator fix)."""

    def __init__(self, path: str = "calculator.py", content: str = _FIXED_CALCULATOR) -> None:
        self._path = path
        self._content = content
        self.usage = replay_eval._Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(
                title="Fix the planted bug",
                summary=f"Correct the buggy logic in {self._path}.",
                subtasks=[SubTask(id="s1", title="Fix the bug",
                                  description=f"Repair {self._path} so the suite passes.",
                                  agent="coder", rationale="a one-line code fix",
                                  depends_on=[], acceptance_criteria=["tests pass"])],
            )
        if schema is Verdict:
            return Verdict(passed=True, score=92, reasons=["bug fixed"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Fixed the bug.", key_points=["logic corrected"],
                            status="completed")
        if schema is RunLessons:
            return RunLessons(summary="Small bugfixes route well to the coder.",
                              what_worked=["direct fix"], what_to_avoid=[], routing_notes=[])
        raise AssertionError(f"unexpected schema {schema}")

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=None, workdir=None,
                        on_step=None):
        toolbox.dispatch("write_file", {"path": self._path, "content": self._content})
        return f"Fixed {self._path}; the suite now passes."

    async def aclose(self):
        return None


async def test_repo_golden_task_offline(tmp_path, monkeypatch):
    """E2+E3+F8: a fixture repo runs through the REAL project path — copied out,
    git-initialized, imported as an ephemeral project — then a (scripted) agent edits it
    and grading runs, including on held-out tests the agent never saw."""
    made = _track_attempt_dirs(monkeypatch, tmp_path)
    task = next(t for t in GOLDEN if t.id == "fix_calculator_subtract")
    report = await run_eval(_offline_settings(), only=[task.id], repeat=1, task_timeout=120,
                            provider_factory=_RepoFixProvider)
    assert len(report.cards) == 1
    card = report.cards[0]
    assert card.error == ""
    grader_names = [g.name for g in card.graders]
    assert "heldout_tests_pass" in grader_names  # E3 grader auto-attached for fixture tasks
    assert card.passed, [f"{g.name}: {g.detail}" for g in card.graders]
    assert card.subtasks_passed == card.subtasks_total == 1
    assert card.run_status == "completed"
    assert card.routing_accuracy == 1.0  # 'coder' is within expected_agents
    assert card.quality > 0
    summary = report.tasks[0]
    assert summary.pass_rate == 1.0
    assert summary.routing_accuracy == 1.0

    # ---- F8: the attempt went through an ephemeral project, not Settings.repo_path ----
    attempts = [Path(d) for d in made if Path(d).name.startswith(f"ada-eval-{task.id}-")]
    assert len(attempts) == 1
    attempt = attempts[0]
    slug = "eval-fix-calculator-subtract"  # slugify(f"eval-{task.id}")
    assert card.project == slug
    rows = RunStore(attempt / "data" / "runs.db").list()
    assert len(rows) == 1
    assert rows[0]["project"] == slug  # the run row is bound to the ephemeral project
    # The imported (user-side) fixture repo copy was operated on in place but never
    # mutated: still the single baseline commit, clean tree, no branch switch.
    repo = attempt / "fixture-repo"
    branch = _git_out(["branch", "--show-current"], repo)
    assert branch and not branch.startswith("ada/")
    assert _git_out(["rev-list", "--count", "HEAD"], repo) == "1"
    assert _git_out(["log", "-1", "--format=%s"], repo) == "fixture baseline"
    assert _git_out(["status", "--porcelain"], repo) == ""
    # The in-package fixture source tree was never turned into a git repo.
    assert not (FIXTURES_DIR / task.repo_fixture / "repo" / ".git").exists()


_FIXED_PAGINATION = '''\
"""Tiny pagination helpers (fixed)."""

from __future__ import annotations


def total_pages(total_items, page_size):
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if total_items <= 0:
        return 0
    return (total_items + page_size - 1) // page_size


def page_items(items, page, page_size):
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if page < 1:
        raise ValueError("page numbers start at 1")
    start = (page - 1) * page_size
    return list(items[start:start + page_size])
'''


async def test_pagination_repo_golden_task_offline(tmp_path, monkeypatch):
    """E2+E3 on the second Python fixture: the off-by-one pagination bug is fixed by a
    scripted agent and graded, including on held-out tests — again via an ephemeral
    project (F8)."""
    made = _track_attempt_dirs(monkeypatch, tmp_path)
    task = next(t for t in GOLDEN if t.id == "fix_pagination_total_pages")
    report = await run_eval(
        _offline_settings(), only=[task.id], repeat=1, task_timeout=120,
        provider_factory=lambda: _RepoFixProvider("pagination.py", _FIXED_PAGINATION))
    assert len(report.cards) == 1
    card = report.cards[0]
    assert card.error == ""
    assert not card.skipped
    assert "heldout_tests_pass" in [g.name for g in card.graders]
    assert card.passed, [f"{g.name}: {g.detail}" for g in card.graders]
    assert card.run_status == "completed"
    assert report.tasks[0].pass_rate == 1.0
    assert card.project == "eval-fix-pagination-total-pages"
    attempts = [Path(d) for d in made if Path(d).name.startswith(f"ada-eval-{task.id}-")]
    assert len(attempts) == 1
    rows = RunStore(attempts[0] / "data" / "runs.db").list()
    assert [r["project"] for r in rows] == [card.project]


async def test_missing_required_tool_skips_task(monkeypatch):
    """Toolchain gating: a task whose required tool is absent is SKIPPED, not failed,
    and skips are excluded from the pass/fail denominator."""
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None)
    task = GoldenTask(id="needs_go", prompt="unused", graders=[], required_tools=("go",))
    report = await run_eval(_offline_settings(), tasks=[task], repeat=3, task_timeout=5)
    assert len(report.cards) == 1  # one skip card regardless of repeat
    card = report.cards[0]
    assert card.skipped
    assert not card.passed
    assert "go" in card.error
    assert card.to_dict()["skipped"] is True  # additive key, to_dict stays compatible
    assert report.skipped == 1
    assert report.attempted == 0
    assert report.passed == len(report.cards)  # a skip never turns the suite red
    assert "SKIPPED" in report.summary()
    assert report.tasks[0].to_dict()["skipped"] == 1
    assert report.tasks[0].pass_rate == 0.0  # no attempted runs -> neutral zero


def test_golden_suite_shape():
    """The suite spans ~10 tasks; every fixture task has a repo, cross-language tasks
    declare their toolchain and ship held-out tests."""
    ids = [t.id for t in GOLDEN]
    assert len(ids) == len(set(ids))
    assert len(GOLDEN) >= 10
    for t in GOLDEN:
        if t.repo_fixture:
            assert (FIXTURES_DIR / t.repo_fixture / "repo").is_dir(), t.id
            assert t.heldout_dir is not None, t.id
    for tid in ("fix_js_clamp", "fix_go_leap_year", "fix_rust_temp_conversion",
                "fix_ruby_titlecase"):
        t = next(x for x in GOLDEN if x.id == tid)
        assert t.required_tools, tid


@pytest.mark.skipif(shutil.which("node") is None or shutil.which("npm") is None,
                    reason="node/npm not installed")
def test_heldout_merge_grading_js_fixture(tmp_path):
    """Non-Python held-out grading: the held-out tree is merged into the workspace and
    the project's own test command (npm test) runs over the merged result."""
    fixture = FIXTURES_DIR / "bugfix_js_clamp"
    ws = tmp_path / "ws"
    shutil.copytree(fixture / "repo", ws)

    res = heldout_tests_pass(ws, fixture / "heldout", timeout=120)
    assert not res.passed  # planted swapped-bounds bug is visible to the held-out tests
    assert "npm" in res.detail

    fixed = (ws / "clamp.js").read_text()
    fixed = fixed.replace("if (value < min) return max;", "if (value < min) return min;")
    fixed = fixed.replace("if (value > max) return min;", "if (value > max) return max;")
    (ws / "clamp.js").write_text(fixed)
    res = heldout_tests_pass(ws, fixture / "heldout", timeout=120)
    assert res.passed, res.detail
    assert (ws / "heldout.test.js").is_file()  # merged in alongside the repo tests


def test_replay_eval_offline_with_repeat():
    """E4+E5: the committed cassettes drive the full pipeline offline; repeat=2 exercises
    the variance aggregation on identical deterministic runs."""
    report = replay_eval.run_replay_eval(repeat=2)
    assert len(report.cards) == 2
    for card in report.cards:
        assert card.error == ""
        assert card.passed
        assert card.subtasks_passed == card.subtasks_total == 3
        assert card.run_status == "completed"
        assert card.cost_usd == 0.0  # ReplayProvider never touches the network
    summary = report.tasks[0]
    assert summary.task_id == "replay_greeting"
    assert summary.pass_rate == 1.0
    assert summary.mean_quality == summary.min_quality == 100.0
    assert summary.parallelism >= 1
    assert summary.routing_accuracy == 1.0


class _SlowProvider:
    def __init__(self) -> None:
        self.usage = replay_eval._Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    async def run_agent(self, **kwargs):
        await asyncio.sleep(10)
        return ""

    async def aclose(self):
        return None


async def test_task_timeout_bounds_an_attempt():
    """E5: a hung task is cut off by the per-task timeout and reported, not hung forever."""
    task = GoldenTask(id="hangs", prompt="never finishes", graders=[])
    report = await run_eval(_offline_settings(), tasks=[task], repeat=1, task_timeout=0.3,
                            provider_factory=_SlowProvider)
    card = report.cards[0]
    assert not card.passed
    assert "timed out" in card.error
    assert card.wall_s < 10


def test_eval_env_knobs(monkeypatch):
    monkeypatch.setenv("ADA_EVAL_REPEAT", "3")
    monkeypatch.setenv("ADA_EVAL_TASK_TIMEOUT", "42.5")
    assert _env_repeat() == 3
    assert _env_task_timeout() == 42.5
    monkeypatch.setenv("ADA_EVAL_REPEAT", "not-a-number")
    monkeypatch.setenv("ADA_EVAL_TASK_TIMEOUT", "")
    assert _env_repeat() == 1
    assert _env_task_timeout() == 600.0
