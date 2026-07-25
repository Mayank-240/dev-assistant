"""Evaluation harness (Tier 5): golden tasks (incl. repo-level fixtures), deterministic +
held-out graders, repeat/variance metrics, and an offline replay eval over committed
cassettes — so output quality is actually measured."""

from .ab import ABArm, ABReport, run_ab  # noqa: F401
from .graders import GraderResult, ast_defines, file_exists, heldout_tests_pass, tests_pass  # noqa: F401
from .harness import (  # noqa: F401
    CASSETTES_DIR,
    FIXTURES_DIR,
    GOLDEN,
    EvalReport,
    GoldenTask,
    Scorecard,
    TaskSummary,
    run_eval,
    run_eval_sync,
)
