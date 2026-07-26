"""Workspaces — named groups of inter-related projects.

A workspace groups projects that relate to each other so they can be run
together through the existing cross-project fan-out
(``orchestration.fanout.run_cross_project``). The workspace stores the group's
default dependency map ({slug: [upstream slugs]}); ``workspace_run_spec``
expands a workspace (or a subset of its members) into the ``projects`` +
``deps`` payload the multi-project run path already accepts. Workspaces build
ON the fan-out machinery — they never duplicate it.

Invariants:
- a project belongs to at most ONE workspace; assigning it elsewhere moves it;
- deleting a workspace removes only the group — projects survive ungrouped;
- deleting a project unassigns it from its workspace (see projects.delete_project);
- default_deps only ever references current members and is acyclic (validated
  via ``fanout.validate_project_deps`` + its wave builder — not reimplemented).

Storage: ``workspaces.json`` next to the project registry in data_dir, same
read/write style as projects.py. Reads never raise: unknown slugs, a missing
file, or a legacy data_dir all yield graceful empties.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import projects as _projects
from .config import Settings
from .orchestration.fanout import _dependency_waves, validate_project_deps

_ENTRY_DEFAULTS: dict[str, Any] = {
    "description": "",
    "created_at": 0.0,
}


def _ws_path(settings: Settings):
    return settings.data_dir / "workspaces.json"


def _normalize(entry: dict[str, Any]) -> dict[str, Any]:
    """A full workspace entry from a possibly-thin/partial record."""
    out = dict(entry)
    out.setdefault("slug", _projects.slugify(str(entry.get("name", ""))))
    out.setdefault("name", out["slug"])
    for key, default in _ENTRY_DEFAULTS.items():
        out.setdefault(key, default)
    slugs = out.get("project_slugs")
    out["project_slugs"] = list(slugs) if isinstance(slugs, list) else []
    deps = out.get("default_deps")
    out["default_deps"] = (
        {str(k): list(v or []) for k, v in deps.items()} if isinstance(deps, dict) else {}
    )
    return out


def _read(settings: Settings) -> list[dict[str, Any]]:
    path = _ws_path(settings)
    if not path.exists():  # legacy data_dir: no workspaces yet
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write(settings: Settings, items: list[dict[str, Any]]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _ws_path(settings).write_text(json.dumps(items, ensure_ascii=False, indent=2))


def _remove_member(ws: dict[str, Any], project_slug: str) -> None:
    """Drop a member and prune every default_deps edge that touches it."""
    ws["project_slugs"] = [s for s in ws["project_slugs"] if s != project_slug]
    deps = ws.get("default_deps") or {}
    pruned: dict[str, list[str]] = {}
    for key, ups in deps.items():
        if key == project_slug:
            continue
        kept = [u for u in ups if u != project_slug]
        if kept:
            pruned[key] = kept
    ws["default_deps"] = pruned


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_workspaces(settings: Settings) -> list[dict[str, Any]]:
    """All workspaces (normalized dicts). Never raises; [] on missing/corrupt file."""
    return [_normalize(w) for w in _read(settings)]


def get_workspace(settings: Settings, ws_slug: str) -> dict[str, Any] | None:
    """The normalized workspace entry for ``ws_slug``, or None."""
    return next((w for w in list_workspaces(settings) if w.get("slug") == ws_slug), None)


def create_workspace(settings: Settings, name: str, description: str = "",
                     projects: list[str] | None = None) -> dict[str, Any]:
    """Create a workspace named ``name`` (slugified; collision → ``-2``/``-3``…).

    ``projects`` (optional) are assigned as initial members — each must exist
    in the project registry (ValueError otherwise, nothing is written), and any
    that already belong to another workspace are MOVED here.
    """
    member_slugs: list[str] = []
    for p in projects or []:
        slug = _projects.slugify(p)
        if _projects.get_project(settings, slug) is None:
            raise ValueError(f"unknown project: {p}")
        if slug not in member_slugs:
            member_slugs.append(slug)

    items = list_workspaces(settings)
    taken = {w["slug"] for w in items}
    base = _projects.slugify(name)
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1

    # one-workspace-per-project: moving members here prunes them (and their
    # dependency edges) from any workspace they currently belong to.
    for w in items:
        for member in member_slugs:
            if member in w["project_slugs"]:
                _remove_member(w, member)

    entry = _normalize({
        "slug": slug,
        "name": name.strip() or slug,
        "description": description,
        "created_at": time.time(),
        "project_slugs": member_slugs,
    })
    items.append(entry)
    _write(settings, items)
    return entry


def update_workspace(settings: Settings, ws_slug: str, *, name: str | None = None,
                     description: str | None = None) -> dict[str, Any]:
    """Rename / re-describe a workspace. The slug is stable — it never changes."""
    items = list_workspaces(settings)
    for i, w in enumerate(items):
        if w.get("slug") == ws_slug:
            if name is not None and name.strip():
                w["name"] = name.strip()
            if description is not None:
                w["description"] = description
            items[i] = w
            _write(settings, items)
            return w
    raise ValueError(f"unknown workspace: {ws_slug}")


def delete_workspace(settings: Settings, ws_slug: str) -> None:
    """Remove the GROUP only — member projects survive, ungrouped. No-op on unknown."""
    items = list_workspaces(settings)
    kept = [w for w in items if w.get("slug") != ws_slug]
    if len(kept) != len(items):
        _write(settings, kept)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def assign_project(settings: Settings, ws_slug: str, project_slug: str) -> dict[str, Any]:
    """Put a project into a workspace. A project belongs to at most ONE
    workspace, so this MOVES it (pruning its dependency edges in the old one).
    The project must exist in the registry. Returns the updated workspace."""
    project_slug = _projects.slugify(project_slug)
    if _projects.get_project(settings, project_slug) is None:
        raise ValueError(f"unknown project: {project_slug}")
    items = list_workspaces(settings)
    target = next((w for w in items if w.get("slug") == ws_slug), None)
    if target is None:
        raise ValueError(f"unknown workspace: {ws_slug}")
    for w in items:
        if w is not target and project_slug in w["project_slugs"]:
            _remove_member(w, project_slug)
    if project_slug not in target["project_slugs"]:
        target["project_slugs"].append(project_slug)
    _write(settings, items)
    return target


def unassign_project(settings: Settings, ws_slug: str, project_slug: str) -> dict[str, Any]:
    """Remove a project from a workspace (deps edges pruned with it). The
    project itself is untouched. Idempotent when it isn't a member."""
    project_slug = _projects.slugify(project_slug)
    items = list_workspaces(settings)
    target = next((w for w in items if w.get("slug") == ws_slug), None)
    if target is None:
        raise ValueError(f"unknown workspace: {ws_slug}")
    if project_slug in target["project_slugs"]:
        _remove_member(target, project_slug)
        _write(settings, items)
    return target


def workspace_of(settings: Settings, project_slug: str) -> str | None:
    """The slug of the workspace containing ``project_slug``, or None."""
    project_slug = _projects.slugify(project_slug)
    for w in list_workspaces(settings):
        if project_slug in w["project_slugs"]:
            return w["slug"]
    return None


# ---------------------------------------------------------------------------
# Dependencies & run expansion
# ---------------------------------------------------------------------------

def set_workspace_deps(settings: Settings, ws_slug: str,
                       deps: dict[str, list[str]] | None) -> dict[str, Any]:
    """Set the workspace's default dependency map ({slug: [upstream slugs]}).

    Validated against current members and for acyclicity through the fan-out's
    own validator/wave builder (the exact code the run path uses) — unknown
    slugs, self-deps, and cycles raise ValueError. Returns the updated entry.
    """
    items = list_workspaces(settings)
    target = next((w for w in items if w.get("slug") == ws_slug), None)
    if target is None:
        raise ValueError(f"unknown workspace: {ws_slug}")
    members = list(target["project_slugs"])
    norm = validate_project_deps(members, deps)
    if norm:
        _dependency_waves(members, norm)  # raises ValueError on cycles
    target["default_deps"] = norm
    _write(settings, items)
    return target


def workspace_run_spec(settings: Settings, ws_slug: str,
                       subset: list[str] | None = None) -> dict[str, Any]:
    """Expand a workspace into the payload for the multi-project run path.

    Returns ``{"projects": [...], "deps": {...}}`` — all member slugs (or the
    validated ``subset``; non-members raise ValueError) plus default_deps
    filtered to the chosen set, dropping edges to excluded members. Feed this
    straight into ``run_cross_project(settings, prompt, spec["projects"],
    deps=spec["deps"])``.
    """
    ws = get_workspace(settings, ws_slug)
    if ws is None:
        raise ValueError(f"unknown workspace: {ws_slug}")
    members = ws["project_slugs"]
    if subset is None:
        chosen = list(members)
    else:
        member_set = set(members)
        chosen = []
        for s in subset:
            slug = _projects.slugify(s)
            if slug not in member_set:
                raise ValueError(
                    f"project '{s}' is not a member of workspace '{ws_slug}'")
            if slug not in chosen:
                chosen.append(slug)
    chosen_set = set(chosen)
    deps: dict[str, list[str]] = {}
    for key, ups in ws["default_deps"].items():
        if key not in chosen_set:
            continue
        kept = [u for u in ups if u in chosen_set]
        if kept:
            deps[key] = kept
    return {"projects": chosen, "deps": deps}
