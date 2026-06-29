"""Accumulated token/cost usage for a provider over a run.

Thread-safe: parallel subtasks share one provider, so ``add`` is guarded by a lock and
``snapshot``/``delta`` let the engine attribute cost to individual phases/subtasks.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0.0) -> None:
        with self._lock:
            self.input_tokens += input_tokens or 0
            self.output_tokens += output_tokens or 0
            self.cost_usd += cost_usd or 0.0
            self.calls += 1

    def snapshot(self) -> dict[str, float | int]:
        """A point-in-time copy of the totals (for computing per-phase deltas)."""
        with self._lock:
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost_usd": self.cost_usd,
                "calls": self.calls,
            }

    @staticmethod
    def delta(before: dict[str, float | int], after: dict[str, float | int]) -> dict[str, float | int]:
        return {
            "input_tokens": int(after["input_tokens"] - before["input_tokens"]),
            "output_tokens": int(after["output_tokens"] - before["output_tokens"]),
            "cost_usd": round(float(after["cost_usd"]) - float(before["cost_usd"]), 6),
            "calls": int(after["calls"] - before["calls"]),
        }

    def to_dict(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost_usd": round(self.cost_usd, 6),
                "calls": self.calls,
            }
