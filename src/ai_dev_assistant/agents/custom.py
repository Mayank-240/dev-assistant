"""User-defined custom agents, loaded from ``<data_dir>/custom_agents.json``.

Each entry defines a routable specialist the operator wrote themselves:

    {"name": "my_agent", "description": "...", "when_to_use": "...",
     "system_prompt": "...", "tools": ["read_file", ...],
     "effort": "", "model": "", "project": ""}

``effort`` and ``model`` are optional overrides; blank means "inherit the
defaults" (``settings.agent_effort`` / ``settings.agent_model``), and the
``role_models`` console setting routes customs exactly like built-ins.

``project`` scopes the agent: "" (the default) means global — the agent joins
every run's roster — while a project slug limits it to runs of that project
(``load_custom_specs`` returns globals plus the active ``settings.project``'s
specs, so ``build_agents`` scopes naturally). The slug must name an existing
project: save rejects unknown slugs with ``ValueError``; load skips them with
a warning. All entries live in the single ``custom_agents.json`` file and
names are globally unique across it (a project-scoped name may shadow neither
built-ins nor globals nor another project's custom).

The store is defensive: a missing or corrupt file yields no customs, and every
invalid entry (bad name, collision with a built-in, unknown tool, bad effort)
is skipped with a logged warning — loading never raises. ``save_custom_agent``
is the strict counterpart: it validates up front and raises ``ValueError`` so a
server/UI can surface the problem instead of silently dropping the agent.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Settings

logger = logging.getLogger("ada.agents")

CUSTOM_AGENTS_FILENAME = "custom_agents.json"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


@dataclass(frozen=True)
class CustomSpec:
    name: str
    description: str
    when_to_use: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    effort: str = ""   # "" = inherit settings.agent_effort
    model: str = ""    # "" = inherit settings.agent_model (role_models still wins)
    project: str = ""  # "" = global; else an existing project slug this agent is scoped to

    def to_dict(self) -> dict:
        return asdict(self)


def toolbox_tool_names() -> set[str]:
    """Every tool name the toolbox defines — the universe customs may pick from."""
    from ..tools.registry import _TOOL_DEFS

    return {d["name"] for d in _TOOL_DEFS}


def _builtin_names() -> set[str]:
    from .registry import builtin_agent_names

    return builtin_agent_names()


def _store_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / CUSTOM_AGENTS_FILENAME


def _known_project_slugs(settings: Settings, raw: list) -> set[str]:
    """Project slugs for validating ``project`` fields.

    Computed only when some entry actually carries a project scope, so the
    common all-global path never touches the project registry (and pathless
    settings used in tests keep working). Defensive: never raises.
    """
    if not any(isinstance(e, dict) and str(e.get("project") or "").strip() for e in raw):
        return set()
    from ..projects import list_projects  # lazy: avoid a module-load cycle

    try:
        return {str(p.get("slug", "")) for p in list_projects(settings)}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("custom agents: cannot list projects (%s); "
                       "project-scoped entries will be rejected", exc)
        return set()


def _validate(entry: object, taken: set[str], tool_names: set[str],
              project_slugs: set[str]) -> CustomSpec | str:
    """Return a CustomSpec, or a human-readable rejection reason."""
    if not isinstance(entry, dict):
        return f"entry is not an object: {entry!r}"
    name = entry.get("name")
    if not isinstance(name, str) or not _SLUG_RE.fullmatch(name.strip().lower()):
        return f"name {name!r} is not a slug (lowercase letters/digits/underscores)"
    name = name.strip().lower()
    if name in taken:
        return f"name {name!r} collides with a built-in agent or an earlier custom"
    for key in ("description", "when_to_use", "system_prompt"):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"custom agent {name!r}: {key} must be a non-empty string"
    tools = entry.get("tools")
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
        return f"custom agent {name!r}: tools must be a non-empty list of tool names"
    unknown = [t for t in tools if t not in tool_names]
    if unknown:
        return f"custom agent {name!r}: unknown tools {unknown} (not in the toolbox)"
    effort = entry.get("effort", "") or ""
    if not isinstance(effort, str) or (effort and effort not in _EFFORTS):
        return (f"custom agent {name!r}: effort {effort!r} invalid "
                f"(want blank or one of {sorted(_EFFORTS)})")
    model = entry.get("model", "") or ""
    if not isinstance(model, str):
        return f"custom agent {name!r}: model must be a string"
    project = entry.get("project", "") or ""
    if not isinstance(project, str):
        return f"custom agent {name!r}: project must be a string"
    project = project.strip()
    if project and project not in project_slugs:
        return f"custom agent {name!r}: unknown project {project!r} (not an existing project slug)"
    return CustomSpec(
        name=name,
        description=entry["description"].strip(),
        when_to_use=entry["when_to_use"].strip(),
        system_prompt=entry["system_prompt"].strip(),
        tools=list(tools),
        effort=effort,
        model=model.strip(),
        project=project,
    )


def _read_raw(settings: Settings) -> list:
    """The raw JSON list from disk — [] on missing/corrupt/wrong shape, never raises."""
    path = _store_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("custom agents: cannot read %s (%s); loading none", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("custom agents: %s is not a JSON list; loading none", path)
        return []
    return data


def _all_valid_specs(settings: Settings) -> list[CustomSpec]:
    """Every valid spec in the store, regardless of project scope.

    Invalid entries (including unknown-project scopes and duplicate names —
    names are globally unique across the file) are skipped with a logged
    warning; missing/corrupt file means no customs. Never raises.
    """
    raw = _read_raw(settings)
    tool_names = toolbox_tool_names()
    project_slugs = _known_project_slugs(settings, raw)
    taken = set(_builtin_names())
    specs: list[CustomSpec] = []
    for entry in raw:
        result = _validate(entry, taken, tool_names, project_slugs)
        if isinstance(result, str):
            logger.warning("custom agents: skipping entry — %s", result)
            continue
        taken.add(result.name)
        specs.append(result)
    return specs


def load_custom_specs(settings: Settings) -> list[CustomSpec]:
    """The custom specs in scope for the active project: globals ("") plus
    those whose ``project`` equals ``settings.project`` — so ``build_agents``
    composes exactly the roster the current run should see. Never raises.
    """
    return [s for s in _all_valid_specs(settings)
            if s.project in ("", settings.project)]


def list_custom_agents(settings: Settings, project: str | None = None) -> list[CustomSpec]:
    """The management (server/UI) listing, unscoped by default.

    ``project`` filters the listing: ``None`` (default) returns ALL specs from
    every scope; ``""`` returns only globals; a slug returns only that
    project's own specs — deliberately NOT including globals, so the UI can
    show exactly what a project defines (use ``load_custom_specs`` for the
    effective roster of a run).
    """
    specs = _all_valid_specs(settings)
    if project is None:
        return specs
    return [s for s in specs if s.project == project]


def save_custom_agent(settings: Settings, spec: CustomSpec | dict) -> CustomSpec:
    """Validate and persist one custom agent (upsert by name). Raises ValueError."""
    entry = spec.to_dict() if isinstance(spec, CustomSpec) else dict(spec)
    name = str(entry.get("name", "")).strip().lower()
    # Validate against built-ins only: overwriting an existing custom of the same
    # name is an update, not a collision (upsert is keyed on the name alone,
    # which is what keeps names globally unique across the file).
    result = _validate(entry, _builtin_names(), toolbox_tool_names(),
                       _known_project_slugs(settings, [entry]))
    if isinstance(result, str):
        raise ValueError(result)
    raw = [e for e in _read_raw(settings)
           if not (isinstance(e, dict) and str(e.get("name", "")).strip().lower() == name)]
    raw.append(result.to_dict())
    path = _store_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return result


def delete_custom_agent(settings: Settings, name: str) -> bool:
    """Remove the named custom agent. Returns True if an entry was removed."""
    name = str(name).strip().lower()
    raw = _read_raw(settings)
    kept = [e for e in raw
            if not (isinstance(e, dict) and str(e.get("name", "")).strip().lower() == name)]
    if len(kept) == len(raw):
        return False
    path = _store_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    return True
