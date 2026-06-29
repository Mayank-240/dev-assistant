"""Run generated code/tests in a workspace to get objective verification.

Detects a test command in the workspace (pytest for Python, ``npm test`` for Node) and
runs it with a timeout. Both async (engine) and sync (agent tool) entry points share the
same detection + result shape.

Hardening (Tier 2): model-authored tests/scripts are untrusted, so the sync path can run
with a scrubbed environment (no API keys leak), POSIX rlimits (CPU/address-space), and a
new process group that is killed as a whole on timeout — not just the direct child.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_MAX_OUT = 6000

# Environment kept when scrubbing — never pass secrets to untrusted child processes.
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "TZ",
              "PYTHONPATH", "VIRTUAL_ENV", "SYSTEMROOT", "PATHEXT")


@dataclass
class ExecutionResult:
    command: str
    return_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.return_code == 0 and not self.timed_out


def _trim(s: str) -> str:
    s = s or ""
    return s if len(s) <= _MAX_OUT else "…" + s[-_MAX_OUT:]


def scrubbed_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}


def _rlimit_preexec(cpu_seconds: int, mem_mb: int):
    """Return a preexec_fn that sets CPU/address-space limits + a new session (POSIX only)."""
    if os.name != "posix":
        return None

    def _apply() -> None:  # pragma: no cover - exercised only in subprocesses
        import resource

        os.setsid()  # new process group so we can kill the whole tree on timeout
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
        except (ValueError, OSError):
            pass
        if mem_mb:
            try:
                soft = mem_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (soft, soft))
            except (ValueError, OSError):
                pass

    return _apply


def detect_test_command(workspace: Path) -> list[str] | None:
    """Best-effort detection of how to test the project in ``workspace``."""
    if not workspace.exists():
        return None
    if any(workspace.rglob("test_*.py")) or any(workspace.rglob("*_test.py")):
        return [sys.executable, "-m", "pytest", "-q"]
    pkg = workspace / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text())
            if isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
                return ["npm", "test", "--silent"]
        except Exception:
            pass
    return None


async def run_command(command: list[str], cwd: Path, timeout: float) -> ExecutionResult:
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *command, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        out, err = await proc.communicate()
    return ExecutionResult(
        command=" ".join(command),
        return_code=proc.returncode if proc.returncode is not None else -1,
        stdout=_trim(out.decode(errors="replace")),
        stderr=_trim(err.decode(errors="replace")),
        duration=time.monotonic() - start,
        timed_out=timed_out,
    )


def run_command_sync(command: list[str], cwd: Path, timeout: float, *,
                     sandbox: bool = False, cpu_seconds: int = 60, mem_mb: int = 1024) -> ExecutionResult:
    start = time.monotonic()
    kwargs: dict = {"cwd": str(cwd), "capture_output": True, "timeout": timeout, "text": True}
    if sandbox:
        kwargs["env"] = scrubbed_env()
        preexec = _rlimit_preexec(cpu_seconds, mem_mb)
        if preexec is not None:
            kwargs["preexec_fn"] = preexec
            kwargs["start_new_session"] = True
    try:
        p = subprocess.run(command, **kwargs)
        return ExecutionResult(" ".join(command), p.returncode, _trim(p.stdout), _trim(p.stderr),
                               time.monotonic() - start, False)
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(" ".join(command), -1, _trim(exc.stdout or ""), _trim(exc.stderr or ""),
                               time.monotonic() - start, True)
    except FileNotFoundError as exc:
        return ExecutionResult(" ".join(command), -1, "", f"command not found: {exc}",
                               time.monotonic() - start, False)


async def run_workspace_tests(workspace: Path, timeout: float) -> ExecutionResult | None:
    cmd = detect_test_command(workspace)
    return await run_command(cmd, workspace, timeout) if cmd else None


def run_workspace_tests_sync(workspace: Path, timeout: float) -> ExecutionResult | None:
    cmd = detect_test_command(workspace)
    return run_command_sync(cmd, workspace, timeout) if cmd else None
