"""Eval harness tests: deterministic graders (no LLM), held-out grading (E3), repo-level
golden tasks driven offline by a scripted provider (E2), repeat/metrics aggregation and
per-task timeouts (E5), and the fully-offline replay eval over committed cassettes (E4)."""

from __future__ import annotations

import asyncio
import shutil

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
    """Scripted provider that actually fixes the calculator fixture via the toolbox."""

    def __init__(self) -> None:
        self.usage = replay_eval._Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(
                title="Fix Subtract Bug",
                summary="Correct the reversed operands in calculator.subtract.",
                subtasks=[SubTask(id="s1", title="Fix the subtract bug",
                                  description="Repair calculator.py so the suite passes.",
                                  agent="coder", rationale="a one-line code fix",
                                  depends_on=[], acceptance_criteria=["tests pass"])],
            )
        if schema is Verdict:
            return Verdict(passed=True, score=92, reasons=["bug fixed"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Fixed subtract.", key_points=["operands reordered"],
                            status="completed")
        if schema is RunLessons:
            return RunLessons(summary="Small bugfixes route well to the coder.",
                              what_worked=["direct fix"], what_to_avoid=[], routing_notes=[])
        raise AssertionError(f"unexpected schema {schema}")

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=None, workdir=None,
                        on_step=None):
        toolbox.dispatch("write_file", {"path": "calculator.py", "content": _FIXED_CALCULATOR})
        return "Fixed subtract(a, b) to return a - b; the suite now passes."

    async def aclose(self):
        return None


async def test_repo_golden_task_offline():
    """E2+E3: a fixture repo is materialized, edited by a (scripted) agent, then graded —
    including on held-out tests the agent never saw."""
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
