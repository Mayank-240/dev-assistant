"""In-process message bus + a thin record of all traffic.

Agents send each other messages via the ``send_message`` tool (point-to-point or
broadcast) and read their inbox via ``read_messages``. The full history is kept so the
documentation step can show how agents collaborated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Message:
    sender: str
    recipient: str | None  # None == broadcast
    content: str
    kind: str = "message"
    ts: float = field(default_factory=time.time)


class MessageBus:
    def __init__(self) -> None:
        self._inboxes: dict[str, list[Message]] = {}
        self._history: list[Message] = []

    def register(self, agent: str) -> None:
        self._inboxes.setdefault(agent, [])

    def send(self, sender: str, recipient: str | None, content: str, kind: str = "message") -> Message:
        msg = Message(sender=sender, recipient=recipient, content=content, kind=kind)
        self._history.append(msg)
        if recipient is None:  # broadcast to everyone except the sender
            for name, inbox in self._inboxes.items():
                if name != sender:
                    inbox.append(msg)
        else:
            self._inboxes.setdefault(recipient, []).append(msg)
        return msg

    def inbox(self, agent: str, *, clear: bool = True) -> list[Message]:
        msgs = self._inboxes.get(agent, [])
        if clear:
            self._inboxes[agent] = []
        return msgs

    @property
    def history(self) -> list[Message]:
        return list(self._history)
