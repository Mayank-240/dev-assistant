"""Roster construction + user-defined custom agents.

Built-ins: every spec builds, tool lists are valid toolbox names, personas are
non-empty, names are unique. Customs: <data_dir>/custom_agents.json round-trip,
the validation table (collision, unknown tool, bad effort, corrupt file), and
build_agents composition — routing (role_models) and per-role effort apply to
customs, and the orchestrator's roster catalog includes them.
"""

from __future__ import annotations

import json
import logging

import pytest

from ai_dev_assistant.agents import custom as custom_mod
from ai_dev_assistant.agents.custom import (
    CustomSpec,
    delete_custom_agent,
    list_custom_agents,
    load_custom_specs,
    save_custom_agent,
    toolbox_tool_names,
)
from ai_dev_assistant.agents.registry import (
    _COLLAB,
    _SPECS,
    build_agents,
    builtin_agent_names,
    capability_catalog,
)
from ai_dev_assistant.config import Settings


def _settings(tmp_path, **kw) -> Settings:
    return Settings(data_dir=tmp_path / "data", **kw)


def _spec(**overrides) -> dict:
    base = {
        "name": "cassette_wrangler",
        "description": "Curates replay cassettes.",
        "when_to_use": "Use for cassette curation tasks.",
        "system_prompt": "You are a Cassette Wrangler agent. You curate cassettes.",
        "tools": ["read_file", "list_dir", "grep"],
        "effort": "",
        "model": "",
    }
    base.update(overrides)
    return base


# ---- built-in roster ----

def test_builtin_specs_have_unique_names():
    names = [s.name for s in _SPECS]
    assert len(names) == len(set(names))


def test_builtin_specs_build_with_valid_tools_and_nonempty_personas():
    agents = build_agents(_settings_pathless())
    valid_tools = toolbox_tool_names()
    for spec in _SPECS:
        agent = agents[spec.name]
        assert spec.prompt.strip(), spec.name
        assert spec.description.strip(), spec.name
        assert spec.when_to_use.strip(), spec.name
        assert agent.system_prompt.startswith(spec.prompt)
        assert agent.system_prompt.endswith(_COLLAB)
        assert agent.profile.tools, spec.name
        assert set(agent.profile.tools) <= valid_tools, spec.name


def _settings_pathless() -> Settings:
    # A data_dir that cannot contain a customs file: build_agents must still work.
    return Settings(data_dir=__import__("pathlib").Path("/nonexistent-ada-data"))


def test_ux_reviewer_is_a_readonly_reviewer_role():
    agents = build_agents(_settings_pathless())
    ux = agents["ux_reviewer"]
    assert "write_file" not in ux.profile.tools
    assert "run_command" not in ux.profile.tools
    assert "read_file" in ux.profile.tools


def test_builtin_agent_names_includes_reviewer_key():
    names = builtin_agent_names()
    assert "reviewer" in names
    assert {s.name for s in _SPECS} <= names


# ---- custom store: round-trip ----

def test_save_list_load_round_trip(tmp_path):
    s = _settings(tmp_path)
    saved = save_custom_agent(s, _spec(effort="high", model="claude-haiku-4-5"))
    assert isinstance(saved, CustomSpec)
    listed = list_custom_agents(s)
    assert [c.name for c in listed] == ["cassette_wrangler"]
    assert listed == load_custom_specs(s)
    c = listed[0]
    assert c.system_prompt == _spec()["system_prompt"]
    assert c.tools == ["read_file", "list_dir", "grep"]
    assert c.effort == "high" and c.model == "claude-haiku-4-5"


def test_save_upserts_by_name(tmp_path):
    s = _settings(tmp_path)
    save_custom_agent(s, _spec())
    save_custom_agent(s, _spec(description="Updated description."))
    listed = list_custom_agents(s)
    assert len(listed) == 1
    assert listed[0].description == "Updated description."


def test_delete_custom_agent(tmp_path):
    s = _settings(tmp_path)
    save_custom_agent(s, _spec())
    assert delete_custom_agent(s, "cassette_wrangler") is True
    assert list_custom_agents(s) == []
    assert delete_custom_agent(s, "cassette_wrangler") is False


def test_save_accepts_customspec_instance(tmp_path):
    s = _settings(tmp_path)
    save_custom_agent(s, CustomSpec(**_spec()))
    assert [c.name for c in list_custom_agents(s)] == ["cassette_wrangler"]


# ---- custom store: validation table ----

def _write_entries(s: Settings, entries) -> None:
    s.data_dir.mkdir(parents=True, exist_ok=True)
    (s.data_dir / custom_mod.CUSTOM_AGENTS_FILENAME).write_text(
        json.dumps(entries), encoding="utf-8")


@pytest.mark.parametrize("bad", [
    _spec(name="coder"),                       # collision with a built-in
    _spec(name="reviewer"),                    # collision with the reviewer key
    _spec(name="Not A Slug!"),                 # not a slug
    _spec(name=""),                            # empty name
    _spec(tools=["no_such_tool"]),             # unknown tool
    _spec(tools=[]),                           # no tools at all
    _spec(effort="ultra"),                     # bad effort tier
    _spec(system_prompt="   "),                # blank persona
    _spec(description=None),                   # wrong type
    "not-an-object",                           # entry is not a dict
])
def test_load_skips_invalid_entries_with_warning(tmp_path, caplog, bad):
    s = _settings(tmp_path)
    _write_entries(s, [bad, _spec(name="survivor")])
    with caplog.at_level(logging.WARNING, logger="ada.agents"):
        specs = load_custom_specs(s)
    assert [c.name for c in specs] == ["survivor"]
    assert any("skipping entry" in r.message for r in caplog.records)


def test_load_skips_duplicate_custom_names(tmp_path, caplog):
    s = _settings(tmp_path)
    _write_entries(s, [_spec(), _spec(description="Second copy.")])
    with caplog.at_level(logging.WARNING, logger="ada.agents"):
        specs = load_custom_specs(s)
    assert len(specs) == 1
    assert specs[0].description == _spec()["description"]  # first wins


def test_missing_file_yields_no_customs(tmp_path):
    assert load_custom_specs(_settings(tmp_path)) == []


@pytest.mark.parametrize("payload", ["{not json", '{"a": 1}', '"just a string"'])
def test_corrupt_or_wrong_shape_file_yields_no_customs(tmp_path, payload):
    s = _settings(tmp_path)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    (s.data_dir / custom_mod.CUSTOM_AGENTS_FILENAME).write_text(payload, encoding="utf-8")
    assert load_custom_specs(s) == []  # never raises


@pytest.mark.parametrize("bad", [
    _spec(name="coder"),
    _spec(tools=["no_such_tool"]),
    _spec(effort="ultra"),
])
def test_save_rejects_invalid_spec_with_valueerror(tmp_path, bad):
    s = _settings(tmp_path)
    with pytest.raises(ValueError):
        save_custom_agent(s, bad)
    assert list_custom_agents(s) == []


def test_save_normalizes_name_case(tmp_path):
    s = _settings(tmp_path)
    save_custom_agent(s, _spec(name="  Cassette_Wrangler "))
    assert [c.name for c in list_custom_agents(s)] == ["cassette_wrangler"]


# ---- build_agents composition ----

def test_build_agents_composes_builtins_and_customs(tmp_path):
    s = _settings(tmp_path)
    save_custom_agent(s, _spec())
    agents = build_agents(s)
    assert "coder" in agents and "cassette_wrangler" in agents
    c = agents["cassette_wrangler"]
    # Author's system_prompt verbatim + the shared collaboration/safety preamble,
    # matching how built-ins are constructed.
    assert c.system_prompt == _spec()["system_prompt"] + " " + _COLLAB
    assert c.profile.tools == ["read_file", "list_dir", "grep"]


def test_custom_inherits_default_model_and_effort_when_blank(tmp_path):
    s = _settings(tmp_path)
    save_custom_agent(s, _spec())
    c = build_agents(s)["cassette_wrangler"]
    assert c.model == s.agent_model
    assert c.profile.effort == s.agent_effort


def test_custom_own_effort_and_model_apply(tmp_path):
    s = _settings(tmp_path)
    save_custom_agent(s, _spec(effort="xhigh", model="custom-model-1"))
    c = build_agents(s)["cassette_wrangler"]
    assert c.profile.effort == "xhigh"
    assert c.model == "custom-model-1"


def test_role_models_routing_wins_for_customs(tmp_path):
    s = _settings(tmp_path, role_models="cassette_wrangler=routed-model")
    save_custom_agent(s, _spec(model="stored-model"))
    assert build_agents(s)["cassette_wrangler"].model == "routed-model"


def test_role_models_custom_name_not_warned_as_unknown(tmp_path, caplog):
    s = _settings(tmp_path, role_models="cassette_wrangler=m1,unknown_role=m2")
    save_custom_agent(s, _spec())
    with caplog.at_level(logging.WARNING, logger="ada.agents"):
        build_agents(s)
    messages = [r.message for r in caplog.records if "unknown role" in r.message]
    assert any("unknown role 'unknown_role'" in m for m in messages)
    assert not any("unknown role 'cassette_wrangler'" in m for m in messages)


def test_invalid_custom_never_shadows_builtin(tmp_path):
    s = _settings(tmp_path)
    _write_entries(s, [_spec(name="coder", system_prompt="I replace the coder.")])
    coder = build_agents(s)["coder"]
    assert "I replace the coder." not in coder.system_prompt


# ---- roster prompt (orchestrator catalog) ----

def test_capability_catalog_includes_customs(tmp_path):
    s = _settings(tmp_path)
    save_custom_agent(s, _spec())
    catalog = capability_catalog(build_agents(s))
    assert "- cassette_wrangler: Curates replay cassettes." in catalog
    assert "Use for cassette curation tasks." in catalog
    assert "- coder:" in catalog
