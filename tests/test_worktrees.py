"""Per-subtask git worktree isolation primitives (vcs.worktree_add/merge/remove).

All repos live in tmp_path; git identity is pinned via environment so the tests
work in CI containers with no global git config.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ai_dev_assistant import vcs

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=_GIT_ENV, capture_output=True, text=True, check=False
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _git(["-c", "init.defaultBranch=main", "init", "-q"], ws).returncode == 0
    _git(["config", "user.name", "Test"], ws)
    _git(["config", "user.email", "test@example.com"], ws)
    (ws / "base.txt").write_text("line one\n")
    _git(["add", "-A"], ws)
    assert _git(["commit", "-q", "-m", "baseline"], ws).returncode == 0
    return ws


def _assert_clean(ws: Path) -> None:
    assert _git(["status", "--porcelain"], ws).stdout.strip() == ""
    assert not (ws / ".git" / "MERGE_HEAD").exists()


def test_add_merge_round_trip(workspace: Path) -> None:
    wt = vcs.worktree_add(workspace, "s1")
    assert wt == workspace / ".ada_worktrees" / "s1"
    assert wt.is_dir()
    (wt / "feature.txt").write_text("from subtask s1\n")

    result = vcs.worktree_merge(workspace, "s1", message="s1: add feature")
    assert result["merged"] is True
    assert result["conflict"] is False
    assert result["commit"]
    assert (workspace / "feature.txt").read_text() == "from subtask s1\n"
    _assert_clean(workspace)


def test_two_worktrees_different_files_both_merge(workspace: Path) -> None:
    wt_a = vcs.worktree_add(workspace, "a")
    wt_b = vcs.worktree_add(workspace, "b")
    (wt_a / "alpha.txt").write_text("alpha\n")
    (wt_b / "beta.txt").write_text("beta\n")

    res_a = vcs.worktree_merge(workspace, "a", message="a: alpha")
    res_b = vcs.worktree_merge(workspace, "b", message="b: beta")
    assert res_a["merged"] is True and res_b["merged"] is True
    assert (workspace / "alpha.txt").exists()
    assert (workspace / "beta.txt").exists()
    _assert_clean(workspace)


def test_same_line_edit_conflicts_and_workspace_stays_clean(workspace: Path) -> None:
    wt_a = vcs.worktree_add(workspace, "a")
    wt_b = vcs.worktree_add(workspace, "b")
    (wt_a / "base.txt").write_text("line one from a\n")
    (wt_b / "base.txt").write_text("line one from b\n")

    res_a = vcs.worktree_merge(workspace, "a", message="a: edit base")
    assert res_a["merged"] is True

    res_b = vcs.worktree_merge(workspace, "b", message="b: edit base")
    assert res_b["merged"] is False
    assert res_b["conflict"] is True
    assert "base.txt" in res_b["files"]
    # Main workspace must be left clean and not mid-merge.
    _assert_clean(workspace)
    # First merge's content survives the aborted second merge.
    assert (workspace / "base.txt").read_text() == "line one from a\n"


def test_add_on_non_git_dir_raises(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        vcs.worktree_add(plain, "x")


def test_dirty_workspace_gets_snapshot_commit(workspace: Path) -> None:
    (workspace / "uncommitted.txt").write_text("dirty\n")
    wt = vcs.worktree_add(workspace, "s1")

    _assert_clean(workspace)
    last_msg = _git(["log", "-1", "--pretty=%s"], workspace).stdout.strip()
    assert last_msg == "ada: pre-worktree snapshot"
    # The snapshot is part of HEAD, so the worktree sees the dirty file too.
    assert (wt / "uncommitted.txt").read_text() == "dirty\n"


def test_worktrees_dir_excluded_via_info_exclude(workspace: Path) -> None:
    vcs.worktree_add(workspace, "s1")
    exclude = (workspace / ".git" / "info" / "exclude").read_text()
    assert ".ada_worktrees/" in exclude.splitlines()
    assert not (workspace / ".gitignore").exists()
    _assert_clean(workspace)


def test_remove_is_idempotent(workspace: Path) -> None:
    wt = vcs.worktree_add(workspace, "s1")
    assert wt.is_dir()

    vcs.worktree_remove(workspace, "s1")
    assert not wt.exists()
    branches = _git(["branch", "--list", "ada/wt-s1"], workspace).stdout.strip()
    assert branches == ""

    # Second (and third) removal of an already-gone worktree must not raise.
    vcs.worktree_remove(workspace, "s1")
    vcs.worktree_remove(workspace, "never-existed")


# ---------------------------------------------------------------------------
# Task-level worktrees (task_worktree_add/remove) + review-flow merge primitives
# ---------------------------------------------------------------------------


def _head(repo: Path, ref: str = "HEAD") -> str:
    return _git(["rev-parse", ref], repo).stdout.strip()


def _current_branch(repo: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()


def _commit_in(wt: Path, fname: str, content: str, msg: str) -> str:
    """Commit one file inside a worktree (test-side, via plain git) and return the sha."""
    (wt / fname).write_text(content)
    _git(["add", "-A"], wt)
    assert _git(["commit", "-q", "-m", msg], wt).returncode == 0
    return _head(wt)


def test_task_worktree_add_outside_repo_and_repo_untouched(workspace: Path, tmp_path: Path) -> None:
    before_head = _head(workspace)
    before_branch = _current_branch(workspace)
    wt_path = tmp_path / "wts" / "t1"

    info = vcs.task_worktree_add(workspace, wt_path, "t1")
    assert info["branch"] == "ada/t1"
    assert info["created"] is True
    assert Path(info["path"]) == wt_path.resolve()
    # worktree materialized outside the repo, seeded from HEAD
    assert workspace.resolve() not in wt_path.resolve().parents
    assert (wt_path / "base.txt").read_text() == "line one\n"
    assert _head(workspace, "ada/t1") == before_head
    # repo checkout untouched: same HEAD, same branch, clean tree
    assert _head(workspace) == before_head
    assert _current_branch(workspace) == before_branch
    assert _git(["status", "--porcelain"], workspace).stdout.strip() == ""


def test_task_worktree_add_is_idempotent(workspace: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wts" / "t1"
    first = vcs.task_worktree_add(workspace, wt_path, "t1")
    second = vcs.task_worktree_add(workspace, wt_path, "t1")
    assert first["created"] is True
    assert second["created"] is False
    assert second["path"] == first["path"]
    assert second["branch"] == "ada/t1"
    listed = _git(["worktree", "list", "--porcelain"], workspace).stdout
    assert listed.count(f"worktree {wt_path.resolve()}") == 1


def test_task_worktree_add_from_base(workspace: Path, tmp_path: Path) -> None:
    first = _head(workspace)
    _commit_in(workspace, "base.txt", "line two\n", "second commit")
    info = vcs.task_worktree_add(workspace, tmp_path / "wts" / "tb", "tb", base=first)
    assert info["created"] is True
    assert _head(workspace, "ada/tb") == first
    assert (tmp_path / "wts" / "tb" / "base.txt").read_text() == "line one\n"


def test_task_worktree_add_empty_repo_raises(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    assert _git(["-c", "init.defaultBranch=main", "init", "-q"], repo).returncode == 0
    with pytest.raises(ValueError, match="no commits"):
        vcs.task_worktree_add(repo, tmp_path / "wts" / "t1", "t1")
    # the (possibly in-place user) repo must not have been committed to
    assert _git(["rev-parse", "--verify", "HEAD"], repo).returncode != 0
    assert not (tmp_path / "wts" / "t1").exists()


def test_nested_subtask_worktrees_inside_task_worktree(workspace: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wts" / "t1"
    vcs.task_worktree_add(workspace, wt_path, "t1")
    assert (wt_path / ".git").is_file()  # linked worktree: .git is a file, not a dir

    sub = vcs.worktree_add(wt_path, "s1")
    assert sub == wt_path / ".ada_worktrees" / "s1"
    (sub / "feature.txt").write_text("nested\n")
    res = vcs.worktree_merge(wt_path, "s1", message="s1: nested feature")
    assert res["merged"] is True
    assert (wt_path / "feature.txt").read_text() == "nested\n"
    # the exclude entry lands in the COMMON git dir (the repo's .git/info/exclude)
    exclude = (workspace / ".git" / "info" / "exclude").read_text()
    assert ".ada_worktrees/" in exclude.splitlines()
    # main repo untouched by the nested flow
    assert not (workspace / "feature.txt").exists()
    assert _git(["status", "--porcelain"], workspace).stdout.strip() == ""
    vcs.worktree_remove(wt_path, "s1")
    assert not sub.exists()


def test_nested_conflict_aborts_clean_in_linked_worktree(workspace: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wts" / "t2"
    vcs.task_worktree_add(workspace, wt_path, "t2")
    wt_a = vcs.worktree_add(wt_path, "a")
    wt_b = vcs.worktree_add(wt_path, "b")
    (wt_a / "base.txt").write_text("from a\n")
    (wt_b / "base.txt").write_text("from b\n")

    assert vcs.worktree_merge(wt_path, "a", message="a: edit base")["merged"] is True
    res = vcs.worktree_merge(wt_path, "b", message="b: edit base")
    assert res["merged"] is False
    assert res["conflict"] is True
    assert "base.txt" in res["files"]
    # linked task worktree left clean and not mid-merge (MERGE_HEAD lives in the
    # per-worktree git dir, not in a ``.git`` directory)
    assert _git(["status", "--porcelain"], wt_path).stdout.strip() == ""
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], wt_path).stdout.strip())
    assert not (git_dir / "MERGE_HEAD").exists()
    assert (wt_path / "base.txt").read_text() == "from a\n"


def test_task_worktree_remove_keeps_branch_by_default(workspace: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wts" / "t1"
    vcs.task_worktree_add(workspace, wt_path, "t1")
    vcs.task_worktree_remove(workspace, wt_path, "t1")
    assert not wt_path.exists()
    assert _git(["rev-parse", "--verify", "refs/heads/ada/t1"], workspace).returncode == 0
    # removing an already-gone worktree must not raise
    vcs.task_worktree_remove(workspace, wt_path, "t1")


def test_task_worktree_remove_can_delete_branch(workspace: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wts" / "t1"
    vcs.task_worktree_add(workspace, wt_path, "t1")
    vcs.task_worktree_remove(workspace, wt_path, "t1", keep_branch=False)
    assert not wt_path.exists()
    assert _git(["rev-parse", "--verify", "refs/heads/ada/t1"], workspace).returncode != 0


def test_merge_branch_refuses_checked_out_target(workspace: Path, tmp_path: Path) -> None:
    vcs.task_worktree_add(workspace, tmp_path / "wts" / "t1", "t1")
    # the repo's own working tree has main checked out
    res = vcs.merge_branch(workspace, "ada/t1", "main")
    assert res == {"merged": False, "conflict": False, "error": "target branch is checked out"}
    # a branch checked out in a linked worktree is refused too
    res2 = vcs.merge_branch(workspace, "main", "ada/t1")
    assert res2["merged"] is False
    assert res2["conflict"] is False
    assert res2["error"] == "target branch is checked out"


def test_merge_branch_into_non_checked_out_branch(workspace: Path, tmp_path: Path) -> None:
    before_head = _head(workspace)
    wt_path = tmp_path / "wts" / "t1"
    vcs.task_worktree_add(workspace, wt_path, "t1")
    _commit_in(wt_path, "task.txt", "task work\n", "t1: work")

    res = vcs.merge_branch(workspace, "ada/t1", "integration", message="accept t1")
    assert res["merged"] is True
    assert res["conflict"] is False
    # the integration branch advanced to the merge commit and carries the file
    assert _head(workspace, "integration") == res["commit"]
    assert _git(["show", "integration:task.txt"], workspace).stdout == "task work\n"
    # the repo's own HEAD/branch/working tree are untouched
    assert _head(workspace) == before_head
    assert _current_branch(workspace) == "main"
    assert not (workspace / "task.txt").exists()
    assert _git(["status", "--porcelain"], workspace).stdout.strip() == ""
    # the temporary worktree is gone
    assert "ada-tmp-wt" not in _git(["worktree", "list", "--porcelain"], workspace).stdout


def test_merge_branch_conflict_aborts_clean(workspace: Path, tmp_path: Path) -> None:
    vcs.task_worktree_add(workspace, tmp_path / "wts" / "c1", "c1")
    vcs.task_worktree_add(workspace, tmp_path / "wts" / "c2", "c2")
    _commit_in(tmp_path / "wts" / "c1", "base.txt", "edit from c1\n", "c1: edit base")
    _commit_in(tmp_path / "wts" / "c2", "base.txt", "edit from c2\n", "c2: edit base")

    assert vcs.merge_branch(workspace, "ada/c1", "integ")["merged"] is True
    tip = _head(workspace, "integ")
    res = vcs.merge_branch(workspace, "ada/c2", "integ")
    assert res["merged"] is False
    assert res["conflict"] is True
    assert "base.txt" in res["files"]
    # target branch did not advance; repo stays clean; temp worktree cleaned up
    assert _head(workspace, "integ") == tip
    assert _git(["status", "--porcelain"], workspace).stdout.strip() == ""
    assert "ada-tmp-wt" not in _git(["worktree", "list", "--porcelain"], workspace).stdout


def test_cherry_pick_subtask_merge_commit(workspace: Path, tmp_path: Path) -> None:
    # The review flow's real shape: a subtask merge commit on the task branch,
    # accepted onto an integration branch via cherry-pick -m 1.
    wt_path = tmp_path / "wts" / "t1"
    vcs.task_worktree_add(workspace, wt_path, "t1")
    sub = vcs.worktree_add(wt_path, "s1")
    (sub / "feature.txt").write_text("subtask output\n")
    merged = vcs.worktree_merge(wt_path, "s1", message="s1: feature")
    assert merged["merged"] is True
    sha = merged["commit"]
    assert len(_git(["rev-list", "--parents", "-n", "1", sha], workspace).stdout.split()) == 3

    res = vcs.cherry_pick_merge(workspace, sha, "integration")
    assert res["merged"] is True
    assert res["conflict"] is False
    assert _head(workspace, "integration") == res["commit"]
    assert _git(["show", "integration:feature.txt"], workspace).stdout == "subtask output\n"
    # repo checkout untouched
    assert _current_branch(workspace) == "main"
    assert not (workspace / "feature.txt").exists()
    assert _git(["status", "--porcelain"], workspace).stdout.strip() == ""


def test_cherry_pick_plain_commit(workspace: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wts" / "t1"
    vcs.task_worktree_add(workspace, wt_path, "t1")
    sha = _commit_in(wt_path, "plain.txt", "plain\n", "t1: plain")
    res = vcs.cherry_pick_merge(workspace, sha, "integration")
    assert res["merged"] is True
    assert res["conflict"] is False
    assert _git(["show", "integration:plain.txt"], workspace).stdout == "plain\n"


def test_cherry_pick_conflict_aborts_clean(workspace: Path, tmp_path: Path) -> None:
    vcs.task_worktree_add(workspace, tmp_path / "wts" / "x1", "x1")
    sha = _commit_in(tmp_path / "wts" / "x1", "base.txt", "task version\n", "x1: edit base")
    vcs.task_worktree_add(workspace, tmp_path / "wts" / "x2", "x2")
    _commit_in(tmp_path / "wts" / "x2", "base.txt", "other version\n", "x2: edit base")
    assert vcs.merge_branch(workspace, "ada/x2", "integ")["merged"] is True
    tip = _head(workspace, "integ")

    res = vcs.cherry_pick_merge(workspace, sha, "integ")
    assert res["merged"] is False
    assert res["conflict"] is True
    assert "base.txt" in res["files"]
    assert _head(workspace, "integ") == tip
    assert _git(["status", "--porcelain"], workspace).stdout.strip() == ""
    assert "ada-tmp-wt" not in _git(["worktree", "list", "--porcelain"], workspace).stdout


def test_cherry_pick_refuses_checked_out_target(workspace: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wts" / "t1"
    vcs.task_worktree_add(workspace, wt_path, "t1")
    sha = _commit_in(wt_path, "plain.txt", "plain\n", "t1: plain")
    res = vcs.cherry_pick_merge(workspace, sha, "main")
    assert res == {"merged": False, "conflict": False, "error": "target branch is checked out"}


def test_worktree_add_with_relative_workspace_path(tmp_path, monkeypatch):
    """Regression: git resolves relative worktree paths against -C, silently nesting
    the worktree INSIDE the workspace at a phantom path while callers use the intended
    one (observed live with ADA_WORKSPACE_DIR=workspace). Paths must be absolutized."""
    import subprocess
    from pathlib import Path

    from ai_dev_assistant import vcs

    ws_abs = tmp_path / "ws"
    ws_abs.mkdir()
    (ws_abs / "base.txt").write_text("base\n")
    for args in (("init", "-q"), ("add", "-A"),
                 ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b")):
        subprocess.run(["git", *args], cwd=ws_abs, check=True, capture_output=True)

    monkeypatch.chdir(tmp_path)
    rel = Path("ws")  # relative, like a relative ADA_WORKSPACE_DIR in production

    wt = vcs.worktree_add(rel, "sX")
    assert wt.is_absolute() and wt.is_dir()
    assert (wt / "base.txt").is_file(), "worktree checkout must be populated"
    # no phantom nested worktree inside the workspace
    assert not (ws_abs / "ws").exists()

    (wt / "new.txt").write_text("n\n")
    res = vcs.worktree_merge(rel, "sX", message="m")
    assert res.get("merged"), res
    assert (ws_abs / "new.txt").is_file()
    vcs.worktree_remove(rel, "sX")
    assert not wt.exists()
