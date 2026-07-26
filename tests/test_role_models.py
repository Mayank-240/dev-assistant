"""Per-role model routing: the role_models setting ("role=model,...") routes cheap
roles (documenter, test_engineer, ...) to a cheaper model at agent-construction time
while unmapped roles keep the single default. Blank = exactly today's behavior.
"""

from __future__ import annotations

import logging

import pytest

from ai_dev_assistant import config
from ai_dev_assistant.agents.registry import build_agents
from ai_dev_assistant.agents.reviewer import Reviewer
from ai_dev_assistant.config import Settings, parse_role_models


# ---- parser ----

def test_parse_valid_mapping():
    assert parse_role_models("documenter=claude-haiku-4-5,test_engineer=claude-sonnet-4-6") == {
        "documenter": "claude-haiku-4-5",
        "test_engineer": "claude-sonnet-4-6",
    }


def test_parse_strips_whitespace_and_lowercases_role():
    assert parse_role_models("  Documenter = claude-haiku-4-5 , coder=m2 ") == {
        "documenter": "claude-haiku-4-5", "coder": "m2"}


def test_parse_last_duplicate_wins():
    assert parse_role_models("coder=a,coder=b") == {"coder": "b"}


def test_parse_empty_and_blank():
    assert parse_role_models("") == {}
    assert parse_role_models("   ") == {}
    assert parse_role_models(",,") == {}


@pytest.mark.parametrize("raw", ["documenter", "=model", "documenter=", "="])
def test_parse_skips_malformed_pair_with_warning(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="ada.config"):
        assert parse_role_models(raw) == {}
    assert any("malformed" in r.message for r in caplog.records)


def test_parse_keeps_good_pairs_next_to_malformed_ones(caplog):
    with caplog.at_level(logging.WARNING, logger="ada.config"):
        out = parse_role_models("documenter=claude-haiku-4-5,broken,coder=m")
    assert out == {"documenter": "claude-haiku-4-5", "coder": "m"}
    assert any("broken" in r.message for r in caplog.records)


def test_settings_role_models_map_property():
    s = Settings(role_models="documenter=claude-haiku-4-5")
    assert s.role_models_map == {"documenter": "claude-haiku-4-5"}
    assert Settings().role_models_map == {}


# ---- registry: routed construction ----

def test_registry_routes_mapped_roles_and_keeps_default_for_rest():
    s = Settings(role_models="documenter=claude-haiku-4-5,test_engineer=claude-sonnet-4-6")
    agents = build_agents(s)
    assert agents["documenter"].model == "claude-haiku-4-5"
    assert agents["test_engineer"].model == "claude-sonnet-4-6"
    for name, agent in agents.items():
        if name not in ("documenter", "test_engineer"):
            assert agent.model == s.agent_model


def test_registry_blank_mapping_is_current_behavior():
    s = Settings()  # role_models defaults to ""
    for agent in build_agents(s).values():
        assert agent.model == s.agent_model


def test_registry_ignores_unknown_role_with_warning(caplog):
    s = Settings(role_models="not_a_role=claude-haiku-4-5,documenter=claude-haiku-4-5")
    with caplog.at_level(logging.WARNING, logger="ada.agents"):
        agents = build_agents(s)
    assert any("not_a_role" in r.message for r in caplog.records)
    assert "not_a_role" not in agents
    assert agents["documenter"].model == "claude-haiku-4-5"
    assert agents["coder"].model == s.agent_model


def test_registry_routing_does_not_change_effort_or_tools():
    plain = build_agents(Settings())
    routed = build_agents(Settings(role_models="documenter=claude-haiku-4-5"))
    for name in plain:
        assert routed[name].profile.effort == plain[name].profile.effort
        assert routed[name].profile.tools == plain[name].profile.tools
        assert routed[name].system_prompt == plain[name].system_prompt


# ---- reviewer: opt-in "reviewer=..." key ----

def test_reviewer_defaults_to_orchestrator_model():
    s = Settings()
    assert Reviewer(s, provider=None)._model == s.orchestrator_model


def test_reviewer_key_routes_reviewer_model():
    s = Settings(role_models="reviewer=claude-sonnet-4-6")
    assert Reviewer(s, provider=None)._model == "claude-sonnet-4-6"


def test_reviewer_key_is_not_warned_as_unknown(caplog):
    with caplog.at_level(logging.WARNING, logger="ada.agents"):
        build_agents(Settings(role_models="reviewer=claude-sonnet-4-6"))
    assert not any("unknown role" in r.message for r in caplog.records)


# ---- settings: env, schema, overlay round-trip ----

def test_env_var_and_default(monkeypatch):
    monkeypatch.delenv("ADA_ROLE_MODELS", raising=False)
    assert Settings.load(overlay=False).role_models == ""
    monkeypatch.setenv("ADA_ROLE_MODELS", "documenter=claude-haiku-4-5")
    assert Settings.load(overlay=False).role_models == "documenter=claude-haiku-4-5"
    assert config.env_var_for("role_models") == "ADA_ROLE_MODELS"


def test_schema_entry_in_llm_group_and_coercion():
    entry = next(e for e in config.SETTINGS_SCHEMA if e["key"] == "role_models")
    assert entry["group"] == "LLM & Models"
    assert entry["type"] == "str"
    assert config.is_editable("role_models")
    assert (config.coerce_setting("role_models", "documenter=claude-haiku-4-5")
            == "documenter=claude-haiku-4-5")
    with pytest.raises(ValueError):
        config.coerce_setting("role_models", {"nested": "dict"})


def test_overlay_wins_over_env_and_delete_reverts(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("ADA_DATA_DIR", str(data))
    monkeypatch.setenv("ADA_ROLE_MODELS", "documenter=env-model")
    assert Settings.load().role_models == "documenter=env-model"
    config.save_overrides(data, {"role_models": "documenter=console-model"})
    assert Settings.load().role_models == "documenter=console-model"
    assert Settings.load(overlay=False).role_models == "documenter=env-model"
    config.save_overrides(data, {"role_models": None})
    assert Settings.load().role_models == "documenter=env-model"
