"""In-run control (Tier 5): pause / resume / steer a live run.

Today the only mid-run control is cancel (lose all work). RunControl lets the UI pause a
run between batches (in-flight subtasks drain, then it halts before the next batch) and
inject steer notes that feed the next subtask's context.
"""

from __future__ import annotations

import asyncio


class RunControl:
    def __init__(self) -> None:
        self._resume = asyncio.Event()
        self._resume.set()  # start un-paused
        self._steer: list[str] = []
        self.paused = False

    def pause(self) -> None:
        self.paused = True
        self._resume.clear()

    def resume(self) -> None:
        self.paused = False
        self._resume.set()

    def steer(self, note: str) -> None:
        if note.strip():
            self._steer.append(note.strip())

    def drain_steer(self) -> list[str]:
        notes, self._steer = self._steer, []
        return notes

    async def gate(self) -> None:
        """Awaited by the scheduler before each batch; blocks while paused."""
        await self._resume.wait()
