"""Accumulated token/cost usage for a provider over a run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0.0) -> None:
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        self.cost_usd += cost_usd or 0.0
        self.calls += 1

    def to_dict(self) -> dict[str, float | int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }
