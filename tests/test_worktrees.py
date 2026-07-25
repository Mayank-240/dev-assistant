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
