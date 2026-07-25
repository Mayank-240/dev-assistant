"""A/B knob-comparison harness tests — fully offline.

Covers: replay-mode A/B over the committed cassettes (two arms, both perfect), summary
table + verdict, env hygiene (restored whether the knob was unset, preset, or a run
raised), input validation, best-arm tie-breaking, and — via a monkeypatched
run_eval_sync — that each arm runs with its own env value and parsed Settings, with
only/repeat/task_timeout passed through.
"""

from __future__ import annotations

import os

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.evals import ab
from ai_dev_assistant.evals.ab import ABArm, ABReport, run_ab
from ai_dev_assistant.evals.harness import EvalReport
from ai_dev_assistant.evals.replay_eval import REPLAY_TASK, replay_base_settings


def test_run_ab_replay_two_arms(monkeypatch):
    """Two arms of a harmless knob over the committed cassettes: both perfect, summary
    names both values plus a verdict, and the env is restored afterwards."""
    monkeypatch.delenv("ADA_TRACE", raising=False)
    report = run_ab(replay_base_settings(), "ADA_TRACE", ["true", "false"], replay=True)

    assert report.knob == "ADA_TRACE"
    assert [a.value for a in report.arms] == ["true", "false"]
    for arm in report.arms:
        assert arm.pass_rate == 1.0
        assert arm.mean_quality == 100.0
        assert arm.mean_cost_usd == 0.0  # replay never touches the network
        assert arm.report.passed == len(arm.report.cards) == 1
        assert arm.report.cards[0].error == ""

    s = report.summary()
    assert "true" in s and "false" in s
    assert "Verdict:" in s

    d = report.to_dict()
    assert d["knob"] == "ADA_TRACE"
    assert [a["value"] for a in d["arms"]] == ["true", "false"]
    assert d["best"] == "true"  # tie on every metric -> first arm wins

    assert "ADA_TRACE" not in os.environ  # override cleaned up


def _fake_run_eval_sync(records):
    def fake(settings, only=None, *, repeat=None, task_timeout=None,
             provider_factory=None, tasks=None):
        records.append({
            "env": os.environ.get("ADA_TRACE"),
            "settings": settings,
            "only": only, "repeat": repeat, "task_timeout": task_timeout, "tasks": tasks,
        })
        return EvalReport()
    return fake


def test_each_arm_sees_its_env_value(monkeypatch):
    """Each arm runs with its own env override active AND a Settings parsed under it;
    only/repeat/task_timeout pass through to run_eval_sync."""
    monkeypatch.delenv("ADA_TRACE", raising=False)
    seen: list[dict] = []
    monkeypatch.setattr(ab, "run_eval_sync", _fake_run_eval_sync(seen))

    base = Settings()  # trace defaults to True
    report = run_ab(base, "ADA_TRACE", ["true", "false"],
                    only=["reverse_string"], repeat=3, task_timeout=9.0)

    assert [r["env"] for r in seen] == ["true", "false"]
    assert [r["settings"].trace for r in seen] == [True, False]
    # base's programmatic fields survive the knob overlay
    assert all(r["settings"].data_dir == base.data_dir for r in seen)
    assert all(r["only"] == ["reverse_string"] and r["repeat"] == 3
               and r["task_timeout"] == 9.0 and r["tasks"] is None for r in seen)
    assert "ADA_TRACE" not in os.environ

    # empty reports degrade gracefully: pass_rate 0, quality unknown, summary renders
    assert all(a.pass_rate == 0.0 and a.mean_quality is None for a in report.arms)
    assert "Verdict:" in report.summary()
    assert report.to_dict()["arms"][0]["mean_quality"] is None


def test_replay_mode_routes_through_cassette_settings(monkeypatch):
    """replay=True swaps base for replay_base_settings + cassette dir + REPLAY_TASK,
    with the knob override applied on top."""
    monkeypatch.delenv("ADA_TRACE", raising=False)
    seen: list[dict] = []
    monkeypatch.setattr(ab, "run_eval_sync", _fake_run_eval_sync(seen))

    run_ab(Settings(), "ADA_TRACE", ["false"], replay=True)
    r = seen[0]
    assert r["tasks"] == [REPLAY_TASK]
    assert r["settings"].replay_dir  # pointed at the committed cassettes
    assert r["settings"].trace is False  # knob applied on top of cassette settings
    assert r["settings"].embeddings_backend == "hash"  # replay determinism pin kept
    assert r["task_timeout"] == 120.0


def test_env_restored_when_knob_preset(monkeypatch):
    monkeypatch.setenv("ADA_TRACE", "preset")
    monkeypatch.setattr(ab, "run_eval_sync", _fake_run_eval_sync([]))
    run_ab(Settings(), "ADA_TRACE", ["true", "false"])
    assert os.environ["ADA_TRACE"] == "preset"


def test_env_restored_when_a_run_raises(monkeypatch):
    monkeypatch.delenv("ADA_TRACE", raising=False)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ab, "run_eval_sync", boom)
    with pytest.raises(RuntimeError, match="boom"):
        run_ab(Settings(), "ADA_TRACE", ["true"])
    assert "ADA_TRACE" not in os.environ


def test_validation_rejects_bad_inputs():
    base = Settings()
    with pytest.raises(ValueError, match="ADA_"):
        run_ab(base, "EVAL_TRACE", ["a", "b"])  # not an ADA_* env name
    with pytest.raises(ValueError, match="non-empty"):
        run_ab(base, "ADA_TRACE", [])
    with pytest.raises(ValueError, match="distinct"):
        run_ab(base, "ADA_TRACE", ["x", "x"])
    with pytest.raises(ValueError, match="strings"):
        run_ab(base, "ADA_TRACE", ["1", 2])  # env values must be strings


def _arm(value: str, pass_rate: float, quality: float | None, cost: float) -> ABArm:
    return ABArm(value=value, report=EvalReport(), pass_rate=pass_rate,
                 mean_quality=quality, mean_cost_usd=cost, mean_wall_s=0.0)


def test_verdict_orders_by_pass_rate_then_quality_then_cost():
    rep = ABReport(knob="ADA_X", arms=[
        _arm("a", 0.5, 99.0, 0.01),   # highest quality but loses on pass_rate
        _arm("b", 1.0, 80.0, 0.20),
        _arm("c", 1.0, 95.0, 0.30),   # quality tie with d, but costlier
        _arm("d", 1.0, 95.0, 0.20),
    ])
    assert rep.best().value == "d"
    assert "ADA_X=d" in rep.summary()
    assert rep.to_dict()["best"] == "d"
    # a None-quality arm never beats a scored arm at equal pass_rate
    rep2 = ABReport(knob="ADA_X", arms=[_arm("n", 1.0, None, 0.0),
                                        _arm("q", 1.0, 10.0, 9.9)])
    assert rep2.best().value == "q"
