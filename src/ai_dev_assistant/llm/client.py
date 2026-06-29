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
from .pricing import cost as price_of
from .resilience import with_retry
from .usage import UsageTotals

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    def __init__(self, settings: Settings, usage: UsageTotals | None = None) -> None:
        self._settings = settings
        self._timeout = settings.request_timeout
        self._max_retries = settings.llm_max_retries
        self.usage = usage or UsageTotals()
        # api_key=None lets the SDK fall back to ANTHROPIC_API_KEY / profiles.
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)

    def _record(self, resp: Any, model: str) -> None:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        in_t = getattr(u, "input_tokens", 0) or 0
        out_t = getattr(u, "output_tokens", 0) or 0
        self.usage.add(input_tokens=in_t, output_tokens=out_t, cost_usd=price_of(model, in_t, out_t))

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
        resp = await with_retry(
            lambda: self._client.messages.create(**kwargs),
            timeout=self._timeout, max_retries=self._max_retries, what="Anthropic messages.create",
        )
        self._record(resp, model)
        return resp

    async def parse(
        self,
        *,
        system: str,
        user: str,
        schema: Type[T],
        model: str,
        effort: str | None = None,
        max_tokens: int = 8000,
    ) -> T:
        """A structured-output turn. Returns a validated instance of ``schema``."""
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_format": schema,
        }
        if effort:
            kwargs["output_config"] = {"effort": effort}
        resp = await with_retry(
            lambda: self._client.messages.parse(**kwargs),
            timeout=self._timeout, max_retries=self._max_retries, what="Anthropic messages.parse",
        )
        self._record(resp, model)
        parsed = resp.parsed_output
        if parsed is None:
            # Fall back to recovering JSON from the raw text rather than failing the run.
            from .jsonout import parse_model

            text = "\n".join(getattr(b, "text", "") for b in getattr(resp, "content", []) if getattr(b, "text", None))
            if text.strip():
                try:
                    return parse_model(text, schema)
                except Exception as exc:  # noqa: BLE001
                    raise LLMError(f"Model did not return valid {schema.__name__}: {exc}") from exc
            raise LLMError(f"Model did not return valid {schema.__name__} (stop={resp.stop_reason}).")
        return parsed

    async def aclose(self) -> None:
        await self._client.close()
