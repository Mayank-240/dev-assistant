"""Thin async wrapper around the Anthropic SDK.

Centralizes the API specifics so agents don't repeat them:
  - default model + per-role ``effort`` (output_config.effort)
  - adaptive thinking (thinking={"type": "adaptive"})
  - prompt caching on the (stable) system prompt AND on a large first-user-message
    prefix, so repeated agent-loop turns reuse the cached prefix
  - structured outputs via ``messages.parse`` + Pydantic
  - a plain tool-use ``create`` for the agent loop

(The Claude SDK backend needs none of this — the Agent SDK manages its own caching.)
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

# Cache the first user message (the stable task/context prefix, incl. repo map) only when
# it's large enough to plausibly clear the API's minimum cacheable prefix (~1024 tokens).
_CACHE_USER_PREFIX_MIN_CHARS = 4000


def _cacheable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add an ephemeral cache breakpoint on the first user block when it's large.

    In the tool-use loop the conversation grows by appending assistant/tool_result turns
    at the *end*; the first user message (task + assembled context) never changes. Marking
    it keeps the [tools → system → first-user] prefix byte-identical across loop turns, so
    every turn after the first is a cache hit — the growing tail sits after the breakpoint
    and can't invalidate it. The transformation is deterministic (same input → same
    bytes), and the caller's list/dicts are never mutated.
    """
    if not messages:
        return messages
    first = messages[0]
    if first.get("role") != "user":
        return messages
    content = first.get("content")
    if isinstance(content, str):
        if len(content) < _CACHE_USER_PREFIX_MIN_CHARS:
            return messages
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif (
        isinstance(content, list)
        and content
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
    ):
        if len(content[0].get("text") or "") < _CACHE_USER_PREFIX_MIN_CHARS:
            return messages
        if "cache_control" in content[0]:
            return messages  # already marked by the caller
        blocks = [dict(content[0], cache_control={"type": "ephemeral"}), *content[1:]]
    else:
        return messages
    return [dict(first, content=blocks), *messages[1:]]


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
        # Cache breakpoint 1: the stable system prompt (also covers the tools list, which
        # renders before it). Breakpoint 2 is the first user block — see _cacheable_messages.
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
            "messages": _cacheable_messages(messages),
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
