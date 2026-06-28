"""The LLM provider interface.

Two backends implement it:
  - AnthropicProvider  — the raw Anthropic API (needs ANTHROPIC_API_KEY).
  - ClaudeSdkProvider  — the Claude Agent SDK, which reuses the user's Claude Code
    login, so it works with no API key.

Agents and the orchestrator depend only on this interface, never on a concrete backend.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, Type, TypeVar

from pydantic import BaseModel

from ..tools.registry import ToolBox
from .errors import LLMError  # re-exported for convenience

T = TypeVar("T", bound=BaseModel)

# A live agent step: {"kind": "text"|"tool"|"thinking", "text"?: str, "tool"?: str, "input"?: str}
StepFn = Callable[[dict[str, Any]], None]

__all__ = ["LLMProvider", "LLMError", "StepFn"]


class LLMProvider(Protocol):
    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: Type[T],
        model: str,
        effort: str | None = None,
        max_tokens: int = 4000,
    ) -> T:
        """Return a validated instance of ``schema`` produced by the model."""
        ...

    async def run_agent(
        self,
        *,
        system_prompt: str,
        prompt: str,
        toolbox: ToolBox,
        allowed_tools: list[str],
        model: str,
        effort: str | None = None,
        max_tokens: int = 8000,
        max_iterations: int | None = None,
        workdir: str | None = None,
        on_step: StepFn | None = None,
    ) -> str:
        """Run an agentic tool-use loop and return the final assistant text.

        If ``on_step`` is given, it is called with each incremental step (text / tool use /
        thinking) as the agent works, for live streaming.
        """
        ...

    async def aclose(self) -> None:
        ...
