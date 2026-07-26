"""Git operations over a run's workspace (Tier 2: real-repo binding + delivery).

Thin subprocess wrappers — no GitPython dependency. Used to (1) materialize a real
checked-out repository into the run workspace before scheduling, and (2) deliver the
run's changes as a branch + commit (and optionally a PR) at the end.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import tempfile
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
    workspace = workspace.resolve()  # git resolves relative worktree paths against -C
    if _git(["rev-parse", "--git-dir"], workspace).returncode != 0:
        raise ValueError(f"not a git repository: {workspace}")

    # Snapshot any uncommitted work; also creates the first commit in a fresh repo.
    dirty = bool(_git(["status", "--porcelain"], workspace).stdout.strip())
    has_head = _git(["rev-parse", "--verify", "HEAD"], workspace).returncode == 0
    if dirty or not has_head:
        _commit_all(workspace, "ada: pre-worktree snapshot")

    # Keep the worktree container invisible to the repo without touching .gitignore.
    # info/exclude lives in the COMMON git dir; inside a linked worktree ``.git`` is a
    # file, so resolve the real location instead of assuming a ``.git`` directory.
    common = _git(["rev-parse", "--git-common-dir"], workspace).stdout.strip()
    exclude = (workspace / common).resolve() / "info" / "exclude"
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
    workspace = workspace.resolve()  # git resolves relative worktree paths against -C
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
        # MERGE_HEAD lives in the per-worktree git dir (a linked worktree's ``.git``
        # is a file), so resolve it via --git-dir rather than assuming a directory.
        git_dir = _git(["rev-parse", "--git-dir"], workspace).stdout.strip()
        if git_dir and ((workspace / git_dir).resolve() / "MERGE_HEAD").exists():
            _git(["merge", "--abort"], workspace)
        else:  # merge died before recording MERGE_HEAD; restore the tree anyway
            _git(["reset", "--merge"], workspace)
        return {"merged": False, "conflict": True, "files": files}
    commit = _git(["rev-parse", "HEAD"], workspace).stdout.strip()
    return {"merged": True, "conflict": False, "commit": commit}


def worktree_remove(workspace: Path, subtask_id: str) -> None:
    """Best-effort removal of the subtask's worktree and branch; never raises."""
    workspace = workspace.resolve()  # git resolves relative worktree paths against -C
    try:
        wt_path = _worktree_path(workspace, subtask_id)
        _git(["worktree", "remove", "--force", str(wt_path)], workspace)
        if wt_path.exists():  # leftover dir (e.g. worktree already pruned)
            shutil.rmtree(wt_path, ignore_errors=True)
        _git(["worktree", "prune"], workspace)
        _git(["branch", "-D", _branch_for(subtask_id)], workspace)
    except Exception as exc:  # pragma: no cover - cleanup must never propagate
        logger.warning("worktree cleanup failed for %s: %s", subtask_id, exc)


# ---------------------------------------------------------------------------
# Task-level worktrees (a task runs in a worktree OUTSIDE the project checkout)
# and review-flow merge primitives (merge / cherry-pick without touching any
# existing checkout — the user's working tree and branch are never mutated).
# ---------------------------------------------------------------------------


def _task_branch(task_id: str) -> str:
    return f"ada/{task_id}"


def _worktree_entries(repo: Path) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` into [{"path": ..., "branch": ...}]."""
    out = _git(["worktree", "list", "--porcelain"], repo).stdout
    entries: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
        elif line.startswith("worktree "):
            cur = {"path": line[len("worktree "):]}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].removeprefix("refs/heads/")
    if cur:
        entries.append(cur)
    return entries


def _checked_out_branches(repo: Path) -> set[str]:
    """Branch names checked out in ANY worktree of ``repo`` (incl. its own tree)."""
    return {e["branch"] for e in _worktree_entries(repo) if "branch" in e}


def _branch_exists(repo: Path, name: str) -> bool:
    return _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], repo).returncode == 0


def task_worktree_add(repo: Path, wt_path: Path, task_id: str, *, base: str = "") -> dict:
    """Create (or reuse) the task worktree for ``task_id`` at ``wt_path``.

    Branch ``ada/{task_id}`` is created at ``base`` (or the repo's HEAD) and checked
    out into ``wt_path``, which lives OUTSIDE the repo (parents are auto-created).
    Idempotent: if ``wt_path`` is already a registered worktree on that branch, it
    is reused. The repo's own checked-out branch and working tree are NEVER touched
    and nothing is ever committed in the repo — an empty repository (no commits)
    raises ``ValueError("repository has no commits")`` so the caller can decide
    (in-place user repos must never be committed to by the assistant).

    Returns ``{"path": str, "branch": "ada/<task_id>", "created": bool}``.
    """
    repo = repo.resolve()
    wt_path = wt_path.resolve()
    repo = Path(repo)
    if _git(["rev-parse", "--git-dir"], repo).returncode != 0:
        raise ValueError(f"not a git repository: {repo}")
    if _git(["rev-parse", "--verify", "--quiet", "HEAD"], repo).returncode != 0:
        raise ValueError("repository has no commits")

    branch = _task_branch(task_id)
    wt_path = Path(wt_path).resolve()
    for entry in _worktree_entries(repo):
        if Path(entry["path"]).resolve() == wt_path and entry.get("branch") == branch:
            return {"path": str(wt_path), "branch": branch, "created": False}

    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo, branch):  # e.g. re-engaging a task whose worktree was removed
        res = _git(["worktree", "add", str(wt_path), branch], repo)
    else:
        res = _git(["worktree", "add", "-b", branch, str(wt_path), base or "HEAD"], repo)
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {(res.stderr or res.stdout).strip()[:300]}")
    return {"path": str(wt_path), "branch": branch, "created": True}


def task_worktree_remove(repo: Path, wt_path: Path, task_id: str, *, keep_branch: bool = True) -> None:
    """Best-effort removal of a task worktree; never raises.

    The ``ada/{task_id}`` branch survives unless ``keep_branch=False``.
    """
    repo = repo.resolve()
    wt_path = wt_path.resolve()
    try:
        wt = Path(wt_path)
        _git(["worktree", "remove", "--force", str(wt)], repo)
        if wt.exists():  # leftover dir (e.g. worktree already pruned)
            shutil.rmtree(wt, ignore_errors=True)
        _git(["worktree", "prune"], repo)
        if not keep_branch:
            _git(["branch", "-D", _task_branch(task_id)], repo)
    except Exception as exc:  # pragma: no cover - cleanup must never propagate
        logger.warning("task worktree cleanup failed for %s: %s", task_id, exc)


@contextlib.contextmanager
def _temp_worktree(repo: Path, branch: str):
    """Yield a throwaway worktree with ``branch`` checked out; always cleaned up."""
    tmp = Path(tempfile.mkdtemp(prefix="ada-tmp-wt-"))
    wt = tmp / "wt"
    res = _git(["worktree", "add", str(wt), branch], repo)
    if res.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed: {(res.stderr or res.stdout).strip()[:300]}")
    try:
        yield wt
    finally:
        _git(["worktree", "remove", "--force", str(wt)], repo)
        _git(["worktree", "prune"], repo)
        shutil.rmtree(tmp, ignore_errors=True)


def _ensure_branch_at_head(repo: Path, name: str) -> str:
    """Create ``name`` at HEAD if missing. Returns "" on success, error text otherwise."""
    if _branch_exists(repo, name):
        return ""
    res = _git(["branch", name], repo)
    return "" if res.returncode == 0 else (res.stderr or res.stdout).strip()[:300]


def _unmerged_files(wt: Path) -> list[str]:
    out = _git(["diff", "--name-only", "--diff-filter=U"], wt).stdout
    return [f for f in out.splitlines() if f.strip()]


def _op_in_progress(wt: Path, state_file: str) -> bool:
    """True when ``state_file`` (MERGE_HEAD / CHERRY_PICK_HEAD) exists in wt's git dir."""
    git_dir = _git(["rev-parse", "--git-dir"], wt).stdout.strip()
    return bool(git_dir) and ((wt / git_dir).resolve() / state_file).exists()


def merge_branch(repo: Path, src: str, into: str, *, message: str = "") -> dict:
    """Merge ``src`` into ``into`` without touching any existing checkout.

    If ``into`` is checked out anywhere (including the repo's own working tree)
    the merge is refused: ``{"merged": False, "conflict": False, "error":
    "target branch is checked out"}`` — callers route in-place projects to an
    integration branch instead. Otherwise the merge happens with ``--no-ff`` in a
    temporary worktree (creating ``into`` at HEAD if it doesn't exist yet).
    Conflicts are aborted and reported as ``{"merged": False, "conflict": True,
    "files": [...]}``; success returns ``{"merged": True, "conflict": False,
    "commit": sha}``.
    """
    repo = Path(repo)
    if into in _checked_out_branches(repo):
        return {"merged": False, "conflict": False, "error": "target branch is checked out"}
    err = _ensure_branch_at_head(repo, into)
    if err:
        return {"merged": False, "conflict": False, "error": f"cannot create branch {into}: {err}"}
    try:
        with _temp_worktree(repo, into) as wt:
            msg = message or f"ada: merge {src} into {into}"
            res = _git([*_IDENT, "merge", "--no-ff", "-m", msg, src], wt)
            if res.returncode != 0:
                files = _unmerged_files(wt)
                if _op_in_progress(wt, "MERGE_HEAD"):
                    _git(["merge", "--abort"], wt)
                else:
                    _git(["reset", "--merge"], wt)
                if files:
                    return {"merged": False, "conflict": True, "files": files}
                return {"merged": False, "conflict": False,
                        "error": (res.stderr or res.stdout).strip()[:300]}
            commit = _git(["rev-parse", "HEAD"], wt).stdout.strip()
            return {"merged": True, "conflict": False, "commit": commit}
    except RuntimeError as exc:
        return {"merged": False, "conflict": False, "error": str(exc)[:300]}


def cherry_pick_merge(repo: Path, sha: str, into: str) -> dict:
    """Apply ONE commit onto ``into`` (per-subtask acceptance in the review flow).

    Uses ``cherry-pick -m 1`` when ``sha`` is a merge commit (a subtask's merge
    into its task branch) and a plain cherry-pick otherwise. Same refusal (target
    checked out anywhere), temp-worktree, conflict-abort semantics and return
    shape as :func:`merge_branch` (plus ``"files"`` on conflict).
    """
    repo = Path(repo)
    if into in _checked_out_branches(repo):
        return {"merged": False, "conflict": False, "error": "target branch is checked out"}
    parents = _git(["rev-list", "--parents", "-n", "1", sha], repo)
    if parents.returncode != 0:
        return {"merged": False, "conflict": False,
                "error": f"cannot resolve commit {sha}: {(parents.stderr or parents.stdout).strip()[:200]}"}
    is_merge_commit = len(parents.stdout.split()) > 2
    err = _ensure_branch_at_head(repo, into)
    if err:
        return {"merged": False, "conflict": False, "error": f"cannot create branch {into}: {err}"}
    try:
        with _temp_worktree(repo, into) as wt:
            args = ["cherry-pick", *(["-m", "1"] if is_merge_commit else []), sha]
            res = _git([*_IDENT, *args], wt)
            if res.returncode != 0:
                files = _unmerged_files(wt)
                if _op_in_progress(wt, "CHERRY_PICK_HEAD"):
                    _git(["cherry-pick", "--abort"], wt)
                else:
                    _git(["reset", "--merge"], wt)
                if files:
                    return {"merged": False, "conflict": True, "files": files}
                return {"merged": False, "conflict": False,
                        "error": (res.stderr or res.stdout).strip()[:300]}
            commit = _git(["rev-parse", "HEAD"], wt).stdout.strip()
            return {"merged": True, "conflict": False, "commit": commit}
    except RuntimeError as exc:
        return {"merged": False, "conflict": False, "error": str(exc)[:300]}


def cherry_pick_in_checkout(repo: Path, sha: str) -> dict:
    """Apply one commit directly in ``repo``'s working tree (must be clean).

    For checkouts the assistant OWNS (greenfield/clone projects): acceptance advances
    the checked-out branch and its working tree together. Same return shape as
    ``cherry_pick_merge``. Never used on in-place user repos.
    """
    if _git(["status", "--porcelain"], repo).stdout.strip():
        return {"merged": False, "conflict": False, "error": "checkout has uncommitted changes"}
    parents = _git(["rev-list", "--parents", "-n", "1", sha], repo).stdout.split()
    args = ["cherry-pick"] + (["-m", "1"] if len(parents) > 2 else []) + [sha]
    res = _git([*_IDENT, *args], repo)
    if res.returncode != 0:
        files = [f for f in _git(["diff", "--name-only", "--diff-filter=U"], repo)
                 .stdout.splitlines() if f.strip()]
        _git(["cherry-pick", "--abort"], repo)
        return {"merged": False, "conflict": True, "files": files}
    return {"merged": True, "conflict": False,
            "commit": _git(["rev-parse", "HEAD"], repo).stdout.strip()}


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
