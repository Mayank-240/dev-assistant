"""The reviewer: verifies a subtask result against its acceptance criteria.

Implemented as a structured-output call (returns a Verdict) rather than a tool loop, so
verification is deterministic to parse and cheap to apply to every subtask.
"""

from __future__ import annotations

from ..config import Settings
from ..llm.provider import LLMProvider
from ..llm.schemas import Verdict
from .registry import REVIEWER_SYSTEM


class Reviewer:
    def __init__(self, settings: Settings, provider: LLMProvider) -> None:
        self._settings = settings
        self._provider = provider

    async def verify(
        self,
        *,
        title: str,
        description: str,
        acceptance_criteria: list[str],
        result: str,
        workspace_files: list[str] | None = None,
        file_contents: str = "",
    ) -> Verdict:
        criteria = "\n".join(f"- {c}" for c in acceptance_criteria) or "- (none specified)"
        files = ("\n".join(f"- {f}" for f in workspace_files) if workspace_files
                 else "(workspace is empty)")
        contents_block = (
            f"\nACTUAL FILE CONTENTS (read these — do not trust the result's claims about them):\n"
            f"{file_contents}\n" if file_contents else ""
        )
        user = (
            f"SUBTASK: {title}\n"
            f"GOAL: {description}\n\n"
            f"ACCEPTANCE CRITERIA:\n{criteria}\n\n"
            f"FILES ACTUALLY PRESENT IN THE WORKSPACE (ground truth — judge file-existence "
            f"criteria against THIS list, not the result's claims):\n{files}\n"
            f"{contents_block}\n"
            f"RESULT TO VERIFY:\n{result}\n\n"
            "Decide whether the result meets every acceptance criterion. When file contents are "
            "provided, judge correctness against them. Fill in the per-criterion breakdown."
        )
        return await self._provider.structured(
            system=REVIEWER_SYSTEM,
            user=user,
            schema=Verdict,
            model=self._settings.orchestrator_model,
            effort=self._settings.reviewer_effort,
            max_tokens=2000,
        )
