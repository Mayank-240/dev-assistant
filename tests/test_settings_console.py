"""Global settings console: the JSON overlay in config.py and the
GET/PATCH/DELETE /api/settings endpoints (no LLM needed).

Precedence under test: console overlay > environment > dataclass default,
with the overlay applied by Settings.load() and rebound live by the server.
"""

from __future__ import annotations

import json

import pytest

from ai_dev_assistant import config
from ai_dev_assistant.config import Settings

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from ai_dev_assistant.web.server import _settings_for, create_app  # noqa: E402

# Fields the console must never list or set.
EXCLUDED_KEYS = {
    "anthropic_api_key", "data_dir", "docs_dir", "workspace_dir", "llm_backend",
    "record_dir", "replay_dir", "protected_paths", "git_branch_prefix",
}


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(_settings(tmp_path), api_token=""))


def _all_fields(payload) -> list[dict]:
    return [f for g in payload["groups"] for f in g["fields"]]


def _field(payload, key) -> dict:
    return next(f for f in _all_fields(payload) if f["key"] == key)


# ---- overlay: load / save / merge / delete ----

def test_load_overrides_missing_and_corrupt_yield_empty(tmp_path):
    assert config.load_overrides(tmp_path) == {}
    (tmp_path / config.OVERRIDES_FILENAME).write_text("{not json")
    assert config.load_overrides(tmp_path) == {}
    (tmp_path / config.OVERRIDES_FILENAME).write_text('["a list"]')  # wrong shape
    assert config.load_overrides(tmp_path) == {}


def test_save_overrides_merges_and_none_deletes(tmp_path):
    config.save_overrides(tmp_path, {"budget_usd": 5, "trace": False})
    config.save_overrides(tmp_path, {"agent_max_turns": 7})       # merge, not replace
    assert config.load_overrides(tmp_path) == {
        "budget_usd": 5, "trace": False, "agent_max_turns": 7}
    config.save_overrides(tmp_path, {"budget_usd": None})         # None deletes the key
    assert config.load_overrides(tmp_path) == {"trace": False, "agent_max_turns": 7}
    config.save_overrides(tmp_path, {"never_there": None})        # deleting absent = no-op
    assert "never_there" not in config.load_overrides(tmp_path)


def test_save_overrides_creates_data_dir(tmp_path):
    target = tmp_path / "deep" / "data"
    config.save_overrides(target, {"trace": True})
    assert json.loads((target / config.OVERRIDES_FILENAME).read_text()) == {"trace": True}


# ---- coercion (types come from the Settings dataclass fields) ----

def test_coerce_setting_types():
    assert config.coerce_setting("trace", "false") is False
    assert config.coerce_setting("trace", 1) is True
    assert config.coerce_setting("agent_max_turns", "12") == 12
    assert config.coerce_setting("agent_max_turns", 12.0) == 12
    assert config.coerce_setting("budget_usd", "2.5") == 2.5
    assert config.coerce_setting("budget_usd", 3) == 3.0
    assert config.coerce_setting("sdk_model", "claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert config.coerce_setting("sandbox", "bwrap") == "bwrap"
    assert config.coerce_setting("memory_scope", "global") == "global"


@pytest.mark.parametrize("key,value", [
    ("agent_max_turns", "not-a-number"),
    ("agent_max_turns", 3.5),          # fractional -> not an int
    ("agent_max_turns", True),         # bool is not an int here
    ("budget_usd", "abc"),
    ("trace", "definitely"),
    ("sandbox", "vm"),                 # not in the choice set
    ("orchestrator_effort", "extreme"),
    ("sdk_model", {"nested": "dict"}),
])
def test_coerce_setting_rejects_bad_values(key, value):
    with pytest.raises(ValueError):
        config.coerce_setting(key, value)


def test_coerce_setting_rejects_unknown_and_excluded_keys():
    with pytest.raises(ValueError):
        config.coerce_setting("no_such_setting", 1)
    for key in EXCLUDED_KEYS:
        assert not config.is_editable(key)
        with pytest.raises(ValueError):
            config.coerce_setting(key, "x")


# ---- apply_overrides + Settings.load precedence ----

def test_apply_overrides_ignores_unknown_and_bad_keys(tmp_path):
    config.save_overrides(tmp_path, {
        "budget_usd": 4.5,
        "agent_max_turns": "not-a-number",     # uncoercible -> ignored
        "anthropic_api_key": "evil",           # excluded -> ignored
        "totally_unknown": True,               # unknown -> ignored
    })
    s = config.apply_overrides(Settings(data_dir=tmp_path))
    assert s.budget_usd == 4.5
    assert s.agent_max_turns == Settings().agent_max_turns
    assert s.anthropic_api_key == ""


def test_settings_load_applies_overlay_over_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("ADA_DATA_DIR", str(data))
    monkeypatch.setenv("ADA_BUDGET_USD", "2.0")
    monkeypatch.delenv("ADA_AGENT_MAX_TURNS", raising=False)
    # env only (no overlay yet)
    assert Settings.load().budget_usd == 2.0
    # console overlay wins over env for overridden keys; env keeps the rest
    config.save_overrides(data, {"budget_usd": 9.0})
    s = Settings.load()
    assert s.budget_usd == 9.0
    assert s.agent_max_turns == Settings().agent_max_turns
    # overlay=False returns the pure env view (what the console calls "env")
    assert Settings.load(overlay=False).budget_usd == 2.0
    # deleting the override reverts to env
    config.save_overrides(data, {"budget_usd": None})
    assert Settings.load().budget_usd == 2.0


def test_sandbox_settings_env_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_DATA_DIR", str(tmp_path / "data"))
    for var in ("ADA_SANDBOX", "ADA_SANDBOX_IMAGE", "ADA_SANDBOX_NET",
                "ADA_SANDBOX_ALLOW_NETWORK"):
        monkeypatch.delenv(var, raising=False)
    s = Settings.load(overlay=False)
    assert (s.sandbox, s.sandbox_image, s.sandbox_allow_network) == ("subprocess", "", False)
    monkeypatch.setenv("ADA_SANDBOX", "container")
    monkeypatch.setenv("ADA_SANDBOX_IMAGE", "python:3.13-slim")
    monkeypatch.setenv("ADA_SANDBOX_NET", "1")  # legacy var: exactly "1" means allow
    s = Settings.load(overlay=False)
    assert (s.sandbox, s.sandbox_image, s.sandbox_allow_network) == (
        "container", "python:3.13-slim", True)
    monkeypatch.setenv("ADA_SANDBOX_NET", "true")  # anything else stays off
    assert Settings.load(overlay=False).sandbox_allow_network is False
    # the console-aligned name works too, and wins over the legacy var
    monkeypatch.setenv("ADA_SANDBOX_ALLOW_NETWORK", "true")
    assert Settings.load(overlay=False).sandbox_allow_network is True


def test_adaptive_replan_defaults_on(tmp_path, monkeypatch):
    # Self-heal is the default: without env/overlay, failed subtasks get a bounded repair.
    monkeypatch.setenv("ADA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ADA_ADAPTIVE_REPLAN", raising=False)
    assert Settings().adaptive_replan is True
    assert Settings.load(overlay=False).adaptive_replan is True
    # env can still turn it off
    monkeypatch.setenv("ADA_ADAPTIVE_REPLAN", "0")
    assert Settings.load(overlay=False).adaptive_replan is False


# ---- GET /api/settings: shape, values, source detection ----

def test_get_settings_shape_and_info(tmp_path):
    c = _client(tmp_path)
    r = c.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    names = [g["name"] for g in body["groups"]]
    assert names == ["LLM & Models", "Guardrails & Budget", "Execution & Safety",
                     "Verification", "Memory & Knowledge", "Observability",
                     "Notifications", "GitHub"]
    fields = _all_fields(body)
    assert {f["key"] for f in fields} == {e["key"] for e in config.SETTINGS_SCHEMA}
    for f in fields:
        assert set(f) == {"key", "label", "help", "type", "choices", "value",
                          "source", "restart_required"}
        assert f["source"] in ("default", "env", "override")
    # choice fields carry their choices; restart_required flags come through
    assert _field(body, "sandbox")["choices"] == ["subprocess", "bwrap", "container", "none"]
    assert _field(body, "embeddings_backend")["restart_required"] is True
    assert _field(body, "trace")["restart_required"] is False
    info = body["info"]
    assert info["llm_backend"] == "anthropic"
    assert str(tmp_path) in info["data_dir"]
    assert isinstance(info["projects"], int)


def test_get_settings_excludes_secrets_and_paths(tmp_path):
    c = _client(tmp_path)
    body = c.get("/api/settings").json()
    keys = {f["key"] for f in _all_fields(body)}
    assert not (keys & EXCLUDED_KEYS)
    assert "anthropic_api_key" not in json.dumps(body)


def test_get_settings_source_detection(tmp_path, monkeypatch):
    monkeypatch.delenv("ADA_SANDBOX_MEM_MB", raising=False)
    monkeypatch.setenv("ADA_AGENT_MAX_TURNS", "24")
    c = _client(tmp_path)
    c.patch("/api/settings", json={"budget_usd": 1.5})
    body = c.get("/api/settings").json()
    assert _field(body, "sandbox_mem_mb")["source"] == "default"
    assert _field(body, "agent_max_turns")["source"] == "env"
    assert _field(body, "budget_usd")["source"] == "override"
    # an env-set key that is ALSO overridden reports override (console wins)
    monkeypatch.setenv("ADA_BUDGET_USD", "0.5")
    assert _field(c.get("/api/settings").json(), "budget_usd")["source"] == "override"


# ---- PATCH: validation, persistence, live rebinding ----

def test_patch_validates_and_persists_and_updates_live_base(tmp_path):
    c = _client(tmp_path)
    r = c.patch("/api/settings", json={"agent_max_turns": 7, "trace": False})
    assert r.status_code == 200
    assert r.json()["settings"] == {"agent_max_turns": 7, "trace": False}
    # persisted to the overlay file in data_dir
    on_disk = config.load_overrides(c.app.state.settings.data_dir)
    assert on_disk["agent_max_turns"] == 7 and on_disk["trace"] is False
    # visible on a follow-up GET as an override
    body = c.get("/api/settings").json()
    f = _field(body, "agent_max_turns")
    assert f["value"] == 7 and f["source"] == "override"
    # the server's live base was rebound -> settings built for a NEW run carry it
    assert c.app.state.base_settings.agent_max_turns == 7
    run_settings = _settings_for(c.app.state.base_settings, None, None)
    assert run_settings.agent_max_turns == 7 and run_settings.trace is False


def test_sandbox_fields_listed_and_patch_round_trips(tmp_path):
    c = _client(tmp_path)
    body = c.get("/api/settings").json()
    exec_group = next(g for g in body["groups"] if g["name"] == "Execution & Safety")
    keys = [f["key"] for f in exec_group["fields"]]
    assert {"sandbox", "sandbox_image", "sandbox_allow_network"} <= set(keys)
    assert _field(body, "sandbox_image")["type"] == "str"
    assert _field(body, "sandbox_allow_network")["type"] == "bool"
    # round-trip: PATCH persists, GET reports the override, live base is rebound
    r = c.patch("/api/settings", json={"sandbox": "container",
                                       "sandbox_image": "python:3.13-slim",
                                       "sandbox_allow_network": True})
    assert r.status_code == 200
    on_disk = config.load_overrides(c.app.state.settings.data_dir)
    assert on_disk == {"sandbox": "container", "sandbox_image": "python:3.13-slim",
                       "sandbox_allow_network": True}
    body = c.get("/api/settings").json()
    for key, value in on_disk.items():
        f = _field(body, key)
        assert f["value"] == value and f["source"] == "override"
    base = c.app.state.base_settings
    assert (base.sandbox, base.sandbox_image, base.sandbox_allow_network) == (
        "container", "python:3.13-slim", True)


def test_patch_coerces_string_inputs(tmp_path):
    c = _client(tmp_path)
    assert c.patch("/api/settings", json={"budget_usd": "2.5"}).status_code == 200
    assert c.app.state.base_settings.budget_usd == 2.5


@pytest.mark.parametrize("payload", [
    {"no_such_setting": 1},
    {"anthropic_api_key": "sk-evil"},      # excluded -> rejected, never persisted
    {"data_dir": "/elsewhere"},
    {"llm_backend": "anthropic"},
    {"agent_max_turns": "not-a-number"},
    {"sandbox": "vm"},
    {},
])
def test_patch_rejects_bad_payloads(tmp_path, payload):
    c = _client(tmp_path)
    r = c.patch("/api/settings", json=payload)
    assert r.status_code == 400
    assert "error" in r.json()
    assert config.load_overrides(c.app.state.settings.data_dir) == {}  # nothing persisted


def test_patch_rejects_mixed_payload_atomically(tmp_path):
    """One bad key rejects the whole PATCH — the good key must not persist."""
    c = _client(tmp_path)
    r = c.patch("/api/settings", json={"trace": False, "sandbox": "vm"})
    assert r.status_code == 400
    assert config.load_overrides(c.app.state.settings.data_dir) == {}
    assert c.app.state.base_settings.trace is True


# ---- DELETE: revert one override ----

def test_delete_reverts_override_and_live_base(tmp_path):
    c = _client(tmp_path)
    base_turns = c.app.state.base_settings.agent_max_turns
    c.patch("/api/settings", json={"agent_max_turns": 5})
    assert c.app.state.base_settings.agent_max_turns == 5
    r = c.delete("/api/settings/agent_max_turns")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert config.load_overrides(c.app.state.settings.data_dir) == {}
    assert c.app.state.base_settings.agent_max_turns == base_turns
    assert _field(c.get("/api/settings").json(), "agent_max_turns")["source"] != "override"


def test_delete_unknown_or_excluded_key_is_400(tmp_path):
    c = _client(tmp_path)
    assert c.delete("/api/settings/no_such_setting").status_code == 400
    assert c.delete("/api/settings/anthropic_api_key").status_code == 400


# ---- schema sanity: every whitelisted key is a real Settings field ----

def test_schema_keys_are_real_dataclass_fields():
    import dataclasses
    names = {f.name for f in dataclasses.fields(Settings)}
    for entry in config.SETTINGS_SCHEMA:
        assert entry["key"] in names
        assert entry["type"] in ("bool", "int", "float", "str", "choice")
        if entry["type"] == "choice":
            assert entry["choices"]
