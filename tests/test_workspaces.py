"""Tests for workspaces — named groups of inter-related projects: CRUD +
slug collisions, one-workspace-per-project moves, group-only deletion,
delete-project unassignment, deps validation through the fan-out validator,
run-spec subset filtering, and graceful legacy reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.projects import create_project, delete_project, get_project, list_projects
from ai_dev_assistant.workspaces import (
    assign_project,
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces,
    set_workspace_deps,
    unassign_project,
    update_workspace,
    workspace_of,
    workspace_run_spec,
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "ws",
                    docs_dir=tmp_path / "docs")


def make_projects(settings: Settings, *names: str) -> list[str]:
    return [create_project(settings, n)["slug"] for n in names]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_create_get_update_delete_roundtrip(settings: Settings) -> None:
    ws = create_workspace(settings, "My Platform", description="apps + libs")
    assert ws["slug"] == "my-platform"
    assert ws["name"] == "My Platform"
    assert ws["description"] == "apps + libs"
    assert ws["created_at"] > 0
    assert ws["project_slugs"] == []
    assert ws["default_deps"] == {}

    got = get_workspace(settings, "my-platform")
    assert got is not None and got["name"] == "My Platform"
    assert [w["slug"] for w in list_workspaces(settings)] == ["my-platform"]

    upd = update_workspace(settings, "my-platform", name="Platform v2", description="new")
    assert upd["name"] == "Platform v2" and upd["description"] == "new"
    assert upd["slug"] == "my-platform"  # slug is stable across renames

    delete_workspace(settings, "my-platform")
    assert get_workspace(settings, "my-platform") is None
    assert list_workspaces(settings) == []


def test_slug_collision_gets_suffix(settings: Settings) -> None:
    assert create_workspace(settings, "Apps")["slug"] == "apps"
    assert create_workspace(settings, "Apps")["slug"] == "apps-2"
    assert create_workspace(settings, "apps!")["slug"] == "apps-3"
    assert len(list_workspaces(settings)) == 3


def test_create_with_projects_validates_and_assigns(settings: Settings) -> None:
    make_projects(settings, "alpha", "beta")
    ws = create_workspace(settings, "Group", projects=["alpha", "beta"])
    assert ws["project_slugs"] == ["alpha", "beta"]
    assert workspace_of(settings, "alpha") == "group"

    with pytest.raises(ValueError, match="unknown project"):
        create_workspace(settings, "Broken", projects=["alpha", "nope"])
    # nothing was written for the failed create
    assert [w["slug"] for w in list_workspaces(settings)] == ["group"]


def test_update_delete_unknown_workspace(settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown workspace"):
        update_workspace(settings, "ghost", name="x")
    delete_workspace(settings, "ghost")  # no-op, never raises


# ---------------------------------------------------------------------------
# Membership: one workspace per project
# ---------------------------------------------------------------------------

def test_assign_moves_between_workspaces(settings: Settings) -> None:
    make_projects(settings, "alpha", "beta")
    create_workspace(settings, "One", projects=["alpha", "beta"])
    create_workspace(settings, "Two")
    set_workspace_deps(settings, "one", {"beta": ["alpha"]})

    assign_project(settings, "two", "alpha")
    assert workspace_of(settings, "alpha") == "two"
    one = get_workspace(settings, "one")
    assert one["project_slugs"] == ["beta"]
    assert one["default_deps"] == {}  # edge to the moved project was pruned
    assert get_workspace(settings, "two")["project_slugs"] == ["alpha"]

    # assigning again is idempotent
    assign_project(settings, "two", "alpha")
    assert get_workspace(settings, "two")["project_slugs"] == ["alpha"]


def test_create_with_projects_moves_from_other_workspace(settings: Settings) -> None:
    make_projects(settings, "alpha")
    create_workspace(settings, "Old", projects=["alpha"])
    create_workspace(settings, "New", projects=["alpha"])
    assert workspace_of(settings, "alpha") == "new"
    assert get_workspace(settings, "old")["project_slugs"] == []


def test_assign_validates_project_and_workspace(settings: Settings) -> None:
    create_workspace(settings, "One")
    with pytest.raises(ValueError, match="unknown project"):
        assign_project(settings, "one", "nope")
    make_projects(settings, "alpha")
    with pytest.raises(ValueError, match="unknown workspace"):
        assign_project(settings, "ghost", "alpha")


def test_unassign_prunes_deps_and_is_idempotent(settings: Settings) -> None:
    make_projects(settings, "alpha", "beta", "gamma")
    create_workspace(settings, "One", projects=["alpha", "beta", "gamma"])
    set_workspace_deps(settings, "one", {"beta": ["alpha"], "gamma": ["alpha", "beta"]})

    unassign_project(settings, "one", "alpha")
    one = get_workspace(settings, "one")
    assert one["project_slugs"] == ["beta", "gamma"]
    assert one["default_deps"] == {"gamma": ["beta"]}
    assert workspace_of(settings, "alpha") is None

    unassign_project(settings, "one", "alpha")  # already gone: no-op
    assert get_workspace(settings, "one")["project_slugs"] == ["beta", "gamma"]


# ---------------------------------------------------------------------------
# Deletion semantics
# ---------------------------------------------------------------------------

def test_delete_workspace_preserves_projects(settings: Settings) -> None:
    make_projects(settings, "alpha", "beta")
    create_workspace(settings, "One", projects=["alpha", "beta"])
    delete_workspace(settings, "one")
    slugs = {p["slug"] for p in list_projects(settings)}
    assert {"alpha", "beta"} <= slugs  # projects survive, ungrouped
    assert workspace_of(settings, "alpha") is None
    assert workspace_of(settings, "beta") is None


def test_delete_project_unassigns_from_workspace(settings: Settings) -> None:
    make_projects(settings, "alpha", "beta")
    create_workspace(settings, "One", projects=["alpha", "beta"])
    set_workspace_deps(settings, "one", {"beta": ["alpha"]})

    delete_project(settings, "alpha")
    assert get_project(settings, "alpha") is None
    one = get_workspace(settings, "one")
    assert one["project_slugs"] == ["beta"]
    assert one["default_deps"] == {}


# ---------------------------------------------------------------------------
# Dependency validation (through the fan-out validator)
# ---------------------------------------------------------------------------

def test_deps_unknown_self_and_cycle_rejected(settings: Settings) -> None:
    make_projects(settings, "alpha", "beta")
    create_workspace(settings, "One", projects=["alpha", "beta"])

    with pytest.raises(ValueError, match="unknown"):
        set_workspace_deps(settings, "one", {"beta": ["nope"]})
    with pytest.raises(ValueError, match="itself"):
        set_workspace_deps(settings, "one", {"beta": ["beta"]})
    with pytest.raises(ValueError, match="cycle"):
        set_workspace_deps(settings, "one", {"alpha": ["beta"], "beta": ["alpha"]})
    assert get_workspace(settings, "one")["default_deps"] == {}  # nothing persisted

    ws = set_workspace_deps(settings, "one", {"beta": ["alpha"], "alpha": []})
    assert ws["default_deps"] == {"beta": ["alpha"]}  # normalized: empty lists dropped
    with pytest.raises(ValueError, match="unknown workspace"):
        set_workspace_deps(settings, "ghost", {})


# ---------------------------------------------------------------------------
# Run expansion
# ---------------------------------------------------------------------------

def test_run_spec_full_and_subset_filtering(settings: Settings) -> None:
    make_projects(settings, "alpha", "beta", "gamma")
    create_workspace(settings, "One", projects=["alpha", "beta", "gamma"])
    set_workspace_deps(settings, "one", {"beta": ["alpha"], "gamma": ["beta"]})

    spec = workspace_run_spec(settings, "one")
    assert spec == {"projects": ["alpha", "beta", "gamma"],
                    "deps": {"beta": ["alpha"], "gamma": ["beta"]}}

    # excluding beta drops both the beta key and gamma's edge to it
    spec = workspace_run_spec(settings, "one", subset=["alpha", "gamma"])
    assert spec == {"projects": ["alpha", "gamma"], "deps": {}}

    spec = workspace_run_spec(settings, "one", subset=["alpha", "beta"])
    assert spec == {"projects": ["alpha", "beta"], "deps": {"beta": ["alpha"]}}

    with pytest.raises(ValueError, match="not a member"):
        workspace_run_spec(settings, "one", subset=["alpha", "nope"])
    with pytest.raises(ValueError, match="unknown workspace"):
        workspace_run_spec(settings, "ghost")


# ---------------------------------------------------------------------------
# Legacy / graceful reads
# ---------------------------------------------------------------------------

def test_legacy_data_dir_reads_are_graceful(settings: Settings) -> None:
    # no workspaces.json at all (pre-workspaces data_dir)
    assert list_workspaces(settings) == []
    assert get_workspace(settings, "anything") is None
    assert workspace_of(settings, "alpha") is None
    assert not (settings.data_dir / "workspaces.json").exists()  # reads never create it


def test_corrupt_or_wrong_shape_file_reads_empty(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "workspaces.json"
    path.write_text("{not json")
    assert list_workspaces(settings) == []
    path.write_text(json.dumps({"slug": "x"}))  # dict, not a list
    assert list_workspaces(settings) == []


def test_thin_entries_normalize_on_read(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "workspaces.json").write_text(json.dumps([{"name": "Old Group"}]))
    ws = list_workspaces(settings)[0]
    assert ws["slug"] == "old-group"
    assert ws["project_slugs"] == [] and ws["default_deps"] == {}
    assert ws["description"] == "" and ws["created_at"] == 0.0
