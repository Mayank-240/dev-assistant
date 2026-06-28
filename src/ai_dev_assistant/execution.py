"""Run generated code/tests in a workspace to get objective verification.

Detects a test command in the workspace (pytest for Python, ``npm test`` for Node) and
runs it with a timeout. Both async (engine) and sync (agent tool) entry points share the
same detection + result shape.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_MAX_OUT = 6000


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


def run_command_sync(command: list[str], cwd: Path, timeout: float) -> ExecutionResult:
    start = time.monotonic()
    try:
        p = subprocess.run(command, cwd=str(cwd), capture_output=True, timeout=timeout, text=True)
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
