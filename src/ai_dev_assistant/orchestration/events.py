"""Structured progress events emitted by the engine.

The CLI renders these as text lines; the web UI serializes them to JSON over a
WebSocket. Using a typed event (instead of bare strings) keeps both consumers in sync.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    type: str  # status | plan | subtask_start | subtask_done | message | sessions | brief | error | done
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "message": self.message, "data": self.data, "ts": self.ts}


def status(message: str, **data: Any) -> Event:
    return Event("status", message, data)
