"""Git operations over a run's workspace (Tier 2: real-repo binding + delivery).

Thin subprocess wrappers — no GitPython dependency. Used to (1) materialize a real
checked-out repository into the run workspace before scheduling, and (2) deliver the
run's changes as a branch + commit (and optionally a PR) at the end.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("ada.vcs")

_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv", "dist", "build")


def _git(args: list[str], cwd: Path, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def materialize(*, dest: Path, repo_url: str = "", repo_path: str = "", repo_ref: str = "") -> dict[str, str]:
    """Populate ``dest`` with a repository to work on.

    - repo_url → clone it.
    - repo_path → copy the working tree (excluding .git/build dirs) and re-init git, so the
      run never mutates the user's real checkout; changes land in the sandbox only.
    Returns {"mode", "ref", "head"}.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if repo_url:
        # clone into a temp sibling then move contents in (dest may already exist/empty)
        res = _git(["clone", "--depth", "1", repo_url, "."], dest)
        if res.returncode != 0:
            raise RuntimeError(f"git clone failed: {res.stderr.strip()[:300]}")
        if repo_ref:
            _git(["fetch", "--depth", "1", "origin", repo_ref], dest)
            _git(["checkout", repo_ref], dest)
        mode = "clone"
    elif repo_path:
        src = Path(repo_path).expanduser().resolve()
        if not src.is_dir():
            raise RuntimeError(f"repo_path does not exist: {src}")
        for child in src.iterdir():
            if child.name in (".git", "__pycache__", ".venv", "node_modules"):
                continue
            target = dest / child.name
            if child.is_dir():
                shutil.copytree(child, target, ignore=_IGNORE, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target)
        _git(["init", "-q"], dest)
        _git(["add", "-A"], dest)
        _git(["-c", "user.email=ada@local", "-c", "user.name=ADA",
              "commit", "-q", "-m", "baseline (copied working tree)"], dest)
        mode = "copy"
    else:
        return {"mode": "greenfield", "ref": "", "head": ""}
    head = _git(["rev-parse", "HEAD"], dest)
    return {"mode": mode, "ref": repo_ref, "head": head.stdout.strip()[:12]}


def ensure_repo(dest: Path) -> None:
    """Make ``dest`` a git repo if it isn't one (greenfield runs still get version control)."""
    if not (dest / ".git").exists():
        _git(["init", "-q"], dest)


def status(dest: Path) -> str:
    return _git(["status", "--short"], dest).stdout.strip()


def diff(dest: Path, *, staged: bool = False) -> str:
    args = ["diff", "--stat"] + (["--cached"] if staged else [])
    return _git(args, dest).stdout.strip()


def finalize(dest: Path, *, branch: str, message: str) -> dict[str, str]:
    """Commit everything in the workspace on a new branch. Returns {"branch","commit"}."""
    ensure_repo(dest)
    _git(["checkout", "-B", branch], dest)
    _git(["add", "-A"], dest)
    res = _git(["-c", "user.email=ada@local", "-c", "user.name=AI Dev Assistant",
                "commit", "-q", "-m", message], dest)
    if res.returncode != 0 and "nothing to commit" not in (res.stdout + res.stderr):
        logger.warning("git commit issue: %s", (res.stderr or res.stdout).strip()[:200])
    commit = _git(["rev-parse", "HEAD"], dest).stdout.strip()[:12]
    return {"branch": branch, "commit": commit}
