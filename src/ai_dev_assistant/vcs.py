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


# ---------------------------------------------------------------------------
# Per-subtask worktree isolation (parallel subtasks each get their own checkout)
# ---------------------------------------------------------------------------

_WORKTREES_DIR = ".ada_worktrees"
_IDENT = ["-c", "user.email=ada@local", "-c", "user.name=AI Dev Assistant"]


def _branch_for(subtask_id: str) -> str:
    return f"ada/wt-{subtask_id}"


def _worktree_path(workspace: Path, subtask_id: str) -> Path:
    return workspace / _WORKTREES_DIR / subtask_id


def _commit_all(cwd: Path, message: str) -> None:
    """Stage everything and commit; a no-op when there is nothing to commit."""
    _git(["add", "-A"], cwd)
    res = _git([*_IDENT, "commit", "-q", "-m", message], cwd)
    if res.returncode != 0 and "nothing to commit" not in (res.stdout + res.stderr):
        logger.warning("git commit issue: %s", (res.stderr or res.stdout).strip()[:200])


def worktree_add(workspace: Path, subtask_id: str) -> Path:
    """Create an isolated git worktree for ``subtask_id`` under the workspace.

    The workspace must already be a git repository (raises ValueError otherwise).
    A dirty workspace is snapshot-committed first so the worktree branches from a
    complete HEAD. The worktree lives at ``workspace/.ada_worktrees/{subtask_id}``
    on branch ``ada/wt-{subtask_id}``; the container dir is excluded from status
    via ``.git/info/exclude`` (not .gitignore, so the repo's files are untouched).
    """
    if _git(["rev-parse", "--git-dir"], workspace).returncode != 0:
        raise ValueError(f"not a git repository: {workspace}")

    # Snapshot any uncommitted work; also creates the first commit in a fresh repo.
    dirty = bool(_git(["status", "--porcelain"], workspace).stdout.strip())
    has_head = _git(["rev-parse", "--verify", "HEAD"], workspace).returncode == 0
    if dirty or not has_head:
        _commit_all(workspace, "ada: pre-worktree snapshot")

    # Keep the worktree container invisible to the repo without touching .gitignore.
    exclude = workspace / ".git" / "info" / "exclude"
    line = f"{_WORKTREES_DIR}/"
    try:
        existing = exclude.read_text() if exclude.exists() else ""
        if line not in existing.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            exclude.write_text(existing + sep + line + "\n")
    except OSError as exc:  # pragma: no cover - non-fatal
        logger.warning("could not update .git/info/exclude: %s", exc)

    wt_path = _worktree_path(workspace, subtask_id)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    res = _git(["worktree", "add", "-b", _branch_for(subtask_id), str(wt_path), "HEAD"], workspace)
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {(res.stderr or res.stdout).strip()[:300]}")
    return wt_path


def worktree_merge(workspace: Path, subtask_id: str, *, message: str) -> dict:
    """Commit the subtask's worktree and merge its branch into the main workspace.

    Returns {"merged": True, "conflict": False, "commit": sha} on success, or
    {"merged": False, "conflict": True, "files": [...]} on conflict — in which
    case the merge is aborted so the main workspace is left clean.
    """
    wt_path = _worktree_path(workspace, subtask_id)
    _commit_all(wt_path, message)

    # A dirty main workspace would make the merge fail spuriously; snapshot it.
    if _git(["status", "--porcelain"], workspace).stdout.strip():
        _commit_all(workspace, "ada: pre-merge snapshot")

    res = _git([*_IDENT, "merge", "--no-ff", "-m", message, _branch_for(subtask_id)], workspace)
    if res.returncode != 0:
        files = [
            f for f in _git(["diff", "--name-only", "--diff-filter=U"], workspace).stdout.splitlines()
            if f.strip()
        ]
        if (workspace / ".git" / "MERGE_HEAD").exists():
            _git(["merge", "--abort"], workspace)
        else:  # merge died before recording MERGE_HEAD; restore the tree anyway
            _git(["reset", "--merge"], workspace)
        return {"merged": False, "conflict": True, "files": files}
    commit = _git(["rev-parse", "HEAD"], workspace).stdout.strip()
    return {"merged": True, "conflict": False, "commit": commit}


def worktree_remove(workspace: Path, subtask_id: str) -> None:
    """Best-effort removal of the subtask's worktree and branch; never raises."""
    try:
        wt_path = _worktree_path(workspace, subtask_id)
        _git(["worktree", "remove", "--force", str(wt_path)], workspace)
        if wt_path.exists():  # leftover dir (e.g. worktree already pruned)
            shutil.rmtree(wt_path, ignore_errors=True)
        _git(["worktree", "prune"], workspace)
        _git(["branch", "-D", _branch_for(subtask_id)], workspace)
    except Exception as exc:  # pragma: no cover - cleanup must never propagate
        logger.warning("worktree cleanup failed for %s: %s", subtask_id, exc)


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
