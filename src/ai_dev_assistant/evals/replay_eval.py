"""Offline replay eval (E4): committed cassettes drive a full engine run with no network.

Cassettes under ``evals/cassettes/`` are generated OFFLINE by running the normal harness
against a scripted deterministic provider (mirroring tests/test_engine_e2e.py's
FakeProvider) wrapped in RecordingProvider:

    uv run python -m ai_dev_assistant.evals.replay_eval --record

and committed to the repo. CI (and tests/test_evals.py) then replays them: with
``settings.replay_dir`` set, ``llm/factory.get_provider`` returns a ReplayProvider that
serves every structured()/run_agent call from the cassettes — the whole
plan → schedule → review → summarize → reflect pipeline runs deterministically offline.

Determinism contract: every knob that shapes a prompt (and therefore a cassette key) is
pinned by ``replay_base_settings`` — fresh per-attempt data dirs (no prior lessons, no
track record, no feedback), no repo binding, no objective signals, hash embeddings.
Nothing machine-specific (paths, run ids, timestamps) reaches any prompt.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..config import Settings
from ..llm.errors import LLMError
from ..llm.record_replay import RecordingProvider
from ..llm.schemas import BriefDoc, Plan, RunLessons, SubTask, Verdict
from .harness import CASSETTES_DIR, EvalReport, GoldenTask, run_eval_sync

# The toy golden task the committed cassettes cover. Graderless on purpose: the replayed
# agents write no files (ReplayProvider serves text, not tool calls), so the scorecard is
# judged on the run itself — rollup status, subtask outcomes, routing (see harness).
REPLAY_TASK = GoldenTask(
    id="replay_greeting",
    prompt=("Design a tiny greeting feature: research the approach, implement the change, "
            "and document it."),
    graders=[],
    expected_agents=("researcher", "coder", "documenter"),
)


class _Usage:
    def __init__(self) -> None:
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": self.cost_usd}

    def snapshot(self) -> dict:
        return {**self.to_dict(), "calls": 0}


class ScriptedReplayProvider:
    """Deterministic provider mirroring tests/test_engine_e2e.py's FakeProvider.

    Used only to RECORD cassettes: fixed plan, verdicts, brief, lessons, and per-agent
    result texts keyed off the subtask title in the prompt. It never touches the
    workspace, so recorded reviewer prompts match the (empty) workspace replay sees.
    """

    _AGENT_SCRIPT = [
        ("Research approach", "Research: a plain greet(name) function is the simplest viable "
                              "approach; no external dependencies needed."),
        ("Implement change", "Implemented: greet(name) returns a friendly greeting string, "
                             "with input normalization."),
        ("Document it", "Documented: usage and rationale for the greeting feature are "
                        "written up for the team."),
    ]

    def __init__(self) -> None:
        self.usage = _Usage()

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(
                title="Tiny Greeting Feature",
                summary="Two independent subtasks run in parallel, then a documentation step.",
                subtasks=[
                    SubTask(id="s1", title="Research approach", description="Investigate options.",
                            agent="researcher", rationale="needs analysis", depends_on=[],
                            acceptance_criteria=["analysis provided"]),
                    SubTask(id="s2", title="Implement change", description="Write the code.",
                            agent="coder", rationale="needs code", depends_on=[],
                            acceptance_criteria=["code provided"]),
                    SubTask(id="s3", title="Document it", description="Write the docs.",
                            agent="documenter", rationale="needs docs", depends_on=["s1", "s2"],
                            acceptance_criteria=["docs written"]),
                ],
            )
        if schema is Verdict:
            return Verdict(passed=True, score=95, reasons=["meets all criteria"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Researched, implemented, and documented the greeting feature.",
                            key_points=["analysis done", "code written", "docs produced"],
                            status="completed")
        if schema is RunLessons:
            return RunLessons(summary="Offline replay smoke run completed cleanly.",
                              what_worked=["parallel decomposition of independent subtasks"],
                              what_to_avoid=[], routing_notes=[])
        raise LLMError(f"scripted provider has no response for schema {schema.__name__}")

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=None, workdir=None,
                        on_step=None):
        if on_step:
            on_step({"kind": "text", "text": "working…"})
        for marker, text in self._AGENT_SCRIPT:
            if marker in prompt:
                return text
        return "Done — satisfies the acceptance criteria."

    async def aclose(self) -> None:
        return None


def replay_base_settings(**overrides) -> Settings:
    """Settings whose prompt-affecting knobs are pinned so cassette keys match between
    record and replay (and across machines/CI)."""
    return Settings(
        llm_backend="anthropic",   # never constructed for real: no key, and replay bypasses it
        anthropic_api_key="",
        embeddings_backend="hash",
        verify_run_tests=False, objective_review=False, lint_check=False,
        adaptive_replan=False, git_finalize=False, max_retries=0,
        **overrides,
    )


def record_cassettes(cassette_dir: Path | None = None) -> EvalReport:
    """Regenerate the committed cassettes offline (scripted provider + RecordingProvider)."""
    target = Path(cassette_dir or CASSETTES_DIR)
    target.mkdir(parents=True, exist_ok=True)
    return run_eval_sync(
        replay_base_settings(), tasks=[REPLAY_TASK], repeat=1, task_timeout=120.0,
        provider_factory=lambda: RecordingProvider(ScriptedReplayProvider(), str(target)))


def run_replay_eval(cassette_dir: Path | None = None, *, repeat: int = 1) -> EvalReport:
    """Run the replay golden task fully offline over the committed cassettes."""
    base = replay_base_settings(replay_dir=str(cassette_dir or CASSETTES_DIR))
    return run_eval_sync(base, tasks=[REPLAY_TASK], repeat=repeat, task_timeout=120.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m ai_dev_assistant.evals.replay_eval",
        description="Offline, deterministic eval over committed replay cassettes (E4).")
    ap.add_argument("--record", action="store_true",
                    help="Regenerate the cassettes (offline, scripted provider) instead of replaying")
    ap.add_argument("--cassette-dir", default="",
                    help=f"Cassette directory (default: {CASSETTES_DIR})")
    ap.add_argument("--record-history", action="store_true",
                    help="Append the aggregate scores to <data_dir>/benchmarks.jsonl "
                         "(also enabled by ADA_EVAL_RECORD_HISTORY=1)")
    args = ap.parse_args(argv)
    cdir = Path(args.cassette_dir) if args.cassette_dir else None
    report = record_cassettes(cdir) if args.record else run_replay_eval(cdir)
    print(report.summary())
    record_history = args.record_history or (
        os.getenv("ADA_EVAL_RECORD_HISTORY", "").strip().lower() in {"1", "true", "yes"})
    if record_history:  # opt-in only — default output/behavior stays byte-identical
        from .history import history_path, record_result
        settings = Settings.load()
        suite = "record-cassettes" if args.record else "replay"
        record_result(settings, suite, report)
        print(f"Recorded benchmark entry ({suite}) -> {history_path(settings)}",
              file=sys.stderr)
    return 0 if report.passed == len(report.cards) else 1


if __name__ == "__main__":
    sys.exit(main())
