"""Benchmark history: record/load round-trip, git state capture, corrupt-line
tolerance, trend_report deltas + per-sha series, and CLI/module flag plumbing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ai_dev_assistant.config import Settings
from ai_dev_assistant.evals import history
from ai_dev_assistant.evals.harness import EvalReport, Scorecard
from ai_dev_assistant.evals.history import (
    history_path,
    load_history,
    record_result,
    trend_report,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


def _report(*, passed=2, failed=1, skipped=1, quality=(90.0, 80.0),
            cost=0.5, wall=3.0) -> EvalReport:
    """Fake result matching the real harness shape: an EvalReport of Scorecards."""
    cards: list[Scorecard] = []
    q = list(quality) + [0.0] * (passed + failed)
    for i in range(passed):
        cards.append(Scorecard(task_id=f"p{i}", passed=True, graders=[],
                               quality=q[i], cost_usd=cost, wall_s=wall))
    for i in range(failed):
        cards.append(Scorecard(task_id=f"f{i}", passed=False, graders=[],
                               quality=q[passed + i], cost_usd=cost, wall_s=wall))
    for i in range(skipped):
        cards.append(Scorecard(task_id=f"s{i}", passed=False, graders=[], skipped=True,
                               quality=0.0, cost_usd=0.0, wall_s=0.0))
    return EvalReport(cards=cards)


def _git(args, cwd):
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


# ---------------------------------------------------------------- record / load


def test_record_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # not a git repo -> sha "", never raises
    settings = _settings(tmp_path)
    report = _report(passed=2, failed=1, skipped=1, quality=(90.0, 80.0, 40.0))
    entry = record_result(settings, "golden", report)

    assert set(entry) == {"ts", "git_sha", "dirty", "suite", "pass_rate", "quality_mean",
                          "quality_min", "cost_usd", "duration_s", "runs"}
    assert entry["suite"] == "golden"
    assert entry["runs"] == 3                       # skipped card excluded
    assert entry["pass_rate"] == round(2 / 3, 4)    # over attempted cards only
    assert entry["quality_mean"] == 70.0            # mean(90, 80, 40)
    assert entry["quality_min"] == 40.0
    assert entry["cost_usd"] == 1.5                 # summed over all cards
    assert entry["duration_s"] == 9.0
    assert "T" in entry["ts"]                       # iso timestamp

    assert history_path(settings) == settings.data_dir / "benchmarks.jsonl"
    assert history_path(settings).is_file()
    loaded = load_history(settings)
    assert loaded == [entry]                        # JSON round-trips exactly

    # empty report -> neutral zeros, still records
    empty = record_result(settings, "golden", EvalReport())
    assert empty["pass_rate"] == 0.0 and empty["runs"] == 0
    assert len(load_history(settings)) == 2


def test_load_history_suite_filter_and_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)
    for i in range(5):
        record_result(settings, "golden" if i % 2 == 0 else "replay", _report())
    assert [e["suite"] for e in load_history(settings, suite="replay")] == ["replay"] * 2
    assert len(load_history(settings, limit=3)) == 3
    got = load_history(settings, suite="golden", limit=2)
    assert len(got) == 2 and all(e["suite"] == "golden" for e in got)


# ------------------------------------------------------------------- git state


def test_git_sha_captured_in_repo_and_dirty_flag(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    (repo / "a.txt").write_text("hello\n")
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=T", "commit", "-q", "-m", "one"], repo)
    monkeypatch.chdir(repo)

    settings = _settings(tmp_path)
    entry = record_result(settings, "golden", _report())
    assert entry["git_sha"] == _git(["rev-parse", "--short", "HEAD"], repo)
    assert entry["dirty"] is False

    (repo / "b.txt").write_text("uncommitted\n")
    entry = record_result(settings, "golden", _report())
    assert entry["dirty"] is True


def test_git_probe_never_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # tmp_path is outside any repo
    settings = _settings(tmp_path)
    entry = record_result(settings, "golden", _report())
    assert entry["git_sha"] == "" and entry["dirty"] is False

    def boom(*args, **kwargs):
        raise OSError("git exploded")

    monkeypatch.setattr(history.subprocess, "run", boom)
    entry = record_result(settings, "golden", _report())
    assert entry["git_sha"] == "" and entry["dirty"] is False


# ------------------------------------------------------------ corrupt tolerance


def test_load_history_skips_corrupt_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)
    good = record_result(settings, "golden", _report())
    with history_path(settings).open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
        fh.write('"a bare string, not an object"\n')
        fh.write("\n")
    good2 = record_result(settings, "golden", _report())
    assert load_history(settings) == [good, good2]


def test_load_history_missing_file(tmp_path):
    assert load_history(_settings(tmp_path)) == []
    assert trend_report(_settings(tmp_path)) == {"latest": None, "delta": None, "series": []}


# ---------------------------------------------------------------- trend_report


def test_trend_report_deltas_and_series(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    shas = iter([("aaa1111", False), ("aaa1111", True), ("bbb2222", False)])
    monkeypatch.setattr(history, "_git_state", lambda cwd=None: next(shas))

    record_result(settings, "golden", _report(passed=1, failed=1, skipped=0,
                                              quality=(60.0, 40.0), cost=1.0))
    record_result(settings, "golden", _report(passed=1, failed=1, skipped=0,
                                              quality=(70.0, 50.0), cost=1.0))
    record_result(settings, "golden", _report(passed=2, failed=0, skipped=0,
                                              quality=(90.0, 80.0), cost=0.5))

    rep = trend_report(settings, suite="golden")
    assert rep["latest"]["git_sha"] == "bbb2222"
    assert rep["latest"]["pass_rate"] == 1.0
    assert rep["delta"] == {"pass_rate": 0.5,       # 1.0 - 0.5
                            "quality_mean": 25.0,   # 85 - 60
                            "quality_min": 30.0,    # 80 - 50
                            "cost_usd": -1.0}       # 1.0 - 2.0
    # per-sha latest: aaa1111's SECOND entry (quality_mean 60) then bbb2222
    assert [p["sha"] for p in rep["series"]] == ["aaa1111", "bbb2222"]
    assert rep["series"][0]["quality_mean"] == 60.0
    assert rep["series"][1] == {"sha": "bbb2222", "ts": rep["latest"]["ts"],
                                "pass_rate": 1.0, "quality_mean": 85.0, "cost_usd": 1.0}


def test_trend_report_single_entry_and_suite_isolation(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(history, "_git_state", lambda cwd=None: ("ccc3333", False))
    record_result(settings, "replay", _report())
    record_result(settings, "golden", _report())
    # latest overall is 'golden'; only one golden entry -> no delta, 1-point series
    rep = trend_report(settings)
    assert rep["latest"]["suite"] == "golden"
    assert rep["delta"] is None
    assert len(rep["series"]) == 1 and rep["series"][0]["sha"] == "ccc3333"


# ------------------------------------------------------------------ CLI plumbing


def test_eval_parser_accepts_record_history():
    from ai_dev_assistant.cli import _build_parser
    args = _build_parser().parse_args(["eval", "--replay", "--record-history"])
    assert args.record_history is True
    assert _build_parser().parse_args(["eval"]).record_history is False


def test_cli_replay_records_history_with_flag(tmp_path, monkeypatch, capsys):
    """`ada eval --replay --record-history` appends a 'replay' entry; without the
    flag nothing is written (suite runner stubbed — no engine run)."""
    from ai_dev_assistant import cli
    from ai_dev_assistant.evals import replay_eval

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(replay_eval, "run_replay_eval",
                        lambda d=None, repeat=1: _report(passed=1, failed=0, skipped=0,
                                                         quality=(100.0,)))

    rc = cli.main(["eval", "--replay"])
    assert rc == 0
    assert not (tmp_path / "data" / "benchmarks.jsonl").exists()

    rc = cli.main(["eval", "--replay", "--record-history"])
    assert rc == 0
    entries = [json.loads(line) for line in
               (tmp_path / "data" / "benchmarks.jsonl").read_text().splitlines()]
    assert [e["suite"] for e in entries] == ["replay"]
    assert entries[0]["pass_rate"] == 1.0
    assert "Recorded benchmark entry (replay)" in capsys.readouterr().err


def test_cli_live_suite_records_golden_history(tmp_path, monkeypatch, capsys):
    """The live-suite path records under suite 'golden' (runner stubbed)."""
    from ai_dev_assistant import cli
    from ai_dev_assistant.evals import harness

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADA_LLM_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
    monkeypatch.setattr(harness, "run_eval_sync",
                        lambda settings, only=None, repeat=None, task_timeout=None:
                        _report(passed=1, failed=0, skipped=0, quality=(88.0,)))

    rc = cli.main(["eval", "--record-history"])
    assert rc == 0
    entries = [json.loads(line) for line in
               (tmp_path / "data" / "benchmarks.jsonl").read_text().splitlines()]
    assert [e["suite"] for e in entries] == ["golden"]
    assert entries[0]["quality_mean"] == 88.0
    assert "Recorded benchmark entry (golden)" in capsys.readouterr().err


def test_replay_eval_module_flag_and_env(tmp_path, monkeypatch, capsys):
    """`python -m ai_dev_assistant.evals.replay_eval --record-history` (or
    ADA_EVAL_RECORD_HISTORY=1) records; default records nothing."""
    from ai_dev_assistant.evals import replay_eval

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ADA_EVAL_RECORD_HISTORY", raising=False)
    monkeypatch.setattr(replay_eval, "run_replay_eval",
                        lambda d=None: _report(passed=1, failed=0, skipped=0,
                                               quality=(100.0,)))

    assert replay_eval.main([]) == 0
    assert not (tmp_path / "data" / "benchmarks.jsonl").exists()

    assert replay_eval.main(["--record-history"]) == 0
    hist = tmp_path / "data" / "benchmarks.jsonl"
    assert len(hist.read_text().splitlines()) == 1

    monkeypatch.setenv("ADA_EVAL_RECORD_HISTORY", "1")
    assert replay_eval.main([]) == 0
    entries = [json.loads(line) for line in hist.read_text().splitlines()]
    assert [e["suite"] for e in entries] == ["replay", "replay"]
    capsys.readouterr()
