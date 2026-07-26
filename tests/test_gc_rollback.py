"""Workspace GC (gc.cleanup_report/cleanup) and rollback of accepted subtasks
(projects.rollback_accept) — real tmp git repos, run rows via RunStore.

Covers the conservative-keep GC policy (terminal+old only, running excluded,
ada/integration never listed, idempotent removal) and both rollback routes:
owned checkout (revert in place) and in-place projects (revert lands on
ada/integration through a temp worktree; the user's checkout is untouched).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ai_dev_assistant import gc as ada_gc
from ai_dev_assistant import projects, vcs
from ai_dev_assistant.config import Settings
from ai_dev_assistant.orchestration.run_store import RunStore

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run([*GIT, *args], cwd=str(cwd), capture_output=True, text=True)
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res.stdout.strip()


def git_try(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*GIT, *args], cwd=str(cwd), capture_output=True, text=True)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace",
    )


def _fingerprint(repo: Path) -> tuple[str, str, str]:
    """(HEAD, branch, porcelain status) — untouched-user-repo assertions."""
    return (git(repo, "rev-parse", "HEAD"),
            git(repo, "branch", "--show-current"),
            git(repo, "status", "--porcelain"))


def _make_task(settings: Settings, slug: str, repo: Path, tid: str, *,
               status: str = "completed", filename: str | None = None,
               content: str | None = None) -> str:
    """Task worktree at workspace/<slug>/worktrees/<tid> with one commit on
    ada/<tid>; run + subtask 's1' recorded in the run store. Returns the sha."""
    ws = (settings.workspace_dir / slug / "worktrees" / tid).resolve()
    vcs.task_worktree_add(repo, ws, tid)
    name = filename or f"{tid}.txt"
    (ws / name).write_text(content if content is not None else f"{tid} content\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", f"ada: subtask s1 ({tid})")
    sha = git(ws, "rev-parse", "HEAD")
    store = RunStore(settings.data_dir / "runs.db")
    try:
        store.start(tid, f"task {tid}", project=slug)
        store.checkpoint_subtask(tid, "s1", status="passed", attempts=1,
                                 result="done", verdict_json=None, merge_commit=sha)
        store.set_status(tid, status)
    finally:
        store.close()
    return sha


def _review(settings: Settings, tid: str, sid: str = "s1") -> dict:
    store = RunStore(settings.data_dir / "runs.db")
    try:
        return store.get_subtask_reviews(tid).get(sid) or {}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Part 1 — GC
# ---------------------------------------------------------------------------

def test_report_lists_only_terminal_and_old(tmp_path):
    settings = _settings(tmp_path)
    slug = projects.create_project(settings, "Gcp")["slug"]
    repo = projects.project_checkout(settings, slug)
    _make_task(settings, slug, repo, "t-done", status="completed")
    _make_task(settings, slug, repo, "t-run", status="running")
    _make_task(settings, slug, repo, "t-interrupted", status="interrupted")
    # a stray worktree dir with no run record must be kept (can't prove terminal)
    (settings.workspace_dir / slug / "worktrees" / "t-stray").mkdir(parents=True)

    report = ada_gc.cleanup_report(settings, slug, keep_days=0)
    assert [w["task_id"] for w in report["worktrees"]] == ["t-done"]
    # a fresh terminal run is younger than the default retention — kept
    assert ada_gc.cleanup_report(settings, slug, keep_days=14)["worktrees"] == []


def test_report_branches_full_acceptance_required_and_integration_never_listed(tmp_path):
    settings = _settings(tmp_path)
    slug = projects.create_project(settings, "Gcb")["slug"]
    repo = projects.project_checkout(settings, slug)
    git(repo, "branch", "ada/integration")  # must never be collectible

    _make_task(settings, slug, repo, "t-acc")
    _make_task(settings, slug, repo, "t-noreview")
    _make_task(settings, slug, repo, "t-rej")
    assert projects.accept_subtask(settings, slug, "t-acc", "s1").get("merged")
    store = RunStore(settings.data_dir / "runs.db")
    try:
        store.set_subtask_review("t-rej", "s1", "rejected", "nope")
    finally:
        store.close()

    report = ada_gc.cleanup_report(settings, slug, keep_days=0)
    branches = [b["branch"] for b in report["branches"]]
    assert branches == ["ada/t-acc"]  # accepted everywhere → collectible
    assert "ada/integration" not in branches
    assert "ada/t-noreview" not in branches and "ada/t-rej" not in branches
    # worktree eligibility is independent of review state (terminal+old suffices)
    assert {w["task_id"] for w in report["worktrees"]} == {"t-acc", "t-noreview", "t-rej"}


def test_report_and_cleanup_safe_on_legacy_layouts(tmp_path):
    settings = _settings(tmp_path)
    # no runs.db, no workspace, unknown slug — empty report, never raises
    assert ada_gc.cleanup_report(settings, "ghost") == {"worktrees": [], "branches": []}
    res = ada_gc.cleanup(settings, "ghost", keep_days=0)
    assert res == {"removed": {"worktrees": [], "branches": []}, "skipped": []}
    # runs.db exists but the project has no checkout / no worktrees dir
    RunStore(settings.data_dir / "runs.db").close()
    assert ada_gc.cleanup_report(settings, "default", keep_days=0) == \
        {"worktrees": [], "branches": []}


def test_cleanup_removes_worktree_and_branch_and_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    slug = projects.create_project(settings, "Gcc")["slug"]
    repo = projects.project_checkout(settings, slug)
    _make_task(settings, slug, repo, "t-old")
    assert projects.accept_subtask(settings, slug, "t-old", "s1").get("merged")
    _make_task(settings, slug, repo, "t-live", status="running")

    res = ada_gc.cleanup(settings, slug, keep_days=0)
    assert [w["task_id"] for w in res["removed"]["worktrees"]] == ["t-old"]
    assert [b["branch"] for b in res["removed"]["branches"]] == ["ada/t-old"]
    assert res["skipped"] == []
    assert not (settings.workspace_dir / slug / "worktrees" / "t-old").exists()
    branches = git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert "ada/t-old" not in branches
    # the running task survives untouched: worktree + branch still there
    assert (settings.workspace_dir / slug / "worktrees" / "t-live").is_dir()
    assert "ada/t-live" in branches

    again = ada_gc.cleanup(settings, slug, keep_days=0)
    assert again["removed"] == {"worktrees": [], "branches": []}

    picky = ada_gc.cleanup(settings, slug, keep_days=0, ids=["nope"])
    assert picky["removed"] == {"worktrees": [], "branches": []}
    assert picky["skipped"] and picky["skipped"][0]["task_id"] == "nope"
    assert "not eligible" in picky["skipped"][0]["reason"]


def test_cleanup_ids_filter_restricts_removal(tmp_path):
    settings = _settings(tmp_path)
    slug = projects.create_project(settings, "Gci")["slug"]
    repo = projects.project_checkout(settings, slug)
    _make_task(settings, slug, repo, "t-a")
    _make_task(settings, slug, repo, "t-b")

    res = ada_gc.cleanup(settings, slug, keep_days=0, ids=["t-a"])
    assert [w["task_id"] for w in res["removed"]["worktrees"]] == ["t-a"]
    assert (settings.workspace_dir / slug / "worktrees" / "t-b").is_dir()


# ---------------------------------------------------------------------------
# Part 2 — rollback of an accepted subtask
# ---------------------------------------------------------------------------

def test_rollback_owned_checkout_reverts_file(tmp_path):
    settings = _settings(tmp_path)
    slug = projects.create_project(settings, "Own")["slug"]
    repo = projects.project_checkout(settings, slug)
    _make_task(settings, slug, repo, "t1", filename="alpha.txt", content="alpha\n")
    acc = projects.accept_subtask(settings, slug, "t1", "s1")
    assert acc.get("merged") and acc.get("commit")
    assert (repo / "alpha.txt").read_text() == "alpha\n"
    assert _review(settings, "t1")["accepted_commit"] == acc["commit"]

    res = projects.rollback_accept(settings, slug, "t1", "s1")
    assert res["ok"] is True and res["reverted"] == acc["commit"]
    assert res["rollback_commit"]
    assert not (repo / "alpha.txt").exists()  # working tree moved back too
    assert git(repo, "status", "--porcelain") == ""
    review = _review(settings, "t1")
    assert review["decision"] == "rolled_back"
    assert review["rollback_commit"] == res["rollback_commit"]
    assert review["accepted_commit"] == acc["commit"]  # history preserved

    # a second rollback has nothing accepted to revert
    res2 = projects.rollback_accept(settings, slug, "t1", "s1")
    assert res2["ok"] is False and "not in an accepted state" in res2["error"]


def test_rollback_in_place_lands_on_integration_user_untouched(tmp_path):
    settings = _settings(tmp_path)
    user = tmp_path / "userrepo"
    user.mkdir()
    git(user, "init", "-q")
    (user / "base.txt").write_text("base\n")
    git(user, "add", "-A")
    git(user, "commit", "-q", "-m", "initial")
    slug = projects.import_project(settings, str(user), name="Inplace")["slug"]
    before = _fingerprint(user)

    _make_task(settings, slug, user, "t1", filename="feat.txt", content="feature\n")
    acc = projects.accept_subtask(settings, slug, "t1", "s1")
    assert acc.get("merged"), acc
    assert git(user, "show", "ada/integration:feat.txt") == "feature"
    assert not (user / "feat.txt").exists()  # user's tree untouched by accept

    res = projects.rollback_accept(settings, slug, "t1", "s1")
    assert res["ok"] is True and res["target"] == "ada/integration"
    # revert landed on ada/integration only; the file is gone from that branch
    assert "feat.txt" not in git(user, "ls-tree", "--name-only", "ada/integration")
    assert git_try(user, "show", "ada/integration:feat.txt").returncode != 0
    # user's checked-out branch, HEAD and working tree: byte-for-byte untouched
    assert _fingerprint(user) == before
    assert _review(settings, "t1")["decision"] == "rolled_back"


def test_rollback_conflict_returns_error_and_leaves_repo_clean(tmp_path):
    settings = _settings(tmp_path)
    slug = projects.create_project(settings, "Cfl")["slug"]
    repo = projects.project_checkout(settings, slug)
    (repo / "file.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")

    _make_task(settings, slug, repo, "t1", filename="file.txt", content="two\n")
    assert projects.accept_subtask(settings, slug, "t1", "s1").get("merged")
    assert (repo / "file.txt").read_text() == "two\n"
    # a later commit on the target rewrites the same content → revert conflicts
    (repo / "file.txt").write_text("three\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "later change")

    res = projects.rollback_accept(settings, slug, "t1", "s1")
    assert res["ok"] is False and res.get("conflict") is True
    assert res.get("error")
    # repo left clean: no leftover revert state, tree unchanged
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "REVERT_HEAD").exists()
    assert not (repo / ".git" / "sequencer").exists()
    assert (repo / "file.txt").read_text() == "three\n"
    assert _review(settings, "t1")["decision"] == "accepted"  # unchanged — not forced


def test_rollback_without_recorded_accept_commit(tmp_path):
    settings = _settings(tmp_path)
    slug = projects.create_project(settings, "Nor")["slug"]
    repo = projects.project_checkout(settings, slug)
    _make_task(settings, slug, repo, "t1")
    res = projects.rollback_accept(settings, slug, "t1", "s1")
    assert res["ok"] is False and "no recorded accept commit" in res["error"]
    assert projects.rollback_accept(settings, "ghost", "t1", "s1")["ok"] is False


# ---------------------------------------------------------------------------
# Run-store bookkeeping (additive migration + upsert semantics)
# ---------------------------------------------------------------------------

def test_review_upsert_preserves_accepted_commit(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        store.set_subtask_review("r1", "s1", "accepted", accepted_commit="abc123")
        row = store.get_subtask_reviews("r1")["s1"]
        assert row["decision"] == "accepted" and row["accepted_commit"] == "abc123"
        # re-deciding without a commit must not wipe the recorded hash
        store.set_subtask_review("r1", "s1", "rejected", "changed my mind")
        row = store.get_subtask_reviews("r1")["s1"]
        assert row["decision"] == "rejected" and row["accepted_commit"] == "abc123"
        store.set_subtask_rollback("r1", "s1", "def456")
        row = store.get_subtask_reviews("r1")["s1"]
        assert row["decision"] == "rolled_back"
        assert row["rollback_commit"] == "def456"
        assert row["accepted_commit"] == "abc123"
    finally:
        store.close()


def test_migration_is_idempotent_on_existing_db(tmp_path):
    path = tmp_path / "runs.db"
    RunStore(path).close()
    store = RunStore(path)  # re-open: ALTER TABLE must be a no-op, not an error
    try:
        store.set_subtask_review("r1", "s1", "accepted", accepted_commit="a1")
        assert store.get_subtask_reviews("r1")["s1"]["accepted_commit"] == "a1"
    finally:
        store.close()
