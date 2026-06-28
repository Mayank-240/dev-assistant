"""Thin async wrapper around the Anthropic SDK.

Centralizes the API specifics so agents don't repeat them:
  - default model + per-role ``effort`` (output_config.effort)
  - adaptive thinking (thinking={"type": "adaptive"})
  - prompt caching on the (stable) system prompt
  - structured outputs via ``messages.parse`` + Pydantic
  - a plain tool-use ``create`` for the agent loop
"""

from __future__ import annotations

from typing import Any, Sequence, Type, TypeVar

import anthropic
from pydantic import BaseModel

from ..config import Settings
from .errors import LLMError

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # api_key=None lets the SDK fall back to ANTHROPIC_API_KEY / profiles.
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)

    def _system_blocks(self, system: str) -> list[dict[str, Any]]:
        # Cache the stable system prompt so repeated agent turns are cheap.
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        model: str,
        effort: str | None = None,
        max_tokens: int = 8000,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> "anthropic.types.Message":
        """One Messages API turn. Returns the full Message (content blocks + stop_reason)."""
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": self._system_blocks(system),
            "messages": messages,
            "thinking": {"type": "adaptive"},
        }
        if effort:
            kwargs["output_config"] = {"effort": effort}
        if tools:
            kwargs["tools"] = list(tools)
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        try:
            return await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:  # surface a clean error to the orchestrator
            raise LLMError(f"Anthropic API error: {exc}") from exc

    async def parse(
        self,
        *,
        system: str,
        user: str,
        schema: Type[T],
        model: str,
        max_tokens: int = 8000,
    ) -> T:
        """A structured-output turn. Returns a validated instance of ``schema``."""
        try:
            resp = await self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic API error during structured parse: {exc}") from exc
        parsed = resp.parsed_output
        if parsed is None:
            raise LLMError(f"Model did not return valid {schema.__name__} (stop={resp.stop_reason}).")
        return parsed

    async def aclose(self) -> None:
        await self._client.close()
