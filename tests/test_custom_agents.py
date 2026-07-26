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
    _DOD_HEADER,
    _ROLE_TOOLS,
    _SPECS,
    REVIEWER_SYSTEM,
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
        assert _COLLAB in agent.system_prompt
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


# ---- per-role definition of done ----

def test_all_19_personas_have_nonempty_dod_and_prompt_ends_with_it():
    assert len(_SPECS) == 19
    agents = build_agents(_settings_pathless())
    for spec in _SPECS:
        assert spec.dod.strip(), spec.name
        sp = agents[spec.name].system_prompt
        assert _DOD_HEADER in sp, spec.name
        # The checklist is the LAST thing in the prompt — after _COLLAB.
        assert sp.endswith(spec.dod), spec.name
        assert sp.index(_COLLAB) < sp.index(spec.dod), spec.name


@pytest.mark.parametrize("role,needles", [
    ("test_engineer", ["RUN", "weakened", "failed before"]),
    ("security_auditor", ["severity", "file:line", "no findings"]),
    ("documenter", ["reading it back", "ACTUALLY does"]),
    ("coder", ["compiles/imports", "commented-out"]),
    ("debugger", ["root cause", "BEFORE the fix"]),
])
def test_dod_spot_checks(role, needles):
    sp = build_agents(_settings_pathless())[role].system_prompt
    dod = sp[sp.index(_DOD_HEADER):]
    for needle in needles:
        assert needle in dod, (role, needle)


def test_reviewer_prompt_has_dod_block():
    assert "DEFINITION OF DONE" in REVIEWER_SYSTEM
    assert "pass/fail" in REVIEWER_SYSTEM


# ---- per-role toolsets ----

_COORDINATION_FOUR = {"kg_write", "blackboard_write", "remember", "send_message"}
_READ_SIDE_CORE = {"recall", "kb_search", "kg_query", "read_file", "list_dir", "grep",
                   "symbols", "find_references"}


def test_role_toolsets_cover_exactly_the_roster():
    assert set(_ROLE_TOOLS) == {s.name for s in _SPECS}


def test_coordination_four_present_on_every_role():
    # "Read-only" is about FILES, never about coordination: every role — the
    # documenter and the auditors included — must carry the coordination writes.
    for name, agent in build_agents(_settings_pathless()).items():
        assert _COORDINATION_FOUR <= set(agent.profile.tools), name


def test_read_side_core_present_on_every_role():
    for name, agent in build_agents(_settings_pathless()).items():
        assert _READ_SIDE_CORE <= set(agent.profile.tools), name


def test_file_readonly_roles_have_no_file_writes_or_exec():
    agents = build_agents(_settings_pathless())
    readonly = {s.name for s in _SPECS if s.readonly}
    assert readonly == {"security_auditor", "accessibility_auditor", "ux_reviewer"}
    for name in readonly:
        tools = set(agents[name].profile.tools)
        assert not tools & {"write_file", "edit_file", "apply_patch",
                            "run_command", "run_tests", "install_packages"}, name


def test_removed_capabilities_are_actually_absent():
    agents = build_agents(_settings_pathless())
    doc = set(agents["documenter"].profile.tools)
    assert "install_packages" not in doc and "run_command" not in doc
    assert {"write_file", "edit_file"} <= doc  # documenter writes doc files now
    for planner in ("product_manager", "architect", "researcher"):
        tools = set(agents[planner].profile.tools)
        assert not tools & {"install_packages", "run_command", "write_file"}, planner
    # install_packages only where changing the dependency set is the job
    installers = {n for n, a in agents.items() if "install_packages" in a.profile.tools}
    assert installers == {"coder", "test_engineer", "debugger", "devops",
                          "database", "frontend", "integrator", "migrator"}


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


# ---- per-project custom agents ----

def _register_projects(s: Settings, *slugs: str) -> None:
    """Register project slugs directly in the registry file (no git checkout)."""
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.registry_path.write_text(json.dumps(
        [{"slug": "default", "name": "Default", "created_at": 0}]
        + [{"slug": slug, "name": slug, "created_at": 0} for slug in slugs]),
        encoding="utf-8")


def test_project_field_defaults_to_global_and_round_trips(tmp_path):
    s = _settings(tmp_path)
    saved = save_custom_agent(s, _spec())
    assert saved.project == ""
    _register_projects(s, "webapp")
    saved = save_custom_agent(s, _spec(name="scoped_one", project="webapp"))
    assert saved.project == "webapp"
    on_disk = json.loads((s.data_dir / custom_mod.CUSTOM_AGENTS_FILENAME).read_text())
    assert {e["name"]: e["project"] for e in on_disk} == {
        "cassette_wrangler": "", "scoped_one": "webapp"}
    listed = {c.name: c.project for c in list_custom_agents(s)}
    assert listed == {"cassette_wrangler": "", "scoped_one": "webapp"}


def test_save_rejects_unknown_project(tmp_path):
    s = _settings(tmp_path)
    with pytest.raises(ValueError, match="unknown project"):
        save_custom_agent(s, _spec(project="ghost"))
    assert list_custom_agents(s) == []


def test_save_accepts_default_project_scope(tmp_path):
    # 'default' always exists (the registry self-creates it).
    s = _settings(tmp_path)
    assert save_custom_agent(s, _spec(project="default")).project == "default"


def test_load_skips_unknown_project_entry_with_warning(tmp_path, caplog):
    s = _settings(tmp_path)
    _register_projects(s, "webapp")
    _write_entries(s, [_spec(name="ghost_scoped", project="nope"), _spec(name="survivor")])
    with caplog.at_level(logging.WARNING, logger="ada.agents"):
        specs = load_custom_specs(s)
    assert [c.name for c in specs] == ["survivor"]
    assert any("unknown project" in r.message for r in caplog.records)


def test_load_custom_specs_scopes_to_settings_project(tmp_path):
    s = _settings(tmp_path)
    _register_projects(s, "webapp", "api")
    save_custom_agent(s, _spec(name="globby"))
    save_custom_agent(s, _spec(name="web_helper", project="webapp"))
    save_custom_agent(s, _spec(name="api_helper", project="api"))
    # default project: only globals
    assert [c.name for c in load_custom_specs(s)] == ["globby"]
    # webapp: globals + webapp's own — and build_agents scopes naturally
    s_web = Settings(data_dir=tmp_path / "data", project="webapp")
    assert [c.name for c in load_custom_specs(s_web)] == ["globby", "web_helper"]
    agents = build_agents(s_web)
    assert "globby" in agents and "web_helper" in agents and "api_helper" not in agents


def test_list_custom_agents_filter_semantics(tmp_path):
    s = _settings(tmp_path)
    _register_projects(s, "webapp")
    save_custom_agent(s, _spec(name="globby"))
    save_custom_agent(s, _spec(name="web_helper", project="webapp"))
    # None = every scope; "" = globals only; slug = that project's ONLY (no globals)
    assert [c.name for c in list_custom_agents(s)] == ["globby", "web_helper"]
    assert [c.name for c in list_custom_agents(s, project="")] == ["globby"]
    assert [c.name for c in list_custom_agents(s, project="webapp")] == ["web_helper"]
    assert list_custom_agents(s, project="api") == []


def test_names_globally_unique_across_projects(tmp_path, caplog):
    # Same name in two different project scopes: the file is one namespace,
    # so the second entry is skipped on load.
    s = _settings(tmp_path)
    _register_projects(s, "webapp", "api")
    _write_entries(s, [_spec(name="helper", project="webapp"),
                       _spec(name="helper", project="api")])
    with caplog.at_level(logging.WARNING, logger="ada.agents"):
        specs = list_custom_agents(s)
    assert [(c.name, c.project) for c in specs] == [("helper", "webapp")]


def test_save_upsert_by_name_can_move_scope(tmp_path):
    s = _settings(tmp_path)
    _register_projects(s, "webapp")
    save_custom_agent(s, _spec())
    save_custom_agent(s, _spec(project="webapp"))
    listed = list_custom_agents(s)
    assert [(c.name, c.project) for c in listed] == [("cassette_wrangler", "webapp")]


def test_delete_works_regardless_of_scope(tmp_path):
    s = _settings(tmp_path)
    _register_projects(s, "webapp")
    save_custom_agent(s, _spec(project="webapp"))
    assert delete_custom_agent(s, "cassette_wrangler") is True
    assert list_custom_agents(s) == []
