"""Run generated code/tests in a workspace to get objective verification.

Detects a test command in the workspace (pytest, ``npm test``, ``go test``,
``cargo test``, Maven/Gradle, rspec/rake) and runs it with a timeout. Both async
(engine) and sync (agent tool) entry points share the same detection + result shape.

Isolation comes in three tiers. The engine selects one per run via
``configure_sandbox()`` from ``Settings`` (``sandbox`` / ``sandbox_image`` /
``sandbox_allow_network``); until that is called the module falls back to the
``ADA_SANDBOX`` / ``ADA_SANDBOX_IMAGE`` / ``ADA_SANDBOX_NET`` env vars, so bare
callers behave exactly as before (the ``sandbox: bool`` parameter stays the
on/off switch):

- ``subprocess`` (default): scrubbed environment (no API keys leak), POSIX rlimits
  (CPU/address-space, plus process count on Linux), and a new process group that is
  killed as a whole on timeout. This is *not* filesystem or network isolation — a
  sandboxed command can still read world-readable files and reach the network; it just
  cannot see the parent's secrets or outlive its timeout.
- ``bwrap``: additionally wraps the command in bubblewrap (Linux) — read-only system
  dirs, the workspace bound read-write, a private ``/tmp``, and no network unless
  allowed. Real filesystem + network containment.
- ``container``: runs the command in a throwaway Docker container
  (``--network=none`` unless network is allowed) with only the workspace mounted;
  image is configurable (python-ish commands default to ``python:3.12-slim``).

If the chosen backend's binary is missing, execution falls back to the ``subprocess``
tier with a one-time warning — the guarantee degrades, it never blocks the run.

Cancellation (R5): callers may pass ``tag=`` to register the child's process group in a
module-level registry; ``terminate_tag(tag)`` SIGKILLs every group registered under it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("ada.exec")

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
    """Return a preexec_fn that sets CPU/address-space/process rlimits (POSIX only).

    The new session/process group comes from ``start_new_session=True`` at the spawn
    site (calling ``os.setsid()`` here too would fail — the child is already a leader).
    """
    if os.name != "posix":
        return None

    def _apply() -> None:  # pragma: no cover - exercised only in subprocesses
        import resource

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
        # A fork bomb survives RLIMIT_AS; cap process count. Linux only — on darwin
        # NPROC is per-user, so a low cap would break the host session.
        if sys.platform.startswith("linux"):
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
            except (ValueError, OSError):
                pass

    return _apply


def _kill_process_group(pid: int) -> None:
    """SIGKILL the whole process group so grandchildren can't outlive a timeout."""
    if os.name != "posix":
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _spawn_kwargs(sandbox: bool, cpu_seconds: int, mem_mb: int) -> dict:
    """Shared spawn options: own session (group-kill on timeout) + optional scrub/rlimits."""
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    if sandbox:
        kwargs["env"] = scrubbed_env()
        preexec = _rlimit_preexec(cpu_seconds, mem_mb)
        if preexec is not None:
            kwargs["preexec_fn"] = preexec
    return kwargs


# ---------------------------------------------------------------------------
# Isolation backends (S5). configure_sandbox() (called by the engine once per
# run, from Settings) selects the tier; the sandbox: bool param on
# run_command(_sync) stays the on/off switch. Until configured, the ADA_SANDBOX*
# env vars apply unchanged. Missing binaries fall back to the subprocess tier
# with a one-time warning.
# ---------------------------------------------------------------------------

_FALLBACK_WARNED: set[str] = set()
_fallback_lock = threading.Lock()

_PYTHONISH = ("python", "python3", "pytest", "pip", "pip3")

# Settings-driven sandbox selection — a plain module global set once per run
# (same pattern as _FALLBACK_WARNED). None = never configured, use the env vars.
_SANDBOX_CONFIG: dict | None = None


def configure_sandbox(backend: str, image: str = "", allow_network: bool = False) -> None:
    """Bind the isolation backend/image/network policy for subsequent commands.

    The engine calls this at run start from its Settings; once called, these
    values take precedence over the ADA_SANDBOX / ADA_SANDBOX_IMAGE /
    ADA_SANDBOX_NET environment variables for the rest of the process."""
    global _SANDBOX_CONFIG
    _SANDBOX_CONFIG = {
        "backend": (backend or "subprocess").strip().lower(),
        "image": image or "",
        "allow_network": bool(allow_network),
    }


def _sandbox_mode() -> str:
    if _SANDBOX_CONFIG is not None:
        return _SANDBOX_CONFIG["backend"]
    return (os.environ.get("ADA_SANDBOX") or "subprocess").strip().lower()


def _sandbox_image() -> str:
    if _SANDBOX_CONFIG is not None:
        return _SANDBOX_CONFIG["image"]
    return os.environ.get("ADA_SANDBOX_IMAGE") or ""


def _sandbox_net_allowed() -> bool:
    if _SANDBOX_CONFIG is not None:
        return _SANDBOX_CONFIG["allow_network"]
    return os.environ.get("ADA_SANDBOX_NET") == "1"


def _warn_fallback_once(backend: str, reason: str) -> None:
    with _fallback_lock:
        if backend in _FALLBACK_WARNED:
            return
        _FALLBACK_WARNED.add(backend)
    logger.warning("ADA_SANDBOX=%s requested but %s; falling back to subprocess hardening",
                   backend, reason)


def _looks_pythonish(command: list[str]) -> bool:
    if not command:
        return False
    base = Path(command[0]).name
    return base in _PYTHONISH or base.startswith("python")


def _wrap_bwrap(command: list[str], cwd: Path) -> list[str]:
    wrapped = ["bwrap", "--die-with-parent", "--proc", "/proc", "--dev", "/dev",
               "--tmpfs", "/tmp"]
    for sysdir in ("/usr", "/lib", "/lib64", "/lib32", "/bin", "/sbin", "/etc"):
        if os.path.exists(sysdir):
            wrapped += ["--ro-bind", sysdir, sysdir]
    wrapped += ["--bind", str(cwd), str(cwd), "--chdir", str(cwd)]
    if not _sandbox_net_allowed():
        wrapped += ["--unshare-net"]
    return wrapped + list(command)


def _wrap_container(command: list[str], cwd: Path) -> list[str]:
    pythonish = _looks_pythonish(command)
    image = _sandbox_image() or (
        "python:3.12-slim" if pythonish else "debian:stable-slim")
    wrapped = ["docker", "run", "--rm", "-v", f"{cwd}:/w", "-w", "/w"]
    if not _sandbox_net_allowed():
        wrapped += ["--network=none"]
    inner = list(command)
    if pythonish and Path(inner[0]).name.startswith("python"):
        inner[0] = "python"  # the host interpreter path does not exist in the image
    return wrapped + [image] + inner


def _apply_sandbox_backend(command: list[str], cwd: Path) -> list[str]:
    """Rewrite ``command`` for the configured isolation backend, if any.

    Returns the command unchanged for the default ``subprocess`` tier (and the
    settings choice ``none`` — the on/off decision is the caller's), an unknown
    mode, or when the chosen backend's binary is missing (one-time warning).
    """
    mode = _sandbox_mode()
    if mode in ("", "subprocess", "none"):
        return command
    if mode == "bwrap":
        if shutil.which("bwrap") is None:
            _warn_fallback_once("bwrap", "bwrap is not on PATH")
            return command
        return _wrap_bwrap(command, cwd)
    if mode == "container":
        if shutil.which("docker") is None:
            _warn_fallback_once("container", "docker is not on PATH")
            return command
        return _wrap_container(command, cwd)
    _warn_fallback_once(mode, "it is not a known backend (subprocess|bwrap|container)")
    return command


# ---------------------------------------------------------------------------
# Process registry (R5): tag -> process-group ids of live children, so the engine
# can hard-cancel everything a run spawned via terminate_tag(run_id).
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_process_registry: dict[str, set[int]] = {}


def _register_pgid(tag: str, pid: int) -> None:
    if os.name != "posix":
        return
    with _registry_lock:
        _process_registry.setdefault(tag, set()).add(pid)


def _unregister_pgid(tag: str, pid: int) -> None:
    if os.name != "posix":
        return
    with _registry_lock:
        pgids = _process_registry.get(tag)
        if pgids is not None:
            pgids.discard(pid)
            if not pgids:
                _process_registry.pop(tag, None)


def terminate_tag(tag: str) -> int:
    """SIGKILL every process group registered under ``tag``; returns groups killed.

    The engine calls this with the run id on cancellation, so children spawned by
    run_command/run_workspace_tests do not outlive a cancelled run.
    """
    with _registry_lock:
        pgids = _process_registry.pop(tag, set())
    killed = 0
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return killed


def detect_test_command(workspace: Path, paths: list[str] | None = None) -> list[str] | None:
    """Best-effort detection of how to test the project in ``workspace``.

    Precedence: pytest, ``npm test``, then Go, Rust, Java (Maven/Gradle), Ruby.
    ``paths`` (workspace-relative test files) narrows the run for pytest and Go
    (mapped to package dirs); the other runners have no portable file-selection so
    paths are ignored there.
    """
    if not workspace.exists():
        return None
    if any(workspace.rglob("test_*.py")) or any(workspace.rglob("*_test.py")):
        cmd = [sys.executable, "-m", "pytest", "-q"]
        if paths:
            cmd += [str(p) for p in paths]
        return cmd
    pkg = workspace / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text())
            if isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
                return ["npm", "test", "--silent"]
        except Exception:
            pass
    if (workspace / "go.mod").is_file():
        cmd = ["go", "test"]
        if paths:
            pkgs = sorted({_go_package(p) for p in paths})
            return cmd + pkgs
        return cmd + ["./..."]
    if (workspace / "Cargo.toml").is_file():
        return ["cargo", "test", "-q"]
    if (workspace / "mvnw").is_file() or (workspace / "pom.xml").is_file():
        return ["mvn", "-q", "test"]
    if (workspace / "gradlew").is_file():
        return ["./gradlew", "test"]
    if (workspace / "Rakefile").is_file():
        if (workspace / "spec").is_dir():
            return ["bundle", "exec", "rspec"]
        return ["rake", "test"]
    return None


def _go_package(path: str) -> str:
    """Map a changed file to the Go package dir ``go test`` accepts."""
    parent = Path(path).parent.as_posix()
    return "." if parent == "." else f"./{parent}"


async def run_command(command: list[str], cwd: Path, timeout: float, *,
                      sandbox: bool = False, cpu_seconds: int = 60, mem_mb: int = 1024,
                      tag: str | None = None) -> ExecutionResult:
    start = time.monotonic()
    actual = _apply_sandbox_backend(command, cwd) if sandbox else command
    try:
        proc = await asyncio.create_subprocess_exec(
            *actual, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **_spawn_kwargs(sandbox, cpu_seconds, mem_mb),
        )
    except FileNotFoundError as exc:
        return ExecutionResult(" ".join(command), -1, "", f"command not found: {exc}",
                               time.monotonic() - start, False)
    if tag:
        _register_pgid(tag, proc.pid)
    timed_out = False
    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_process_group(proc.pid)
            try:
                proc.kill()  # belt-and-braces if the group kill raced
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
    finally:
        if tag:
            _unregister_pgid(tag, proc.pid)
    return ExecutionResult(
        command=" ".join(command),
        return_code=proc.returncode if proc.returncode is not None else -1,
        stdout=_trim(out.decode(errors="replace")),
        stderr=_trim(err.decode(errors="replace")),
        duration=time.monotonic() - start,
        timed_out=timed_out,
    )


def run_command_sync(command: list[str], cwd: Path, timeout: float, *,
                     sandbox: bool = False, cpu_seconds: int = 60, mem_mb: int = 1024,
                     tag: str | None = None) -> ExecutionResult:
    start = time.monotonic()
    actual = _apply_sandbox_backend(command, cwd) if sandbox else command
    try:
        proc = subprocess.Popen(actual, cwd=str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                **_spawn_kwargs(sandbox, cpu_seconds, mem_mb))
    except FileNotFoundError as exc:
        return ExecutionResult(" ".join(command), -1, "", f"command not found: {exc}",
                               time.monotonic() - start, False)
    if tag:
        _register_pgid(tag, proc.pid)
    try:
        out, err = proc.communicate(timeout=timeout)
        return ExecutionResult(" ".join(command), proc.returncode, _trim(out), _trim(err),
                               time.monotonic() - start, False)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc.pid)
        proc.kill()  # belt-and-braces if the group kill raced
        out, err = proc.communicate()
        return ExecutionResult(" ".join(command), -1, _trim(out or ""), _trim(err or ""),
                               time.monotonic() - start, True)
    finally:
        if tag:
            _unregister_pgid(tag, proc.pid)


async def run_workspace_tests(workspace: Path, timeout: float, *,
                              paths: list[str] | None = None, sandbox: bool = True,
                              cpu_seconds: int = 60, mem_mb: int = 1024,
                              tag: str | None = None) -> ExecutionResult | None:
    cmd = detect_test_command(workspace, paths)
    if cmd is None:
        return None
    return await run_command(cmd, workspace, timeout,
                             sandbox=sandbox, cpu_seconds=cpu_seconds, mem_mb=mem_mb, tag=tag)


def run_workspace_tests_sync(workspace: Path, timeout: float, *,
                             paths: list[str] | None = None, sandbox: bool = True,
                             cpu_seconds: int = 60, mem_mb: int = 1024,
                             tag: str | None = None) -> ExecutionResult | None:
    cmd = detect_test_command(workspace, paths)
    if cmd is None:
        return None
    return run_command_sync(cmd, workspace, timeout,
                            sandbox=sandbox, cpu_seconds=cpu_seconds, mem_mb=mem_mb, tag=tag)
