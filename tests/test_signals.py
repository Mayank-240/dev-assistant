"""SignalProvider registry (V5), extra-signal gating, and multi-language detection (V6)."""

import ai_dev_assistant.verification as verification
from ai_dev_assistant.execution import ExecutionResult, detect_test_command
from ai_dev_assistant.llm.schemas import Verdict
from ai_dev_assistant.verification import (
    SIGNAL_PROVIDERS,
    SignalProvider,
    apply_objective_gate,
    capture_baseline,
    gather_signals,
    register_signal_provider,
    run_signal_providers,
)


def _res(passed: bool) -> ExecutionResult:
    return ExecutionResult("cmd", 0 if passed else 1, "", "", 0.01)


# --- provider availability: degrade silently when tools are absent (CI has none) ---


def test_no_providers_on_empty_workspace(tmp_path):
    assert run_signal_providers(tmp_path, [], 10) == {}


def test_typecheck_and_coverage_skip_when_tools_absent(tmp_path, monkeypatch):
    (tmp_path / "m.py").write_text("x = 1\n")
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    monkeypatch.setattr(verification.importlib.util, "find_spec", lambda name: None)
    extra = run_signal_providers(tmp_path, [], 15)
    assert "typecheck" not in extra
    assert "coverage" not in extra
    # the build provider needs no external tool for Python (compileall sweep)
    assert "build" in extra and extra["build"].passed


def test_build_signal_catches_syntax_error(tmp_path):
    (tmp_path / "broken.py").write_text("def oops(:\n")
    extra = run_signal_providers(tmp_path, ["broken.py"], 15)
    assert "build" in extra and not extra["build"].passed


def test_broken_provider_never_aborts(tmp_path):
    class Exploding(SignalProvider):
        name = "exploding"

        def available(self, workspace):
            return True

        def run(self, workspace, changed_files, timeout):
            raise RuntimeError("boom")

    prov = Exploding()
    register_signal_provider(prov)
    try:
        extra = run_signal_providers(tmp_path, [], 10)
        assert "exploding" not in extra
    finally:
        SIGNAL_PROVIDERS.remove(prov)


# --- registry: a registered provider flows into signals["extra"] and the baseline ---


class _FakeProvider(SignalProvider):
    name = "fake"

    def available(self, workspace):
        return True

    def run(self, workspace, changed_files, timeout):
        return _res(True)


def test_registered_provider_in_signals_and_baseline(tmp_path):
    prov = _FakeProvider()
    register_signal_provider(prov)
    try:
        signals = gather_signals(tmp_path, [], run_tests=False, lint=False, timeout=10)
        assert signals["extra"]["fake"].passed
        baseline = capture_baseline(tmp_path, run_tests=False, lint=False, timeout=10)
        assert baseline["extra"]["fake"].passed
    finally:
        SIGNAL_PROVIDERS.remove(prov)


# --- gate: baseline-green -> now-red typecheck/build subtracts 15, never hard-fails ---


def test_gate_subtracts_on_typecheck_regression():
    out = apply_objective_gate(
        Verdict(passed=True, score=90),
        {"extra": {"typecheck": _res(False)}},
        baseline={"extra": {"typecheck": _res(True)}},
    )
    assert out.passed  # a signal regression is a penalty, not a hard fail
    assert out.score == 75
    assert "typecheck" in out.objective_note


def test_gate_stacks_typecheck_and_build_penalties():
    out = apply_objective_gate(
        Verdict(passed=True, score=90),
        {"extra": {"typecheck": _res(False), "build": _res(False)}},
        baseline={"extra": {"typecheck": _res(True), "build": _res(True)}},
    )
    assert out.passed and out.score == 60


def test_gate_ignores_signal_failing_at_baseline_too():
    out = apply_objective_gate(
        Verdict(passed=True, score=90),
        {"extra": {"build": _res(False)}},
        baseline={"extra": {"build": _res(False)}},
    )
    assert out.passed and out.score == 90
    assert "build" in out.objective_note  # noted, not blamed


def test_gate_no_penalty_without_baseline_entry():
    out = apply_objective_gate(
        Verdict(passed=True, score=90),
        {"extra": {"typecheck": _res(False)}},
    )
    assert out.passed and out.score == 90


# --- V6: multi-language detect_test_command ---


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    assert detect_test_command(tmp_path) == ["go", "test", "./..."]


def test_detect_go_scopes_paths_to_packages(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    cmd = detect_test_command(tmp_path, ["pkg/a/a_test.go", "main_test.go"])
    assert cmd is not None and cmd[:2] == ["go", "test"]
    assert "./pkg/a" in cmd and "." in cmd
    assert "./..." not in cmd


def test_detect_cargo_ignores_paths(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
    assert detect_test_command(tmp_path) == ["cargo", "test", "-q"]
    assert detect_test_command(tmp_path, ["src/lib.rs"]) == ["cargo", "test", "-q"]


def test_detect_maven_via_pom(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>\n")
    assert detect_test_command(tmp_path) == ["mvn", "-q", "test"]


def test_detect_maven_via_mvnw(tmp_path):
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    assert detect_test_command(tmp_path) == ["mvn", "-q", "test"]


def test_detect_gradle(tmp_path):
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    assert detect_test_command(tmp_path) == ["./gradlew", "test"]


def test_detect_ruby_rake_then_rspec(tmp_path):
    (tmp_path / "Rakefile").write_text("")
    assert detect_test_command(tmp_path) == ["rake", "test"]
    (tmp_path / "spec").mkdir()
    assert detect_test_command(tmp_path) == ["bundle", "exec", "rspec"]


def test_pytest_precedence_over_other_languages(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n")
    cmd = detect_test_command(tmp_path)
    assert cmd is not None and "pytest" in " ".join(cmd)
