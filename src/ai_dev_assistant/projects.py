"""Project registry — named projects that scope memory and the knowledge graph.

Each project owns its own memory.db and knowledge_graph.json (under
data_dir/projects/<slug>/). Memory can additionally be written to a shared global
store; the knowledge graph is project-scoped only. Metadata lives in
data_dir/projects.json.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .config import Settings

DEFAULT_PROJECT = "default"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48] or "project"


def _read_registry(settings: Settings) -> list[dict[str, Any]]:
    path = settings.registry_path
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_registry(settings: Settings, items: list[dict[str, Any]]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.registry_path.write_text(json.dumps(items, ensure_ascii=False, indent=2))


def list_projects(settings: Settings) -> list[dict[str, Any]]:
    """All known projects, default-first. Ensures a 'default' project always exists."""
    items = _read_registry(settings)
    if not any(p.get("slug") == DEFAULT_PROJECT for p in items):
        items.insert(0, {"slug": DEFAULT_PROJECT, "name": "Default", "created_at": time.time()})
        _write_registry(settings, items)
    return items


def create_project(settings: Settings, name: str) -> dict[str, Any]:
    """Create (or return existing) project by name; provisions its data dir."""
    slug = slugify(name)
    items = list_projects(settings)
    existing = next((p for p in items if p.get("slug") == slug), None)
    if existing:
        return existing
    entry = {"slug": slug, "name": name.strip() or slug, "created_at": time.time()}
    items.append(entry)
    _write_registry(settings, items)
    (settings.projects_dir / slug).mkdir(parents=True, exist_ok=True)
    return entry


def resolve(settings: Settings, slug: str | None) -> str:
    """Return a valid project slug, falling back to default for unknown/blank input."""
    if not slug:
        return DEFAULT_PROJECT
    slug = slugify(slug)
    known = {p["slug"] for p in list_projects(settings)}
    return slug if slug in known else DEFAULT_PROJECT
