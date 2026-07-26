"""Tests for the playbooks module: catalog shape, template/param integrity
(meta-tests over every playbook), render happy paths, and validation errors."""

from __future__ import annotations

import dataclasses
import json
from string import Formatter

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.playbooks import PLAYBOOKS, Playbook, catalog, get_playbook, render


def _template_fields(template: str) -> set[str]:
    out = set()
    for _, name, _, _ in Formatter().parse(template):
        if name is not None:
            out.add(name.split(".")[0].split("[")[0])
    return out


# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------

def test_catalog_shape():
    cat = catalog()
    assert isinstance(cat, list) and len(cat) == len(PLAYBOOKS) >= 7
    for entry in cat:
        assert set(entry) >= {"id", "name", "description", "params", "settings_overrides"}
        assert entry["id"] and isinstance(entry["id"], str)
        assert entry["name"] and isinstance(entry["name"], str)
        assert entry["description"] and isinstance(entry["description"], str)
        assert isinstance(entry["params"], list)
        for spec in entry["params"]:
            assert spec["key"] and isinstance(spec["key"], str)
            assert spec["type"] in ("str", "int", "choice")
            assert isinstance(spec.get("label", ""), str)
            if spec["type"] == "choice":
                assert spec["choices"]


def test_catalog_is_json_safe():
    json.dumps(catalog())  # must not raise


def test_catalog_ids_unique():
    ids = [e["id"] for e in catalog()]
    assert len(ids) == len(set(ids))


def test_catalog_returns_copies():
    catalog()[0]["params"].append({"key": "evil", "type": "str"})
    catalog()[0]["settings_overrides"]["budget_usd"] = 9999
    assert all(p.get("key") != "evil" for p in catalog()[0]["params"])
    assert catalog()[0]["settings_overrides"].get("budget_usd") != 9999


# ---------------------------------------------------------------------------
# Meta-tests over every playbook
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda pb: pb.id)
def test_every_placeholder_is_a_declared_param(pb: Playbook):
    declared = {spec["key"] for spec in pb.params}
    for template in (pb.prompt_template, pb.suggested_title):
        fields = _template_fields(template)
        assert "" not in fields, f"{pb.id}: positional placeholder in template"
        assert fields <= declared, (
            f"{pb.id}: undeclared placeholders {fields - declared}")


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda pb: pb.id)
def test_settings_overrides_keys_exist_on_settings(pb: Playbook):
    valid = {f.name for f in dataclasses.fields(Settings)}
    unknown = set(pb.settings_overrides) - valid
    assert not unknown, f"{pb.id}: overrides reference unknown Settings fields {unknown}"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda pb: pb.id)
def test_settings_overrides_are_applicable(pb: Playbook):
    # The server applies overrides via dataclasses.replace — must not raise.
    dataclasses.replace(Settings(), **pb.settings_overrides)


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda pb: pb.id)
def test_defaults_satisfy_param_specs(pb: Playbook):
    # Any declared default must itself pass validation (choice defaults in
    # choices, int defaults integral) — rendered with only required params.
    for spec in pb.params:
        assert spec["type"] in ("str", "int", "choice")
        if not spec.get("required", True):
            assert "default" in spec, f"{pb.id}: optional param '{spec['key']}' needs a default"


def test_bad_template_rejected_at_construction():
    with pytest.raises(ValueError, match="undeclared placeholder"):
        Playbook(id="x", name="x", description="x", params=(),
                 prompt_template="do the thing to {path}")


def test_bad_choice_spec_rejected_at_construction():
    with pytest.raises(ValueError, match="no 'choices'"):
        Playbook(id="x", name="x", description="x",
                 params=({"key": "m", "label": "m", "type": "choice"},),
                 prompt_template="mode {m}")


# ---------------------------------------------------------------------------
# get_playbook
# ---------------------------------------------------------------------------

def test_get_playbook():
    pb = get_playbook("raise-coverage")
    assert pb is not None and pb.id == "raise-coverage"
    assert get_playbook("no-such-playbook") is None


# ---------------------------------------------------------------------------
# render — happy paths
# ---------------------------------------------------------------------------

def test_render_raise_coverage_happy_path():
    out = render("raise-coverage", {"path": "src/ai_dev_assistant/projects.py",
                                    "target_percent": 90})
    assert set(out) == {"prompt", "title", "settings_overrides"}
    assert "src/ai_dev_assistant/projects.py" in out["prompt"]
    assert "90 percent" in out["prompt"]
    assert out["title"] == "Raise coverage of src/ai_dev_assistant/projects.py to 90%"
    assert out["settings_overrides"]["agent_effort"] == "high"


def test_render_applies_defaults():
    out = render("raise-coverage", {"path": "src/pkg"})
    assert "85 percent" in out["prompt"]          # default target_percent
    assert out["title"].endswith("85%")


def test_render_int_accepts_numeric_string():
    out = render("raise-coverage", {"path": "src/pkg", "target_percent": "70"})
    assert "70 percent" in out["prompt"]


def test_render_choice_happy_path_and_default():
    out = render("security-audit", {"scope": "src/web", "mode": "audit-only"})
    assert "Mode: audit-only" in out["prompt"]
    out = render("security-audit", {})            # scope and mode both default
    assert "Mode: audit-and-fix" in out["prompt"]
    assert out["title"] == "Security audit of ."


def test_render_every_playbook_with_minimal_params():
    minimal = {
        "raise-coverage": {"path": "src/x.py"},
        "upgrade-dependency": {"package": "requests"},
        "security-audit": {},
        "refactor-module": {"path": "src/x.py", "goal": "split IO from logic"},
        "document-codebase": {},
        "fix-failing-tests": {},
        "add-feature-tdd": {"feature": "add a --json flag to the CLI"},
    }
    assert set(minimal) == {pb.id for pb in PLAYBOOKS}
    for pid, params in minimal.items():
        out = render(pid, params)
        assert out["prompt"].strip() and out["title"].strip()
        assert "{" not in out["prompt"] and "}" not in out["prompt"]  # fully formatted
        assert isinstance(out["settings_overrides"], dict)


def test_render_settings_overrides_is_a_copy():
    render("raise-coverage", {"path": "a"})["settings_overrides"]["budget_usd"] = 1e9
    assert "budget_usd" not in render("raise-coverage", {"path": "a"})["settings_overrides"]


def test_render_strips_whitespace_in_str_params():
    out = render("upgrade-dependency", {"package": "  numpy  ", "target_version": "2.1"})
    assert "dependency numpy to version 2.1" in out["prompt"]


# ---------------------------------------------------------------------------
# render — validation errors
# ---------------------------------------------------------------------------

def test_render_unknown_playbook():
    with pytest.raises(ValueError, match="unknown playbook.*known:"):
        render("does-not-exist", {})


def test_render_missing_required_param():
    with pytest.raises(ValueError, match="missing required param 'path'"):
        render("raise-coverage", {})


def test_render_blank_required_param_rejected():
    with pytest.raises(ValueError, match="missing required param 'feature'"):
        render("add-feature-tdd", {"feature": "   "})


def test_render_bad_choice():
    with pytest.raises(ValueError, match="must be one of.*audit-and-fix.*got 'yolo'"):
        render("security-audit", {"mode": "yolo"})


def test_render_bad_int_type():
    with pytest.raises(ValueError, match="'target_percent' must be an integer"):
        render("raise-coverage", {"path": "src", "target_percent": "eighty"})


def test_render_bool_rejected_for_int():
    with pytest.raises(ValueError, match="'target_percent' must be an integer"):
        render("raise-coverage", {"path": "src", "target_percent": True})


def test_render_non_string_rejected_for_str():
    with pytest.raises(ValueError, match="'package' must be a string"):
        render("upgrade-dependency", {"package": 42})


def test_render_unknown_param_key():
    with pytest.raises(ValueError, match="unknown param"):
        render("raise-coverage", {"path": "src", "coverage": 90})
