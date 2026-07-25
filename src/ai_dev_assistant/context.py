"""Token-budgeted context assembly (Tier 3).

The agent prompt was unbounded string concatenation of the task + plan summary + every
dependency result + prior suggestions — a wide DAG or large dep results silently blew the
context window or inflated cost. This assembles those parts to a budget, truncating the
largest parts first so the most important (task, criteria) survive intact.

It also owns the unified budget policy (C3): one ``BudgetPolicy`` object keyed off model
family + role via ``budget_for``, instead of magic numbers scattered across call sites.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

# Symbol-density threshold separating "code-heavy" from "prose" text; see estimate_tokens.
_CODE_SYMBOL_DENSITY = 0.08


def estimate_tokens(text: str) -> int:
    """Cheap heuristic token estimate — NOT a real tokenizer.

    Two branches on symbol density (fraction of non-alphanumeric, non-whitespace chars):
    code-heavy text tokenizes denser (~3.5 chars/token) than prose (~4 chars/token),
    because punctuation and operators usually become their own tokens. Good enough for
    budgeting; do not use for billing or hard limits.
    """
    if not text:
        return 1
    n = len(text)
    symbols = sum(1 for ch in text if not (ch.isalnum() or ch.isspace()))
    chars_per_token = 3.5 if (symbols / n) > _CODE_SYMBOL_DENSITY else 4.0
    return max(1, int(n / chars_per_token))


@dataclass(frozen=True)
class BudgetPolicy:
    """Context-size knobs for one (model family, role) combination.

    - context_budget_tokens: budget for ``assemble`` when building an agent prompt
      (already role-adjusted by ``budget_for``).
    - max_part_chars: cap for any single injected part (file content, dep result, ...).
    - reviewer_budget_tokens: the reviewer-role context budget for this family.
    """

    context_budget_tokens: int
    max_part_chars: int
    reviewer_budget_tokens: int


# Per-model-family defaults. Bigger models get bigger context budgets; haiku-class models
# are kept lean since they're used for cheap/fast roles.
_FAMILY_POLICIES: dict[str, BudgetPolicy] = {
    "opus": BudgetPolicy(context_budget_tokens=12000, max_part_chars=12000, reviewer_budget_tokens=8000),
    "sonnet": BudgetPolicy(context_budget_tokens=8000, max_part_chars=8000, reviewer_budget_tokens=6000),
    "haiku": BudgetPolicy(context_budget_tokens=4000, max_part_chars=6000, reviewer_budget_tokens=3000),
}
_DEFAULT_POLICY = BudgetPolicy(context_budget_tokens=8000, max_part_chars=8000, reviewer_budget_tokens=6000)


def budget_for(model: str, role: str = "agent") -> BudgetPolicy:
    """The budget policy for ``model`` in ``role`` ("agent" or "reviewer").

    Family is matched by substring on the model id (opus/sonnet/haiku); unknown models get
    a middle-of-the-road default. For ``role="reviewer"`` the returned policy's
    ``context_budget_tokens`` is the reviewer budget, so callers can read one field
    uniformly. The env var ``ADA_CONTEXT_BUDGET_TOKENS`` overrides
    ``context_budget_tokens`` regardless of model/role.
    """
    name = (model or "").lower()
    policy = _DEFAULT_POLICY
    for family, fam_policy in _FAMILY_POLICIES.items():
        if family in name:
            policy = fam_policy
            break
    if role == "reviewer":
        policy = replace(policy, context_budget_tokens=policy.reviewer_budget_tokens)
    override = os.environ.get("ADA_CONTEXT_BUDGET_TOKENS", "").strip()
    if override:
        try:
            policy = replace(policy, context_budget_tokens=int(override))
        except ValueError:
            pass  # a malformed override must not break runs
    return policy


def _line_boundary_cut(text: str, keep: int) -> int:
    """Largest cut point <= ``keep`` that lands on a line boundary (falls back to ``keep``
    when the part has no usable newline, e.g. one giant line)."""
    cut = text.rfind("\n", 0, keep)
    return cut if cut >= 200 else keep


def assemble(parts: list[tuple[str, str]], *, budget_tokens: int = 6000) -> str:
    """Join (label, text) parts, trimming oversized ones to fit ``budget_tokens``.

    Parts are kept in order; when over budget, the largest parts are truncated (not dropped)
    so every section still contributes something. Truncation breaks at line boundaries
    rather than mid-line, so trimmed code/logs stay parseable.
    """
    rendered = [(label, text or "") for label, text in parts if text]
    total = sum(estimate_tokens(t) for _, t in rendered)
    if total <= budget_tokens:
        return "\n\n".join(f"{label}:\n{t}" if label else t for label, t in rendered)

    # Trim proportionally, largest first, until under budget.
    budget_chars = budget_tokens * 4
    order = sorted(range(len(rendered)), key=lambda i: len(rendered[i][1]), reverse=True)
    used = sum(len(t) for _, t in rendered)
    for i in order:
        if used <= budget_chars:
            break
        label, text = rendered[i]
        # leave each part at least a small head so it's still informative
        keep = max(400, int(len(text) * budget_chars / used))
        if keep < len(text):
            keep = _line_boundary_cut(text, keep)
            used -= len(text) - keep
            rendered[i] = (label, text[:keep] + "\n…(truncated to fit context budget)")
    return "\n\n".join(f"{label}:\n{t}" if label else t for label, t in rendered)
