"""Tests for autonomous maintenance mode (src/ai_dev_assistant/maintenance.py):
policy CRUD/validation via the projects policy mechanism, cadence math for both
interval and cron recurrence, rendered due entries, and skip rules."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.maintenance import (
    MAINTENANCE_TASKS,
    due_maintenance,
    get_policy,
    mark_maintenance_started,
    set_policy,
)
from ai_dev_assistant.playbooks import render
from ai_dev_assistant.projects import archive_project, create_project, get_project

T0 = datetime(2026, 7, 1, 12, 0).timestamp()  # local noon, deterministic cron math
HOUR = 3600.0


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "ws",
                    docs_dir=tmp_path / "docs")


@pytest.fixture()
def project(settings) -> str:
    return create_project(settings, "Alpha")["slug"]


# ---------------------------------------------------------------------------
# policy CRUD via the projects policy mechanism
# ---------------------------------------------------------------------------

def test_default_policy_is_disabled_with_all_tasks(settings, project):
    pol = get_policy(settings, project)
    assert pol == {"enabled": False, "cadence": None, "budget_usd": 0.0,
                   "tasks": list(MAINTENANCE_TASKS), "last_run_at": None}


def test_get_policy_unknown_project(settings):
    with pytest.raises(ValueError, match="unknown project"):
        get_policy(settings, "nope")


def test_set_policy_persists_under_project_policy(settings, project):
    stored = set_policy(settings, project, {"enabled": True, "cadence": 24,
                                            "budget_usd": 2.0,
                                            "tasks": ["doc-drift"]})
    assert stored["enabled"] is True and stored["cadence"] == 24.0
    # Round-trips through the SAME registry mechanism projects.set_policy uses.
    entry = get_project(settings, project)
    assert entry["policy"]["maintenance"] == stored
    assert get_policy(settings, project) == stored


def test_set_policy_partial_update_preserves_other_keys(settings, project):
    set_policy(settings, project, {"enabled": True, "cadence": "0 3 * * *",
                                   "tasks": ["dead-code"]})
    stored = set_policy(settings, project, {"budget_usd": 5.0})
    assert stored["cadence"] == "0 3 * * *"          # preserved
    assert stored["tasks"] == ["dead-code"]          # preserved
    assert stored["budget_usd"] == 5.0
    mark_maintenance_started(settings, project, T0)
    # And a later edit keeps last_run_at (owned by mark_maintenance_started).
    assert set_policy(settings, project, {"budget_usd": 1.0})["last_run_at"] == T0


def test_set_policy_validation_errors(settings, project):
    with pytest.raises(ValueError, match="cron"):
        set_policy(settings, project, {"enabled": True, "cadence": "not a cron"})
    with pytest.raises(ValueError, match="hours must be > 0"):
        set_policy(settings, project, {"enabled": True, "cadence": 0})
    with pytest.raises(ValueError, match="needs a cadence"):
        set_policy(settings, project, {"enabled": True})
    with pytest.raises(ValueError, match="unknown maintenance task"):
        set_policy(settings, project, {"tasks": ["doc-drift", "mine-bitcoin"]})
    with pytest.raises(ValueError, match="budget_usd"):
        set_policy(settings, project, {"budget_usd": -1})
    with pytest.raises(ValueError, match="unknown maintenance policy key"):
        set_policy(settings, project, {"cadense": 24})
    with pytest.raises(ValueError, match="'enabled' must be a bool"):
        set_policy(settings, project, {"enabled": "yes"})
    with pytest.raises(ValueError, match="unknown project"):
        set_policy(settings, "nope", {"enabled": False})


def test_set_policy_normalizes_cron_whitespace(settings, project):
    stored = set_policy(settings, project, {"enabled": True, "cadence": " 0   3 * * *  "})
    assert stored["cadence"] == "0 3 * * *"


# ---------------------------------------------------------------------------
# due_maintenance cadence math
# ---------------------------------------------------------------------------

def test_never_run_enabled_project_is_due(settings, project):
    set_policy(settings, project, {"enabled": True, "cadence": 24})
    due = due_maintenance(settings, now=T0)
    assert {e["slug"] for e in due} == {project}


def test_interval_cadence_elapsed_math(settings, project):
    set_policy(settings, project, {"enabled": True, "cadence": 4})
    mark_maintenance_started(settings, project, T0)
    assert due_maintenance(settings, now=T0 + 2 * HOUR) == []
    assert due_maintenance(settings, now=T0 + 4 * HOUR) != []


def test_cron_cadence_fires_at_next_matching_minute(settings, project):
    set_policy(settings, project, {"enabled": True, "cadence": "0 3 * * *"})
    mark_maintenance_started(settings, project, T0)  # July 1, 12:00 local
    before = datetime(2026, 7, 2, 2, 59).timestamp()
    after = datetime(2026, 7, 2, 3, 0).timestamp()
    assert due_maintenance(settings, now=before) == []
    assert due_maintenance(settings, now=after) != []


def test_mark_maintenance_started_advances(settings, project):
    set_policy(settings, project, {"enabled": True, "cadence": 24})
    assert due_maintenance(settings, now=T0) != []
    mark_maintenance_started(settings, project, T0)
    assert get_policy(settings, project)["last_run_at"] == T0
    assert due_maintenance(settings, now=T0 + HOUR) == []
    assert due_maintenance(settings, now=T0 + 24 * HOUR) != []


# ---------------------------------------------------------------------------
# rendered entries
# ---------------------------------------------------------------------------

def test_due_entries_carry_rendered_playbook_and_budget(settings, project):
    set_policy(settings, project, {"enabled": True, "cadence": 24, "budget_usd": 2.5,
                                   "tasks": ["doc-drift", "security-audit"]})
    due = due_maintenance(settings, now=T0)
    assert [e["playbook_id"] for e in due] == ["doc-drift", "security-audit"]
    by_task = {e["task_id_hint"]: e for e in due}
    assert set(by_task) == {f"maint-{project}-doc-drift",
                            f"maint-{project}-security-audit"}
    for e in due:
        assert set(e) == {"slug", "task_id_hint", "playbook_id", "prompt", "title",
                          "settings_overrides", "budget_usd"}
        assert e["slug"] == project and e["budget_usd"] == 2.5
        assert e["prompt"].strip() and e["title"].strip()
        assert isinstance(e["settings_overrides"], dict)
    assert "documentation drift" in by_task[f"maint-{project}-doc-drift"]["prompt"]
    assert "security audit" in by_task[f"maint-{project}-security-audit"]["prompt"]


def test_every_maintenance_task_maps_to_a_paramless_playbook():
    for task, pid in MAINTENANCE_TASKS.items():
        out = render(pid, {})  # the unattended tick has no params to give
        assert out["prompt"].strip(), f"{task}: empty prompt"
        assert "{" not in out["prompt"] and "}" not in out["prompt"]


# ---------------------------------------------------------------------------
# skip rules
# ---------------------------------------------------------------------------

def test_disabled_empty_and_archived_projects_skipped(settings):
    disabled = create_project(settings, "Disabled")["slug"]
    set_policy(settings, disabled, {"cadence": 24})  # enabled stays False
    empty = create_project(settings, "Empty")["slug"]
    set_policy(settings, empty, {"enabled": True, "cadence": 24, "tasks": []})
    archived = create_project(settings, "Archived")["slug"]
    set_policy(settings, archived, {"enabled": True, "cadence": 24})
    archive_project(settings, archived)
    live = create_project(settings, "Live")["slug"]
    set_policy(settings, live, {"enabled": True, "cadence": 24, "tasks": ["dead-code"]})
    assert {e["slug"] for e in due_maintenance(settings, now=T0)} == {live}


def test_corrupt_stored_policy_never_crashes_the_poll(settings, project):
    # A hand-edited registry entry must read as "disabled", not explode.
    from ai_dev_assistant.projects import set_policy as set_project_policy
    set_project_policy(settings, project, {"maintenance": {"enabled": True,
                                                           "cadence": "not a cron"}})
    assert due_maintenance(settings, now=T0) == []
    assert get_policy(settings, project)["enabled"] is False
