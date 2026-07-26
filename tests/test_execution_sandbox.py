"""Sandbox behavior of execution.py: env scrubbing, process-group kill, test selection,
isolation-backend fallback, and the tag -> process-group cancellation registry."""

import asyncio
import sys
import threading
import time

import pytest

import ai_dev_assistant.execution as execution
from ai_dev_assistant.execution import (
    detect_test_command,
    run_command,
    run_command_sync,
    terminate_tag,
)

_PRINT_SENTINEL = "import os; print(os.environ.get('ADA_TEST_SENTINEL', 'ABSENT'))"


@pytest.fixture(autouse=True)
def _unconfigured_sandbox(monkeypatch):
    # An engine test elsewhere in the session may have bound its run's settings
    # via configure_sandbox(); these tests exercise the env-var fallback and
    # explicit configuration, so start each one unconfigured.
    monkeypatch.setattr(execution, "_SANDBOX_CONFIG", None)


def test_sync_sandbox_scrubs_parent_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_TEST_SENTINEL", "super-secret-value")
    r = run_command_sync([sys.executable, "-c", _PRINT_SENTINEL], tmp_path, 30, sandbox=True)
    assert r.passed
    assert "ABSENT" in r.stdout and "super-secret-value" not in r.stdout


def test_sync_no_sandbox_inherits_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_TEST_SENTINEL", "super-secret-value")
    r = run_command_sync([sys.executable, "-c", _PRINT_SENTINEL], tmp_path, 30, sandbox=False)
    assert r.passed and "super-secret-value" in r.stdout


def test_async_sandbox_scrubs_parent_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_TEST_SENTINEL", "super-secret-value")
    r = asyncio.run(
        run_command([sys.executable, "-c", _PRINT_SENTINEL], tmp_path, 30, sandbox=True)
    )
    assert r.passed
    assert "ABSENT" in r.stdout and "super-secret-value" not in r.stdout


def test_sync_timeout_kills_process_group(tmp_path):
    # 'sleep 30 &' leaves a grandchild holding the stdout pipe; without a group kill,
    # communicate() blocks until the sleep exits (~30s).
    start = time.monotonic()
    r = run_command_sync(["sh", "-c", "sleep 30 & wait"], tmp_path, 1, sandbox=True)
    elapsed = time.monotonic() - start
    assert r.timed_out and not r.passed
    assert elapsed < 10, f"group kill too slow: {elapsed:.1f}s"


def test_async_timeout_kills_process_group(tmp_path):
    start = time.monotonic()
    r = asyncio.run(run_command(["sh", "-c", "sleep 30 & wait"], tmp_path, 1))
    elapsed = time.monotonic() - start
    assert r.timed_out and not r.passed
    assert elapsed < 10, f"group kill too slow: {elapsed:.1f}s"


def test_detect_test_command_appends_paths(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / "test_b.py").write_text("def test_b():\n    assert True\n")
    cmd = detect_test_command(tmp_path, ["test_b.py"])
    assert cmd is not None and "pytest" in " ".join(cmd)
    assert cmd[-1] == "test_b.py"
    # without paths, nothing is appended
    plain = detect_test_command(tmp_path)
    assert plain is not None and "test_b.py" not in plain


def test_detect_test_command_npm_ignores_paths(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node t.js"}}')
    cmd = detect_test_command(tmp_path, ["test_b.py"])
    assert cmd is not None and cmd[0] == "npm"
    assert "test_b.py" not in cmd


# --- S5: isolation backends fall back to subprocess hardening when binaries are missing ---


def test_bwrap_falls_back_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_SANDBOX", "bwrap")
    monkeypatch.setattr(execution.shutil, "which", lambda name: None)
    r = run_command_sync([sys.executable, "-c", "print('ok')"], tmp_path, 30, sandbox=True)
    assert r.passed and "ok" in r.stdout


def test_container_falls_back_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_SANDBOX", "container")
    monkeypatch.setattr(execution.shutil, "which", lambda name: None)
    r = run_command_sync([sys.executable, "-c", "print('ok')"], tmp_path, 30, sandbox=True)
    assert r.passed and "ok" in r.stdout


def test_async_backend_falls_back_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_SANDBOX", "container")
    monkeypatch.setattr(execution.shutil, "which", lambda name: None)
    r = asyncio.run(
        run_command([sys.executable, "-c", "print('ok')"], tmp_path, 30, sandbox=True)
    )
    assert r.passed and "ok" in r.stdout


def test_fallback_still_scrubs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_SANDBOX", "bwrap")
    monkeypatch.setenv("ADA_TEST_SENTINEL", "super-secret-value")
    monkeypatch.setattr(execution.shutil, "which", lambda name: None)
    r = run_command_sync([sys.executable, "-c", _PRINT_SENTINEL], tmp_path, 30, sandbox=True)
    assert r.passed
    assert "ABSENT" in r.stdout and "super-secret-value" not in r.stdout


# --- Settings-driven selection: configure_sandbox() beats env; env is the fallback ---


def test_configure_sandbox_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_SANDBOX", "container")
    monkeypatch.setattr(execution.shutil, "which", lambda name: f"/usr/bin/{name}")
    execution.configure_sandbox("subprocess")
    assert execution._apply_sandbox_backend(["echo", "hi"], tmp_path) == ["echo", "hi"]
    # and the other direction: env says subprocess, settings say bwrap
    monkeypatch.setenv("ADA_SANDBOX", "subprocess")
    execution.configure_sandbox("bwrap")
    assert execution._apply_sandbox_backend(["echo", "hi"], tmp_path)[0] == "bwrap"


def test_env_fallback_when_never_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_SANDBOX", "container")
    monkeypatch.setattr(execution.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert execution._apply_sandbox_backend(["echo", "hi"], tmp_path)[0] == "docker"


def test_configured_backend_missing_binary_still_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(execution.shutil, "which", lambda name: None)
    execution.configure_sandbox("container")
    r = run_command_sync([sys.executable, "-c", "print('ok')"], tmp_path, 30, sandbox=True)
    assert r.passed and "ok" in r.stdout


def test_none_backend_is_a_silent_passthrough(tmp_path):
    # "none" is a legitimate settings choice, not an unknown backend.
    execution.configure_sandbox("none")
    assert execution._apply_sandbox_backend(["echo", "hi"], tmp_path) == ["echo", "hi"]
    assert "none" not in execution._FALLBACK_WARNED


def test_allow_network_toggles_bwrap_and_container_flags(tmp_path):
    execution.configure_sandbox("bwrap", allow_network=False)
    assert "--unshare-net" in execution._wrap_bwrap(["true"], tmp_path)
    assert "--network=none" in execution._wrap_container(["true"], tmp_path)
    execution.configure_sandbox("bwrap", allow_network=True)
    assert "--unshare-net" not in execution._wrap_bwrap(["true"], tmp_path)
    assert "--network=none" not in execution._wrap_container(["true"], tmp_path)


def test_env_net_fallback_requires_exactly_1(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_SANDBOX_NET", "1")
    assert "--unshare-net" not in execution._wrap_bwrap(["true"], tmp_path)
    monkeypatch.setenv("ADA_SANDBOX_NET", "true")  # legacy semantics: only "1" allows
    assert "--unshare-net" in execution._wrap_bwrap(["true"], tmp_path)


def test_container_image_configured_env_and_default(tmp_path, monkeypatch):
    execution.configure_sandbox("container", image="custom:img")
    assert "custom:img" in execution._wrap_container(["python", "x.py"], tmp_path)
    # configured with a blank image -> per-command default, env var no longer read
    execution.configure_sandbox("container")
    monkeypatch.setenv("ADA_SANDBOX_IMAGE", "env:img")
    cmd = execution._wrap_container(["python", "x.py"], tmp_path)
    assert "python:3.12-slim" in cmd and "env:img" not in cmd
    # never configured -> the env var applies
    monkeypatch.setattr(execution, "_SANDBOX_CONFIG", None)
    assert "env:img" in execution._wrap_container(["python", "x.py"], tmp_path)


# --- R5: tag registry lets the engine hard-cancel everything a run spawned ---


def test_terminate_tag_kills_async_tagged_group(tmp_path):
    async def scenario():
        task = asyncio.create_task(
            run_command(["sh", "-c", "sleep 30 & wait"], tmp_path, 30, tag="cancel-async")
        )
        await asyncio.sleep(0.5)
        assert terminate_tag("cancel-async") == 1
        return await asyncio.wait_for(task, timeout=8)

    start = time.monotonic()
    r = asyncio.run(scenario())
    assert time.monotonic() - start < 10, "terminate_tag did not kill the group quickly"
    assert not r.passed
    # the registry entry is consumed — nothing left under the tag
    assert terminate_tag("cancel-async") == 0


def test_terminate_tag_kills_sync_tagged_group(tmp_path):
    result: dict = {}

    def target():
        result["r"] = run_command_sync(
            ["sh", "-c", "sleep 30 & wait"], tmp_path, 30, tag="cancel-sync"
        )

    t = threading.Thread(target=target)
    t.start()
    time.sleep(0.5)
    killed = terminate_tag("cancel-sync")
    t.join(timeout=8)
    assert not t.is_alive(), "sync call did not return after terminate_tag"
    assert killed == 1
    assert not result["r"].passed


def test_terminate_tag_unknown_tag_is_noop():
    assert terminate_tag("never-registered") == 0
