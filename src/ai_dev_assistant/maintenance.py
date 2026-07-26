"""Autonomous maintenance mode — recurring housekeeping runs per project.

A project can opt in to unattended maintenance: on a cadence (interval hours or
a 5-field cron expression), a chosen subset of MAINTENANCE_TASKS is rendered
through the playbook catalog and handed to the server tick as ready-to-enqueue
run payloads. Every maintenance playbook's prompt instructs the agent that a
clean bill of health means completing WITHOUT changing files — so no delivery
branch appears when there is nothing to do.

Storage: the policy is a plain dict under the ``"maintenance"`` key of the
project's policy in the registry — persisted through :func:`projects.set_policy`
(the same mechanism the run-policy editor uses), not a new store. Shape:

    {"enabled": bool,
     "cadence": "<cron>" | <hours: float> | None,
     "budget_usd": float,
     "tasks": [subset of MAINTENANCE_TASKS],
     "last_run_at": float | None}

Cadence semantics mirror ``orchestration/schedules.py``: a cron string is
validated by its ``validate_cron`` and fires when a matching minute boundary
(LOCAL time) has passed since ``last_run_at``; a numeric cadence is
``every_hours`` and fires once that many hours have elapsed. A never-run
enabled project is due immediately. This module computes; the SERVER's periodic
tick enqueues each due entry as a normal task and then calls
:func:`mark_maintenance_started` to advance ``last_run_at``.

No background thread, no writes outside the project registry; everything is
deterministic through the explicit ``now`` parameters.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from . import playbooks, projects
from .config import Settings
from .orchestration.schedules import next_fire, validate_cron

__all__ = [
    "MAINTENANCE_TASKS",
    "get_policy",
    "set_policy",
    "due_maintenance",
    "mark_maintenance_started",
]

# The key inside the project's policy dict where the maintenance policy lives.
POLICY_KEY = "maintenance"

# Maintenance task id -> playbook id. Every playbook here must render with NO
# params (all optional/defaulted) so the unattended tick can call it, and its
# prompt must state that "nothing to do" means completing without file changes.
MAINTENANCE_TASKS: dict[str, str] = {
    "dependency-refresh": "dependency-refresh",
    "security-audit": "security-audit",
    "doc-drift": "doc-drift",
    "dead-code": "dead-code",
}

_POLICY_KEYS = {"enabled", "cadence", "budget_usd", "tasks", "last_run_at"}


def _default_policy() -> dict[str, Any]:
    return {
        "enabled": False,
        "cadence": None,
        "budget_usd": 0.0,
        "tasks": list(MAINTENANCE_TASKS),
        "last_run_at": None,
    }


# ---------------------------------------------------------------------------
# Validation / normalization
# ---------------------------------------------------------------------------

def _validate_cadence(cadence: Any) -> str | float | None:
    """None stays None; a string must validate as a 5-field cron expression
    (returned whitespace-normalized); a number is every_hours and must be > 0."""
    if cadence is None:
        return None
    if isinstance(cadence, str):
        if not cadence.strip():
            return None
        return validate_cron(cadence)  # raises ValueError naming the bad field
    if isinstance(cadence, bool):  # bool is an int subclass — reject explicitly
        raise ValueError(f"maintenance cadence must be a cron string or hours, got {cadence!r}")
    if isinstance(cadence, (int, float)):
        hours = float(cadence)
        if hours <= 0:
            raise ValueError(f"maintenance cadence hours must be > 0, got {hours}")
        return hours
    raise ValueError(
        f"maintenance cadence must be a cron string or a number of hours, got {cadence!r}")


def _validate_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """A full, validated, normalized policy dict — or ValueError."""
    unknown = sorted(set(policy) - _POLICY_KEYS)
    if unknown:
        raise ValueError(
            f"unknown maintenance policy key(s): {unknown}; expected {sorted(_POLICY_KEYS)}")
    out = _default_policy()

    enabled = policy.get("enabled", out["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError(f"maintenance 'enabled' must be a bool, got {enabled!r}")
    out["enabled"] = enabled

    out["cadence"] = _validate_cadence(policy.get("cadence", out["cadence"]))
    if out["enabled"] and out["cadence"] is None:
        raise ValueError("enabled maintenance needs a cadence (cron string or hours)")

    budget = policy.get("budget_usd", out["budget_usd"])
    try:
        budget = float(budget)
    except (TypeError, ValueError):
        raise ValueError(f"maintenance 'budget_usd' must be a number, got {budget!r}") from None
    if budget < 0:
        raise ValueError(f"maintenance 'budget_usd' must be >= 0, got {budget}")
    out["budget_usd"] = budget

    tasks = policy.get("tasks", out["tasks"])
    if isinstance(tasks, str) or not isinstance(tasks, (list, tuple)):
        raise ValueError(f"maintenance 'tasks' must be a list, got {tasks!r}")
    bad = sorted(set(tasks) - set(MAINTENANCE_TASKS))
    if bad:
        raise ValueError(
            f"unknown maintenance task(s): {bad}; expected a subset of {sorted(MAINTENANCE_TASKS)}")
    out["tasks"] = list(dict.fromkeys(tasks))  # dedupe, keep order

    last = policy.get("last_run_at", out["last_run_at"])
    if last is not None:
        try:
            last = float(last)
        except (TypeError, ValueError):
            raise ValueError(
                f"maintenance 'last_run_at' must be a timestamp or None, got {last!r}") from None
    out["last_run_at"] = last
    return out


def _lenient_policy(raw: Any) -> dict[str, Any]:
    """Best-effort normalization for reads: a hand-edited or legacy registry
    entry must never crash the poll — anything invalid falls back to defaults
    (which, crucially, means disabled)."""
    if not isinstance(raw, dict):
        return _default_policy()
    try:
        return _validate_policy({k: v for k, v in raw.items() if k in _POLICY_KEYS})
    except ValueError:
        return _default_policy()


# ---------------------------------------------------------------------------
# Policy CRUD (via the projects policy mechanism)
# ---------------------------------------------------------------------------

def get_policy(settings: Settings, slug: str) -> dict[str, Any]:
    """The project's maintenance policy, normalized with defaults applied.

    Unknown project -> ValueError. A project that never opted in gets the
    default policy (disabled, all tasks, no cadence).
    """
    entry = projects.get_project(settings, slug)
    if entry is None:
        raise ValueError(f"unknown project: {slug}")
    return _lenient_policy((entry.get("policy") or {}).get(POLICY_KEY))


def set_policy(settings: Settings, slug: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Merge ``policy`` into the project's maintenance policy, validate, persist.

    Partial updates work: keys absent from ``policy`` keep their stored value.
    ``last_run_at`` is owned by :func:`mark_maintenance_started` and is
    preserved unless explicitly provided. Returns the stored, normalized policy.
    Raises ValueError on an unknown project, an unknown key, a bad cadence
    (cron validated via schedules' validator; hours must be > 0), tasks outside
    MAINTENANCE_TASKS, or a negative budget.
    """
    if not isinstance(policy, dict):
        raise ValueError(f"maintenance policy must be a dict, got {type(policy).__name__}")
    current = get_policy(settings, slug)  # ValueError on unknown project
    merged = _validate_policy({**current, **policy})
    projects.set_policy(settings, slug, {POLICY_KEY: merged})
    return merged


# ---------------------------------------------------------------------------
# Due computation (polled by the server tick)
# ---------------------------------------------------------------------------

def _cadence_elapsed(cadence: str | float, last_run_at: float | None, ts: float) -> bool:
    if last_run_at is None:
        return True  # never run -> due immediately
    if isinstance(cadence, str):
        try:
            fire = next_fire(cadence, datetime.fromtimestamp(last_run_at))
        except ValueError:
            return False  # never-firing expression: skip, never crash the poll
        return fire.timestamp() <= ts
    return last_run_at + cadence * 3600.0 <= ts


def due_maintenance(settings: Settings, now: float | None = None) -> list[dict[str, Any]]:
    """Ready-to-enqueue maintenance entries for every due project.

    A project contributes when it is not archived, its maintenance policy is
    enabled with a cadence and a non-empty task list, and the cadence has
    elapsed since ``last_run_at`` (never-run projects are due immediately).
    Each ENABLED TASK of a due project yields one entry:

        {"slug", "task_id_hint", "playbook_id", "prompt", "title",
         "settings_overrides", "budget_usd"}

    where prompt/title/settings_overrides come from ``playbooks.render`` with
    default params and ``budget_usd`` is the policy's per-run budget (0 = no
    cap). The server enqueues each entry as a normal task and then calls
    :func:`mark_maintenance_started` ONCE per project — all task entries of one
    project come from the same tick.
    """
    ts = time.time() if now is None else float(now)
    out: list[dict[str, Any]] = []
    for entry in projects.list_projects(settings):
        if entry.get("archived"):
            continue
        pol = _lenient_policy((entry.get("policy") or {}).get(POLICY_KEY))
        if not pol["enabled"] or pol["cadence"] is None or not pol["tasks"]:
            continue
        if not _cadence_elapsed(pol["cadence"], pol["last_run_at"], ts):
            continue
        slug = entry["slug"]
        for task in pol["tasks"]:
            payload = playbooks.render(MAINTENANCE_TASKS[task], {})
            out.append({
                "slug": slug,
                "task_id_hint": f"maint-{slug}-{task}",
                "playbook_id": MAINTENANCE_TASKS[task],
                "prompt": payload["prompt"],
                "title": payload["title"],
                "settings_overrides": payload["settings_overrides"],
                "budget_usd": pol["budget_usd"],
            })
    return out


def mark_maintenance_started(settings: Settings, slug: str, when: float | None = None) -> dict[str, Any]:
    """Record that maintenance runs were enqueued for ``slug`` — advances the
    cadence reference so the project cannot fire twice in one window. Returns
    the stored policy."""
    ts = time.time() if when is None else float(when)
    current = get_policy(settings, slug)
    current["last_run_at"] = ts
    projects.set_policy(settings, slug, {POLICY_KEY: current})
    return current
