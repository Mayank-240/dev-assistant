"""Token-budgeted context assembly (Tier 3).

The agent prompt was unbounded string concatenation of the task + plan summary + every
dependency result + prior suggestions — a wide DAG or large dep results silently blew the
context window or inflated cost. This assembles those parts to a budget, truncating the
largest parts first so the most important (task, criteria) survive intact.
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Cheap heuristic: ~4 chars per token. Good enough for budgeting."""
    return max(1, len(text) // 4)


def assemble(parts: list[tuple[str, str]], *, budget_tokens: int = 6000) -> str:
    """Join (label, text) parts, trimming oversized ones to fit ``budget_tokens``.

    Parts are kept in order; when over budget, the largest parts are truncated (not dropped)
    so every section still contributes something.
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
            used -= len(text) - keep
            rendered[i] = (label, text[:keep] + "\n…(truncated to fit context budget)")
    return "\n\n".join(f"{label}:\n{t}" if label else t for label, t in rendered)
