"""Token pricing table → USD, so the API backend can populate cost_usd and the budget
guardrail actually trips.

Prices are USD per 1M tokens (input, output). Unknown models fall back to a conservative
default. Update as pricing changes; this is intentionally a plain table, not a network call.
"""

from __future__ import annotations

# model id (prefix-matched) -> (input $/1M, output $/1M)
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-opus": (15.0, 75.0),
}
_DEFAULT = (3.0, 15.0)


def rate_for(model: str | None) -> tuple[float, float]:
    name = (model or "").lower()
    for prefix, rate in _PRICES.items():
        if name.startswith(prefix):
            return rate
    return _DEFAULT


def cost(model: str | None, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = rate_for(model)
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
