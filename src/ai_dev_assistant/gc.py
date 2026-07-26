"""Workspace garbage collection — reclaim finished task worktrees and delivered
ada/* branches.

Task worktrees (``workspace/<slug>/worktrees/<task-id>``) persist after runs by
design: review, files, continue and resume all read them. Over time finished
tasks accumulate; this module reclaims them — dry-run first (:func:`cleanup_report`),
destructive second (:func:`cleanup`).

Conservative by design (the cost of keeping is disk, the cost of deleting is a
lost review/rollback trail):

- only runs the store proves terminal (completed / failed / cancelled) AND older
  than ``keep_days`` are collectible; queued/running/interrupted (resumable) and
  unknown-to-the-store items are always kept;
- ``ada/integration`` (the in-place review target) is NEVER collectible;
- an ``ada/<task-id>`` branch is collectible only when every recorded subtask of
  its run was accepted in review — any rejected, rolled-back or undecided
  subtask keeps the branch;
- legacy layouts (missing worktrees dir, no runs.db, project without a checkout)
  yield an empty report and never raise.

The safety contract for in-place imported repos is honoured: only ``ada/*``
branch refs are ever deleted, never the user's branches, working tree or
checked-out branch.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .projects import project_checkout

logger = logging.getLogger("ada.gc")

# Statuses that make a run's artifacts collectible. Deliberately narrower than the
# run store's full terminal set: "interrupted" runs are resumable mid-plan and
# "partial"/"over_budget" runs may still be re-engaged — err on the side of keeping.
_COLLECTIBLE_STATUSES = {"completed", "failed", "cancelled"}

_PROTECTED_BRANCHES = {"ada/integration"}


def _git(args: list[str], cwd: Path, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _open_store(settings: Settings):
    """The run store, or None when runs.db doesn't exist yet (legacy layout —
    without run records nothing can be proven terminal, so nothing is collected)."""
    path = settings.data_dir / "runs.db"
    if not path.exists():
        return None
    from .orchestration.run_store import RunStore

    return RunStore(path)


def _age_days(row: dict[str, Any], now: float, fallback: Path | None = None) -> float | None:
    """Days since the run ended (or started); ``None`` when age can't be proven."""
    ts = row.get("ended_at") or row.get("created_at") or 0.0
    if not ts and fallback is not None:
        try:
            ts = fallback.stat().st_mtime
        except OSError:
            ts = 0.0
    if not ts:
        return None
    return (now - float(ts)) / 86400.0


def _fully_accepted(store: Any, task_id: str) -> bool:
    """True only when the run has recorded subtasks and EVERY one was accepted."""
    try:
        states = store.get_subtask_states(task_id) or {}
        reviews = store.get_subtask_reviews(task_id) or {}
    except Exception:  # noqa: BLE001 - err on the side of keeping
        return False
    if not states:
        return False
    return all((reviews.get(sid) or {}).get("decision") == "accepted" for sid in states)


def _project_repo(settings: Settings, slug: str) -> Path | None:
    """The project's checkout when it is a usable git repo, else None."""
    root = project_checkout(settings, slug)
    if root is None or not root.is_dir():
        return None
    if _git(["rev-parse", "--git-dir"], root).returncode != 0:
        return None
    return root.resolve()


def cleanup_report(settings: Settings, slug: str, keep_days: int = 14) -> dict[str, Any]:
    """Dry-run listing of collectible items for ``slug``. Never mutates anything.

    Returns ``{"worktrees": [{"task_id", "path", "status", "age_days"}, ...],
    "branches": [{"task_id", "branch", "status", "age_days"}, ...]}``.

    Worktrees: dirs under ``workspace/<slug>/worktrees/*`` whose run is terminal
    (completed/failed/cancelled) and older than ``keep_days``. Branches:
    ``ada/<task-id>`` branches whose run is additionally fully accepted in
    review. ``ada/integration`` is never listed. Safe on legacy layouts:
    missing dirs / no runs.db → empty report, never raises.
    """
    report: dict[str, Any] = {"worktrees": [], "branches": []}
    store = _open_store(settings)
    if store is None:
        return report
    now = time.time()
    try:
        wt_dir = settings.workspace_dir / slug / "worktrees"
        try:
            entries = sorted(p for p in wt_dir.iterdir() if p.is_dir()) if wt_dir.is_dir() else []
        except OSError:
            entries = []
        for path in entries:
            task_id = path.name
            try:
                row = store.get(task_id)
            except Exception:  # noqa: BLE001
                row = None
            if row is None:  # no run record — can't prove terminal, keep
                continue
            status = str(row.get("status") or "")
            if status not in _COLLECTIBLE_STATUSES:
                continue
            age = _age_days(row, now, fallback=path)
            if age is None or age < keep_days:
                continue
            report["worktrees"].append({
                "task_id": task_id, "path": str(path.resolve()),
                "status": status, "age_days": round(age, 1),
            })

        repo = _project_repo(settings, slug)
        if repo is not None:
            res = _git(["for-each-ref", "--format=%(refname:short)", "refs/heads/ada/"], repo)
            branches = [b.strip() for b in res.stdout.splitlines() if b.strip()] \
                if res.returncode == 0 else []
            for branch in branches:
                if branch in _PROTECTED_BRANCHES:
                    continue
                task_id = branch.removeprefix("ada/")
                try:
                    row = store.get(task_id)
                except Exception:  # noqa: BLE001
                    row = None
                if row is None:  # not a task branch we can vouch for — keep
                    continue
                status = str(row.get("status") or "")
                if status not in _COLLECTIBLE_STATUSES:  # queued/running/… — keep
                    continue
                age = _age_days(row, now)
                if age is None or age < keep_days:
                    continue
                if not _fully_accepted(store, task_id):
                    continue
                report["branches"].append({
                    "task_id": task_id, "branch": branch,
                    "status": status, "age_days": round(age, 1),
                })
    finally:
        store.close()
    return report


def cleanup(settings: Settings, slug: str, keep_days: int = 14,
            ids: list[str] | None = None) -> dict[str, Any]:
    """Remove collectible worktrees and branches (the destructive half of GC).

    Eligibility is exactly :func:`cleanup_report` — anything the report keeps is
    kept here too. ``ids`` restricts removal to those task ids; requested ids
    that are not eligible are skipped with a reason. Worktrees are removed via
    the vcs primitives (worktree remove + prune, branch kept); branches via
    ``git branch -D`` in the project checkout (a branch still checked out in a
    live worktree fails safely and is reported as skipped). Idempotent: a
    second call finds nothing to remove. Returns ``{"removed": {"worktrees":
    [...], "branches": [...]}, "skipped": [{"task_id", "reason"}, ...]}``.
    """
    from . import vcs

    report = cleanup_report(settings, slug, keep_days=keep_days)
    wanted = {str(i) for i in ids} if ids else None
    removed: dict[str, list[dict[str, Any]]] = {"worktrees": [], "branches": []}
    skipped: list[dict[str, Any]] = []

    eligible_wt = {i["task_id"]: i for i in report["worktrees"]}
    eligible_br = {i["task_id"]: i for i in report["branches"]}
    if wanted is not None:
        for tid in sorted(wanted - (set(eligible_wt) | set(eligible_br))):
            skipped.append({"task_id": tid,
                            "reason": "not eligible for cleanup (kept by policy or already removed)"})

    repo = _project_repo(settings, slug)

    # Worktrees first: removing a task's worktree unregisters it, which is what
    # lets a same-pass branch deletion succeed (branch -D refuses live worktrees).
    for tid, item in eligible_wt.items():
        if wanted is not None and tid not in wanted:
            continue
        path = Path(item["path"])
        try:
            if repo is not None:
                vcs.task_worktree_remove(repo, path, tid, keep_branch=True)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            removed["worktrees"].append(item)
        except Exception as exc:  # noqa: BLE001 - report, keep going
            skipped.append({"task_id": tid, "reason": f"worktree removal failed: {exc}"})

    for tid, item in eligible_br.items():
        if wanted is not None and tid not in wanted:
            continue
        if repo is None:
            skipped.append({"task_id": tid, "reason": "project has no git checkout"})
            continue
        if item["branch"] in _PROTECTED_BRANCHES:  # belt-and-braces
            skipped.append({"task_id": tid, "reason": "protected branch"})
            continue
        try:
            res = _git(["branch", "-D", item["branch"]], repo)
        except (OSError, subprocess.SubprocessError) as exc:
            skipped.append({"task_id": tid, "reason": f"git branch -D failed: {exc}"})
            continue
        if res.returncode == 0:
            removed["branches"].append(item)
        else:
            skipped.append({"task_id": tid,
                            "reason": (res.stderr or res.stdout).strip()[:200] or "git branch -D failed"})
    return {"removed": removed, "skipped": skipped}
