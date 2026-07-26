"""User-defined custom agents, loaded from ``<data_dir>/custom_agents.json``.

Each entry defines a routable specialist the operator wrote themselves:

    {"name": "my_agent", "description": "...", "when_to_use": "...",
     "system_prompt": "...", "tools": ["read_file", ...],
     "effort": "", "model": ""}

``effort`` and ``model`` are optional overrides; blank means "inherit the
defaults" (``settings.agent_effort`` / ``settings.agent_model``), and the
``role_models`` console setting routes customs exactly like built-ins.

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
    effort: str = ""  # "" = inherit settings.agent_effort
    model: str = ""   # "" = inherit settings.agent_model (role_models still wins)

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


def _validate(entry: object, taken: set[str], tool_names: set[str]) -> CustomSpec | str:
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
    return CustomSpec(
        name=name,
        description=entry["description"].strip(),
        when_to_use=entry["when_to_use"].strip(),
        system_prompt=entry["system_prompt"].strip(),
        tools=list(tools),
        effort=effort,
        model=model.strip(),
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


def load_custom_specs(settings: Settings) -> list[CustomSpec]:
    """The valid custom agent specs from ``<data_dir>/custom_agents.json``.

    Invalid entries are skipped with a logged warning; missing/corrupt file
    means no customs. Never raises.
    """
    tool_names = toolbox_tool_names()
    taken = set(_builtin_names())
    specs: list[CustomSpec] = []
    for entry in _read_raw(settings):
        result = _validate(entry, taken, tool_names)
        if isinstance(result, str):
            logger.warning("custom agents: skipping entry — %s", result)
            continue
        taken.add(result.name)
        specs.append(result)
    return specs


def list_custom_agents(settings: Settings) -> list[CustomSpec]:
    """Alias of load_custom_specs, named for the management (server/UI) surface."""
    return load_custom_specs(settings)


def save_custom_agent(settings: Settings, spec: CustomSpec | dict) -> CustomSpec:
    """Validate and persist one custom agent (upsert by name). Raises ValueError."""
    entry = spec.to_dict() if isinstance(spec, CustomSpec) else dict(spec)
    name = str(entry.get("name", "")).strip().lower()
    # Validate against built-ins only: overwriting an existing custom of the same
    # name is an update, not a collision.
    result = _validate(entry, _builtin_names(), toolbox_tool_names())
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
