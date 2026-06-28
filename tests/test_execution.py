import sys
from pathlib import Path

from ai_dev_assistant.execution import (
    detect_test_command,
    run_command_sync,
    run_workspace_tests_sync,
)


def test_run_command_sync_success():
    r = run_command_sync([sys.executable, "-c", "print('hello')"], Path("."), 30)
    assert r.return_code == 0
    assert "hello" in r.stdout
    assert r.passed


def test_run_command_sync_failure():
    r = run_command_sync([sys.executable, "-c", "import sys; sys.exit(3)"], Path("."), 30)
    assert r.return_code == 3
    assert not r.passed


def test_run_command_sync_missing_binary():
    r = run_command_sync(["definitely-not-a-real-binary-xyz"], Path("."), 10)
    assert not r.passed


def test_detect_none_when_no_tests(tmp_path):
    assert detect_test_command(tmp_path) is None


def test_detect_pytest_when_test_file(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n")
    cmd = detect_test_command(tmp_path)
    assert cmd and "pytest" in " ".join(cmd)


def test_run_workspace_tests_sync_runs_pytest(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_math():\n    assert 1 + 1 == 2\n")
    r = run_workspace_tests_sync(tmp_path, 60)
    assert r is not None
    assert r.passed, r.stdout + r.stderr


def test_run_workspace_tests_sync_detects_failure(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_bad():\n    assert 1 == 2\n")
    r = run_workspace_tests_sync(tmp_path, 60)
    assert r is not None
    assert not r.passed
