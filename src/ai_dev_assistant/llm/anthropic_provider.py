"""LLM provider backed by the raw Anthropic API (requires ANTHROPIC_API_KEY)."""

from __future__ import annotations

from typing import Any, Type, TypeVar

from pydantic import BaseModel

from ..config import Settings
from ..tools.registry import ToolBox
from .client import LLMClient
from .usage import UsageTotals

T = TypeVar("T", bound=BaseModel)


def _text_of(resp: Any) -> str:
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in parts if p).strip()


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self.usage = UsageTotals()
        self._client = LLMClient(settings, usage=self.usage)  # one place records tokens + cost
        self._max_turns = settings.agent_max_turns

    async def structured(
        self, *, system: str, user: str, schema: Type[T], model: str,
        effort: str | None = None, max_tokens: int = 4000,
    ) -> T:
        return await self._client.parse(
            system=system, user=user, schema=schema, model=model, effort=effort, max_tokens=max_tokens
        )

    async def run_agent(
        self, *, system_prompt: str, prompt: str, toolbox: ToolBox, allowed_tools: list[str],
        model: str, effort: str | None = None, max_tokens: int = 8000, max_iterations: int | None = None,
        workdir: str | None = None,  # accepted for interface parity; the API path has no file tools
        on_step=None,
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        last_text = ""
        for _ in range(max_iterations or self._max_turns):
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
                        on_step({"kind": "text", "text": b.text[:240]})
                    elif bt == "thinking" and getattr(b, "thinking", None):
                        on_step({"kind": "thinking", "text": b.thinking[:240]})
                    elif bt == "tool_use":
                        on_step({"kind": "tool", "tool": b.name, "input": str(dict(b.input or {}))[:120]})
            if resp.stop_reason == "tool_use":
                results: list[dict[str, Any]] = []
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use":
                        output = toolbox.dispatch(block.name, dict(block.input or {}))
                        results.append(
                            {"type": "tool_result", "tool_use_id": block.id, "content": output}
                        )
                messages.append({"role": "user", "content": results})
                continue
            return last_text
        return last_text or "(agent reached its iteration limit without a final answer)"

    async def aclose(self) -> None:
        await self._client.aclose()
