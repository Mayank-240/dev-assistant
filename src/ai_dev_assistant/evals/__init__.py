"""Evaluation harness (Tier 5): golden tasks + deterministic graders + a scorecard, so
output quality is actually measured (the only e2e otherwise drives a fake provider)."""

from .graders import GraderResult, ast_defines, file_exists, tests_pass  # noqa: F401
from .harness import GOLDEN, Scorecard, run_eval  # noqa: F401
