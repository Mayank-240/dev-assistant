"""The LLM provider interface.

Two backends implement it:
  - AnthropicProvider  — the raw Anthropic API (needs ANTHROPIC_API_KEY).
  - ClaudeSdkProvider  — the Claude Agent SDK, which reuses the user's Claude Code
    login, so it works with no API key.

Agents and the orchestrator depend only on this interface, never on a concrete backend.

The llm layer also owns ``ToolDispatcher``: the minimal structural interface providers
need from a tool registry. The concrete ``ToolBox`` (tools/registry.py) satisfies it
structurally, so the llm layer never imports from the tools layer — that import direction
(llm → tools → memory/knowledge/orchestration) was a layering inversion.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, Type, TypeVar

from pydantic import BaseModel

from .errors import LLMError  # re-exported for convenience

T = TypeVar("T", bound=BaseModel)

# A live agent step: {"kind": "text"|"tool"|"thinking", "text"?: str, "tool"?: str, "input"?: str}
StepFn = Callable[[dict[str, Any]], None]

__all__ = ["LLMProvider", "ToolDispatcher", "LLMError", "StepFn"]


class ToolDispatcher(Protocol):
    """What providers actually need from a tool registry (``ToolBox`` conforms).

    Structural — no inheritance required. Owned by the llm layer so providers never
    import the concrete tools package.
    """

    def definitions(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """JSON tool definitions ({name, description, input_schema}) for ``names``."""
        ...

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> str:
        """Execute one tool call and return its (string) output."""
        ...


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
        toolbox: ToolDispatcher,
        allowed_tools: list[str],
        model: str,
        effort: str | None = None,
        max_tokens: int = 8000,
        max_iterations: int | None = None,
        workdir: str | None = None,
        on_step: StepFn | None = None,
        max_cost_usd: float | None = None,
    ) -> str:
        """Run an agentic tool-use loop and return the final assistant text.

        If ``on_step`` is given, it is called with each incremental step (text / tool use /
        thinking) as the agent works, for live streaming.

        If ``max_cost_usd`` is given, the provider checks its accumulated ``usage.cost_usd``
        against it and stops cleanly (appending a ``[stopped: budget]`` marker) instead of
        starting a turn that would run past the budget.
        """
        ...

    async def aclose(self) -> None:
        ...
