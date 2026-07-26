"""LLM provider backed by the raw Anthropic API (requires ANTHROPIC_API_KEY)."""

from __future__ import annotations

import logging
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from ..config import Settings
from .client import LLMClient
from .provider import ToolDispatcher
from .usage import UsageTotals

logger = logging.getLogger("ada.llm")

T = TypeVar("T", bound=BaseModel)


def _text_of(resp: Any) -> str:
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in parts if p).strip()


# Transcript step clip limits — mirror claude_sdk_provider: the full transcript is the
# durable record; the UI truncates for the compact ticker itself.
_STEP_CLIP = 4000
_TOOL_INPUT_CLIP = 2000


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self.usage = UsageTotals()
        self._client = LLMClient(settings, usage=self.usage)  # one place records tokens + cost
        self._max_turns = settings.agent_max_turns

    async def structured(
        self, *, system: str, user: str, schema: Type[T], model: str,
        effort: str | None = None, max_tokens: int = 4000,
        max_cost_usd: float | None = None,  # accepted for parity; a single turn isn't budget-gated
    ) -> T:
        return await self._client.parse(
            system=system, user=user, schema=schema, model=model, effort=effort, max_tokens=max_tokens
        )

    async def run_agent(
        self, *, system_prompt: str, prompt: str, toolbox: ToolDispatcher, allowed_tools: list[str],
        model: str, effort: str | None = None, max_tokens: int = 8000, max_iterations: int | None = None,
        workdir: str | None = None,  # accepted for interface parity; the API path has no file tools
        on_step=None, max_cost_usd: float | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        last_text = ""
        for _ in range(max_iterations or self._max_turns):
            # Per-turn budget enforcement (R3): stop before a turn that would overspend,
            # not just before the subtask starts.
            if max_cost_usd is not None and self.usage.cost_usd >= max_cost_usd:
                logger.warning(
                    "Cost budget exceeded mid-agent-loop ($%.4f >= $%.4f); stopping cleanly.",
                    self.usage.cost_usd, max_cost_usd,
                )
                return (last_text + "\n\n[stopped: budget]").strip()
            resp = await self._client.create(
                system=system_prompt, messages=messages, model=model, effort=effort,
                tools=toolbox.definitions(allowed_tools) or None, max_tokens=max_tokens,
            )
            # usage (tokens + cost) is recorded inside LLMClient.create
            messages.append({"role": "assistant", "content": resp.content})
            last_text = _text_of(resp) or last_text
            if on_step:
                for b in resp.content:
                    bt = getattr(b, "type", None)
                    if bt == "text" and getattr(b, "text", None):
                        on_step({"kind": "text", "text": b.text[:_STEP_CLIP]})
                    elif bt == "thinking" and getattr(b, "thinking", None):
                        on_step({"kind": "thinking", "text": b.thinking[:_STEP_CLIP]})
                    elif bt == "tool_use":
                        on_step({"kind": "tool", "tool": b.name,
                                 "input": str(dict(b.input or {}))[:_TOOL_INPUT_CLIP]})
            if resp.stop_reason == "tool_use":
                results: list[dict[str, Any]] = []
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use":
                        # attention tools await the operator; dispatch_async keeps the
                        # loop free so the answer can actually arrive.
                        if hasattr(toolbox, "dispatch_async"):
                            output = await toolbox.dispatch_async(block.name, dict(block.input or {}))
                        else:
                            output = toolbox.dispatch(block.name, dict(block.input or {}))
                        results.append(
                            {"type": "tool_result", "tool_use_id": block.id, "content": output}
                        )
                        if on_step:  # transcript: what the tool actually returned
                            on_step({"kind": "tool_result", "tool": block.name,
                                     "text": str(output or "")[:_STEP_CLIP], "is_error": False})
                messages.append({"role": "user", "content": results})
                continue
            if on_step and last_text:
                on_step({"kind": "result", "text": last_text[:_STEP_CLIP]})
            return last_text
        return last_text or "(agent reached its iteration limit without a final answer)"

    async def aclose(self) -> None:
        await self._client.aclose()
