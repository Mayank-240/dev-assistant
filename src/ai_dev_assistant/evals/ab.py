"""A/B knob-comparison harness: tune config choices with data, not vibes.

``run_ab`` runs the golden suite once per candidate value of ONE ``ADA_*`` environment
knob and aggregates each arm's results (pass rate, quality, cost, wall time) into an
``ABReport`` with a human table and a best-arm verdict.

How a knob is applied (per arm):
  1. ``os.environ[knob] = value`` — the override stays active for the whole arm so any
     code that reads the environment directly (e.g. the harness's own ``ADA_EVAL_*``
     knobs) sees it too. The previous value is ALWAYS restored afterwards, even on error.
  2. With the override active, ``Settings.load()`` re-parses the environment through the
     exact production path, and only the fields the knob actually changed (relative to
     the ambient environment) are copied onto the arm's base Settings. This preserves
     programmatic overrides on ``base`` (data dirs, backend choice, ...) while the knob
     itself goes through the same env-string parsing users rely on.

The knob therefore MUST be an ``ADA_*`` environment variable name (a ``Settings.load``
env name such as ``ADA_AGENT_EFFORT``, or a harness env knob such as ``ADA_EVAL_*``).

``replay=True`` routes every arm through the offline replay eval (E4): the arm's base
becomes ``replay_eval.replay_base_settings`` pointed at the committed cassettes, the
suite is the single ``REPLAY_TASK``, and the knob override is applied on top. This keeps
the A/B machinery itself testable with no network — but only knobs that do not shape
prompts (and therefore cassette keys) can meaningfully replay; see replay_eval's
determinism contract.
"""

from __future__ import annotations

import dataclasses
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from ..config import Settings
from .harness import CASSETTES_DIR, EvalReport, run_eval_sync
from .replay_eval import REPLAY_TASK, replay_base_settings

# Matches run_replay_eval's per-task timeout; replayed runs are fast and deterministic.
_REPLAY_TASK_TIMEOUT = 120.0


@contextmanager
def _env_override(knob: str, value: str) -> Iterator[None]:
    """Set ``os.environ[knob] = value`` for the duration; restore exactly on exit."""
    prev = os.environ.get(knob)
    os.environ[knob] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(knob, None)
        else:
            os.environ[knob] = prev


def _knob_delta(baseline: Settings) -> dict:
    """With a knob override active in the env, the Settings fields it changed.

    Reloads Settings from the environment and diffs against ``baseline`` (loaded from
    the ambient environment WITHOUT the override), so only the knob's own effect is
    captured — not unrelated ambient env/.env state.
    """
    loaded = Settings.load()
    return {f.name: getattr(loaded, f.name)
            for f in dataclasses.fields(Settings)
            if getattr(loaded, f.name) != getattr(baseline, f.name)}


@dataclass
class ABArm:
    """One knob value's aggregated results across the whole suite (all tasks × repeats)."""

    value: str
    report: EvalReport
    pass_rate: float              # passed / graded attempts (attempts with a scorecard)
    mean_quality: float | None    # None when the arm produced no graded attempts
    mean_cost_usd: float
    mean_wall_s: float


def _make_arm(value: str, report: EvalReport) -> ABArm:
    graded = report.cards  # every attempt yields a scorecard; there is no skip state
    n = len(graded)
    return ABArm(
        value=value,
        report=report,
        pass_rate=(sum(1 for c in graded if c.passed) / n) if n else 0.0,
        mean_quality=(sum(c.quality for c in graded) / n) if n else None,
        mean_cost_usd=(sum(c.cost_usd for c in graded) / n) if n else 0.0,
        mean_wall_s=(sum(c.wall_s for c in graded) / n) if n else 0.0,
    )


@dataclass
class ABReport:
    knob: str
    arms: list[ABArm]

    def best(self) -> ABArm:
        """Best arm by pass_rate, then mean quality, then LOWER mean cost (ties: first)."""
        return max(self.arms, key=lambda a: (
            a.pass_rate,
            a.mean_quality if a.mean_quality is not None else float("-inf"),
            -a.mean_cost_usd,
        ))

    def summary(self) -> str:
        width = max(len("value"), *(len(a.value) for a in self.arms))
        lines = [f"A/B eval on {self.knob} ({len(self.arms)} arms)", ""]
        lines.append(f"{'value':<{width}}  pass_rate  quality  mean_cost$  mean_wall_s")
        for a in self.arms:
            quality = f"{a.mean_quality:7.1f}" if a.mean_quality is not None else f"{'n/a':>7}"
            lines.append(f"{a.value:<{width}}  {a.pass_rate:>9.0%}  {quality}  "
                         f"{a.mean_cost_usd:>10.4f}  {a.mean_wall_s:>11.1f}")
        b = self.best()
        quality = f"{b.mean_quality:.1f}" if b.mean_quality is not None else "n/a"
        lines += ["", f"Verdict: best arm is {self.knob}={b.value} "
                      f"(pass_rate {b.pass_rate:.0%}, quality {quality}, "
                      f"${b.mean_cost_usd:.4f}/run)"]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "knob": self.knob,
            "best": self.best().value if self.arms else None,
            "arms": [{
                "value": a.value,
                "pass_rate": round(a.pass_rate, 3),
                "mean_quality": (round(a.mean_quality, 1)
                                 if a.mean_quality is not None else None),
                "mean_cost_usd": round(a.mean_cost_usd, 4),
                "mean_wall_s": round(a.mean_wall_s, 1),
                "report": {
                    "passed": a.report.passed,
                    "attempts": len(a.report.cards),
                    "tasks": [t.to_dict() for t in a.report.tasks],
                },
            } for a in self.arms],
        }


def run_ab(
    base: Settings,
    knob: str,
    values: list[str],
    *,
    only: list[str] | None = None,
    repeat: int = 1,
    task_timeout: float | None = None,
    replay: bool = False,
) -> ABReport:
    """Run the golden suite once per ``values`` entry of one ``ADA_*`` env knob.

    ``knob`` must be an ``ADA_*`` environment variable name; each value is applied via
    the environment (and ``Settings.load()``) for its arm only, and the prior env state
    is restored afterwards — even when a run raises. ``values`` must be a non-empty list
    of distinct strings. ``only``/``repeat``/``task_timeout`` pass straight through to
    ``run_eval_sync``. With ``replay=True`` the arms run fully offline over the
    committed cassettes (see module docstring); ``base`` is then only a signature
    convenience — the cassette settings replace it.
    """
    if not isinstance(knob, str) or not knob.startswith("ADA_"):
        raise ValueError(f"knob must be an ADA_* environment variable name, got {knob!r}")
    if not values:
        raise ValueError("values must be a non-empty list of knob values")
    if not all(isinstance(v, str) for v in values):
        raise ValueError("knob values must be strings (they are written into os.environ)")
    if len(set(values)) != len(values):
        raise ValueError(f"knob values must be distinct, got {values!r}")

    # Ambient-env baseline (no override): diffed against per-arm loads so only the
    # knob's own effect lands on the arm settings.
    baseline = Settings.load()
    arms: list[ABArm] = []
    for value in values:
        with _env_override(knob, value):
            delta = _knob_delta(baseline)
            arm_base = replay_base_settings(replay_dir=str(CASSETTES_DIR)) if replay else base
            settings = dataclasses.replace(arm_base, **delta)
            report = run_eval_sync(
                settings, only,
                repeat=repeat,
                task_timeout=(task_timeout if task_timeout is not None
                              else (_REPLAY_TASK_TIMEOUT if replay else None)),
                tasks=[REPLAY_TASK] if replay else None,
            )
        arms.append(_make_arm(value, report))
    return ABReport(knob=knob, arms=arms)
