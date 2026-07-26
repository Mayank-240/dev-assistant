"""Static-analysis objective gates: detection, count parsing, baseline/delta math,
and the engine-level gate wiring.

CASSETTE-CRITICAL feature: when no checks are detected (or the setting is off) every
prompt must be byte-identical to a control run — proven below with the captured-prompt
technique from tests/test_workspace_context.py.
"""

from __future__ import annotations

import dataclasses

from test_engine_e2e import FakeProvider, _settings

import ai_dev_assistant.static_checks as static_checks
import ai_dev_assistant.verification as verification
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.evals.replay_eval import replay_base_settings
from ai_dev_assistant.execution import ExecutionResult
from ai_dev_assistant.llm.schemas import Verdict
from ai_dev_assistant.static_checks import (
    Check,
    detect_static_checks,
    run_static_checks,
    static_delta_note,
)
from ai_dev_assistant.verification import apply_objective_gate, capture_baseline


def _res(stdout: str = "", rc: int = 0, timed_out: bool = False) -> ExecutionResult:
    return ExecutionResult("cmd", rc, stdout, "", 0.01, timed_out)


# --- detection table ---------------------------------------------------------------


def test_detect_ruff_only_repo(tmp_path, monkeypatch):
    (tmp_path / "m.py").write_text("x = 1\n")
    monkeypatch.setattr(static_checks.shutil, "which",
                        lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    assert [c.name for c in detect_static_checks(tmp_path)] == ["ruff"]


def test_detect_ruff_via_config_without_py_files(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    monkeypatch.setattr(static_checks.shutil, "which",
                        lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    assert [c.name for c in detect_static_checks(tmp_path)] == ["ruff"]


def test_detect_tsc_repo(tmp_path, monkeypatch):
    (tmp_path / "tsconfig.json").write_text("{}\n")
    monkeypatch.setattr(static_checks.shutil, "which",
                        lambda name: "/usr/bin/tsc" if name == "tsc" else None)
    checks = detect_static_checks(tmp_path)
    assert [c.name for c in checks] == ["tsc"]
    assert checks[0].cmd == ("tsc", "--noEmit")


def test_detect_eslint_prefers_local_bin(tmp_path, monkeypatch):
    (tmp_path / "eslint.config.js").write_text("export default [];\n")
    local = tmp_path / "node_modules" / ".bin" / "eslint"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    monkeypatch.setattr(static_checks.shutil, "which", lambda name: None)
    checks = detect_static_checks(tmp_path)
    assert [c.name for c in checks] == ["eslint"]
    assert checks[0].cmd[0] == str(local) and checks[0].cmd[1:] == ("-f", "unix", ".")


def test_detect_nothing_when_tools_absent_or_repo_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(static_checks.shutil, "which", lambda name: None)
    (tmp_path / "m.py").write_text("x = 1\n")            # .py files but no ruff on PATH
    (tmp_path / "eslint.config.js").write_text("[];\n")  # config but no eslint anywhere
    assert detect_static_checks(tmp_path) == []
    assert detect_static_checks(tmp_path / "missing") == []


# --- count parsing per tool (fake run_command) -------------------------------------


def _run_with(monkeypatch, result: ExecutionResult, check: Check, tmp_path):
    monkeypatch.setattr(static_checks, "run_command_sync",
                        lambda cmd, cwd, timeout, **kw: result)
    return run_static_checks(tmp_path, [check])


def test_ruff_concise_line_count(tmp_path, monkeypatch):
    out = ("a.py:1:1: F401 `os` imported but unused\n"
           "pkg/b.py:12:80: E501 Line too long\n"
           "Found 2 errors.\n")
    counts = _run_with(monkeypatch, _res(out, rc=1),
                       Check("ruff", ("ruff",), static_checks._count_diag_lines), tmp_path)
    assert counts == {"ruff": 2}


def test_eslint_unix_line_count(tmp_path, monkeypatch):
    out = ("/w/src/a.js:3:5: 'x' is not defined. [Error/no-undef]\n"
           "\n"
           "1 problem\n")
    counts = _run_with(monkeypatch, _res(out, rc=1),
                       Check("eslint", ("eslint",), static_checks._count_diag_lines), tmp_path)
    assert counts == {"eslint": 1}


def test_tsc_error_line_count(tmp_path, monkeypatch):
    out = ("src/a.ts(1,1): error TS2304: Cannot find name 'foo'.\n"
           "src/a.ts(9,3): error TS2322: Type 'string' is not assignable.\n"
           "some non-error chatter\n")
    counts = _run_with(monkeypatch, _res(out, rc=2),
                       Check("tsc", ("tsc",), static_checks._count_tsc_errors), tmp_path)
    assert counts == {"tsc": 2}


def test_clean_run_counts_zero(tmp_path, monkeypatch):
    counts = _run_with(monkeypatch, _res("All checks passed!\n", rc=0),
                       Check("ruff", ("ruff",), static_checks._count_diag_lines), tmp_path)
    assert counts == {"ruff": 0}


# --- failure safety: timeout / crash checks are skipped, never blocking ------------


def test_timed_out_check_is_skipped(tmp_path, monkeypatch):
    counts = _run_with(monkeypatch, _res("", rc=-1, timed_out=True),
                       Check("ruff", ("ruff",), static_checks._count_diag_lines), tmp_path)
    assert counts == {}


def test_tool_crash_without_diagnostics_is_skipped(tmp_path, monkeypatch):
    # eslint exits 2 on a config error with no path:line:col lines — not an honest zero
    counts = _run_with(monkeypatch, _res("Oops! Something went wrong!\n", rc=2),
                       Check("eslint", ("eslint",), static_checks._count_diag_lines), tmp_path)
    assert counts == {}


def test_missing_binary_is_skipped(tmp_path):
    counts = run_static_checks(
        tmp_path, [Check("ruff", ("definitely-not-a-real-tool-xyz",),
                         static_checks._count_diag_lines)])
    assert counts == {}


# --- baseline/delta math (incl. negative) and the gate -----------------------------


def test_delta_note_positive_negative_zero():
    note, regressed = static_delta_note({"ruff": 15, "tsc": 0}, {"ruff": 12})
    assert note == "static checks: ruff +3 (baseline 12), tsc +0"
    assert regressed
    note, regressed = static_delta_note({"ruff": 10}, {"ruff": 12})
    assert note == "static checks: ruff -2 (baseline 12)" and not regressed
    note, regressed = static_delta_note({}, {"ruff": 12})
    assert note == "" and not regressed


def test_gate_demotes_on_positive_delta_like_failed_tests():
    out = apply_objective_gate(
        Verdict(passed=True, score=95),
        {"static": {"ruff": 3}}, baseline={"static": {"ruff": 0}})
    assert not out.passed and out.score == 20
    assert "static checks: ruff +3" in out.objective_note
    assert any("static checks regressed" in r for r in out.reasons)


def test_gate_zero_and_negative_deltas_note_only():
    for post, base in (({"ruff": 2}, {"ruff": 2}), ({"ruff": 1}, {"ruff": 4})):
        out = apply_objective_gate(
            Verdict(passed=True, score=95), {"static": post}, baseline={"static": base})
        assert out.passed and out.score == 95
        assert "static checks: ruff" in out.objective_note


def test_gate_static_regression_blocks_scoped_soft_pass():
    ok_tests = ExecutionResult("pytest", 0, "", "", 0.1)
    out = apply_objective_gate(
        Verdict(passed=False, score=40),
        {"tests": ok_tests, "scoped": True, "static": {"ruff": 5}},
        baseline={"static": {"ruff": 0}})
    assert not out.passed and out.score == 20


def test_capture_baseline_static_capture(tmp_path, monkeypatch):
    check = Check("ruff", ("ruff",), static_checks._count_diag_lines)
    monkeypatch.setattr(verification, "detect_static_checks", lambda ws: [check])
    monkeypatch.setattr(verification, "run_static_checks",
                        lambda ws, checks, timeout=60.0: {"ruff": 12})
    base = capture_baseline(tmp_path, run_tests=False, lint=False, timeout=10, static=True)
    assert base["static"] == {"ruff": 12} and base["static_checks"] == [check]
    base = capture_baseline(tmp_path, run_tests=False, lint=False, timeout=10, static=False)
    assert "static" not in base and "static_checks" not in base


# --- engine e2e: gate wiring with a stubbed check runner ---------------------------


class _CaptureProvider(FakeProvider):
    """Records every agent prompt AND every structured (system, user) payload, so
    byte-identity covers reviewer prompts too."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        self.prompts.append(f"[structured]\n{system}\n---\n{user}")
        return await super().structured(system=system, user=user, schema=schema,
                                        model=model, effort=effort, max_tokens=max_tokens)

    async def run_agent(self, *, prompt, **kw):
        self.prompts.append(prompt)
        return await super().run_agent(prompt=prompt, **kw)


def _static_settings(tmp_path, **over):
    return dataclasses.replace(
        _settings(tmp_path), objective_review=True, lint_check=False,
        verify_run_tests=False, **over)


def _stub_checks(monkeypatch, baseline_count: int, post_count: int):
    """First run_static_checks call = the run baseline; every later call = post-change."""
    calls = {"n": 0}
    check = Check("ruff", ("ruff", "check"), static_checks._count_diag_lines)
    monkeypatch.setattr(verification, "detect_static_checks", lambda ws: [check])

    def fake_run(ws, checks, timeout=60.0):
        calls["n"] += 1
        return {"ruff": baseline_count if calls["n"] == 1 else post_count}

    monkeypatch.setattr(verification, "run_static_checks", fake_run)
    return calls


async def _run_engine(settings, fake: FakeProvider) -> tuple[list[dict], object]:
    engine = Engine(settings)
    engine.provider = fake
    engine.orchestrator._provider = fake
    engine.reviewer._provider = fake
    events: list = []
    try:
        run, _brief, _out = await engine.run("Add input validation and document it",
                                             on_event=events.append)
    finally:
        await engine.aclose()
    reviews = [e.data for e in events if e.type == "subtask_review"]
    return reviews, run


async def test_engine_regression_demotes_verdict_with_note(tmp_path, monkeypatch):
    calls = _stub_checks(monkeypatch, baseline_count=0, post_count=3)
    reviews, run = await _run_engine(_static_settings(tmp_path), _CaptureProvider())
    assert calls["n"] >= 2  # baseline once + at least one per-subtask re-count
    assert reviews and all(not r["passed"] and r["score"] <= 20 for r in reviews)
    assert all("static checks: ruff +3" in r["objective_note"] for r in reviews)
    assert not run.all_passed


async def test_engine_zero_delta_is_note_only(tmp_path, monkeypatch):
    _stub_checks(monkeypatch, baseline_count=2, post_count=2)
    reviews, run = await _run_engine(_static_settings(tmp_path), _CaptureProvider())
    assert reviews and all(r["passed"] and r["score"] == 95 for r in reviews)
    assert all("static checks: ruff +0 (baseline 2)" in r["objective_note"] for r in reviews)
    assert run.all_passed


async def test_engine_detection_empty_prompts_byte_identical(tmp_path, monkeypatch):
    """No tools detected → nothing appended anywhere: every prompt equals a control
    run with the feature off entirely."""
    monkeypatch.setattr(verification, "detect_static_checks", lambda ws: [])
    control = _CaptureProvider()
    await _run_engine(_static_settings(tmp_path / "control", objective_static=False), control)
    feature = _CaptureProvider()
    reviews, _ = await _run_engine(_static_settings(tmp_path / "feature"), feature)
    assert len(control.prompts) == len(feature.prompts) > 0
    assert sorted(control.prompts) == sorted(feature.prompts)  # byte-identical
    assert all("static checks" not in (r["objective_note"] or "") for r in reviews)


async def test_engine_setting_off_prompts_byte_identical(tmp_path, monkeypatch):
    """objective_static=False must neutralize the gate even when checks WOULD detect
    and regress — prompts and verdicts match a run where nothing exists to detect."""
    control = _CaptureProvider()
    with monkeypatch.context() as m:
        m.setattr(verification, "detect_static_checks", lambda ws: [])
        await _run_engine(_static_settings(tmp_path / "control", objective_static=False),
                          control)
    _stub_checks(monkeypatch, baseline_count=0, post_count=40)
    feature = _CaptureProvider()
    reviews, run = await _run_engine(
        _static_settings(tmp_path / "feature", objective_static=False), feature)
    assert sorted(control.prompts) == sorted(feature.prompts)
    assert reviews and all(r["passed"] for r in reviews) and run.all_passed
    assert all("static checks" not in (r["objective_note"] or "") for r in reviews)


# --- replay eval determinism: the knob is pinned off like adaptive_replan ----------


def test_replay_base_settings_pin_static_off():
    assert replay_base_settings().objective_static is False
