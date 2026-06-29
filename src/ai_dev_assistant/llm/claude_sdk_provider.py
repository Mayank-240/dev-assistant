"""LLM provider backed by the Claude Agent SDK.

Uses the local Claude Code login (no ANTHROPIC_API_KEY required). Our agent tools are
exposed to the SDK as an in-process MCP server; structured calls instruct the model to
emit JSON which we validate ourselves (the SDK has no version-stable structured mode).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from ..config import Settings
from ..tools.registry import ToolBox
from .errors import LLMError
from .jsonout import parse_model
from .usage import UsageTotals

T = TypeVar("T", bound=BaseModel)

_PERMISSION = "bypassPermissions"  # run our tools without interactive approval


def _clip(s: Any, n: int = 240) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def _short_input(inp: Any) -> str:
    if inp is None:
        return ""
    if isinstance(inp, str):
        return inp
    try:
        return json.dumps(inp)
    except Exception:
        return str(inp)


class ClaudeSdkProvider:
    name = "claude_sdk"

    def __init__(self, settings: Settings) -> None:
        try:
            import claude_agent_sdk as sdk  # noqa: PLC0415 (lazy: only needed for this backend)
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "claude-agent-sdk is not installed. Run: uv pip install claude-agent-sdk"
            ) from exc
        self._sdk = sdk
        self._model = settings.sdk_model or None
        self._max_turns = settings.agent_max_turns
        self.usage = UsageTotals()
        self._allowed_builtins = settings.allowed_builtins_list
        # Confine the SDK's built-in file/bash tools to a workspace dir, not the source tree.
        self._cwd = str(Path(settings.workspace_dir).resolve())
        Path(self._cwd).mkdir(parents=True, exist_ok=True)

    # Built-in SDK tools we deny unless they're on the allowlist (Bash + web are the
    # dangerous ones: arbitrary host code execution and SSRF/prompt-injection vectors).
    _ALL_BUILTINS = ("Read", "Write", "Edit", "Bash", "Glob", "Grep", "LS", "WebSearch", "WebFetch",
                     "NotebookEdit", "TodoWrite")

    def _disallowed_builtins(self) -> list[str]:
        return [t for t in self._ALL_BUILTINS if t not in self._allowed_builtins]

    def _model_kw(self, model: str | None = None) -> dict[str, Any]:
        chosen = model or self._model
        return {"model": chosen} if chosen else {}

    async def _collect(self, *, prompt: str, options: Any, on_step=None, soft_cap: bool = False) -> str:
        sdk = self._sdk
        result: str | None = None
        last_assistant = ""
        try:
            async for msg in sdk.query(prompt=prompt, options=options):
                if isinstance(msg, sdk.ResultMessage):
                    result = getattr(msg, "result", None) or result
                    self._record_usage(msg)
                elif isinstance(msg, sdk.AssistantMessage):
                    text_parts = []
                    for b in getattr(msg, "content", []):
                        if getattr(b, "thinking", None):
                            if on_step:
                                on_step({"kind": "thinking", "text": _clip(b.thinking)})
                        elif getattr(b, "name", None):  # ToolUseBlock
                            if on_step:
                                on_step({"kind": "tool", "tool": str(b.name),
                                         "input": _clip(_short_input(getattr(b, "input", None)), 120)})
                        elif getattr(b, "text", None):
                            text_parts.append(b.text)
                            if on_step:
                                on_step({"kind": "text", "text": _clip(b.text)})
                    joined = "".join(text_parts).strip()
                    if joined:
                        last_assistant = joined
        except Exception as exc:  # auth, transport, CLI not found, etc.
            # Hitting the per-agent turn limit isn't fatal: the agent has usually already
            # written files to the workspace. Keep the partial output so the subtask is
            # reviewed against what's actually on disk instead of being discarded.
            if soft_cap and "maximum number of turns" in str(exc).lower() and (last_assistant or result):
                return ((result or last_assistant) +
                        "\n\n[note: agent reached its turn limit; output may be partial — "
                        "verify against the workspace files]").strip()
            raise LLMError(f"Claude Agent SDK call failed: {exc}") from exc
        return (result or last_assistant or "").strip()

    async def structured(
        self, *, system: str, user: str, schema: Type[T], model: str,
        effort: str | None = None, max_tokens: int = 4000,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema())
        sys_prompt = (
            f"{system}\n\nIMPORTANT: Respond with ONLY a single JSON object that conforms to "
            f"this JSON Schema. No prose, no explanation, no markdown code fence.\n{schema_json}"
        )
        options = self._sdk.ClaudeAgentOptions(
            system_prompt=sys_prompt,
            allowed_tools=[],
            disallowed_tools=list(self._ALL_BUILTINS),  # planning/review needs no tools at all
            permission_mode=_PERMISSION,
            max_turns=4,  # headroom: a model occasionally needs >1 turn to emit clean JSON
            cwd=self._cwd,  # keep planning/review out of the project source tree
            **self._model_kw(model),
        )
        attempt_user = user
        last_err: Exception | None = None
        for _ in range(2):
            text = await self._collect(prompt=attempt_user, options=options)
            try:
                return parse_model(text, schema)
            except Exception as exc:
                last_err = exc
                attempt_user = (
                    f"{user}\n\nYour previous reply was not valid JSON for the schema. "
                    "Return ONLY the JSON object, nothing else."
                )
        raise LLMError(f"Could not parse {schema.__name__} from SDK output: {last_err}")

    async def run_agent(
        self, *, system_prompt: str, prompt: str, toolbox: ToolBox, allowed_tools: list[str],
        model: str, effort: str | None = None, max_tokens: int = 8000, max_iterations: int | None = None,
        workdir: str | None = None, on_step=None,
    ) -> str:
        defs = toolbox.definitions(allowed_tools)
        server = self._build_server(toolbox, defs)
        mcp_allowed = [f"mcp__ada__{d['name']}" for d in defs]
        cwd = workdir or self._cwd
        Path(cwd).mkdir(parents=True, exist_ok=True)
        # Allow our MCP tools plus the whitelisted built-ins (Read/Write/Edit/Glob/Grep by
        # default); deny Bash + web unless explicitly enabled, so a normal run can't execute
        # arbitrary host commands or reach the network.
        options = self._sdk.ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={"ada": server},
            allowed_tools=mcp_allowed + list(self._allowed_builtins),
            disallowed_tools=self._disallowed_builtins(),
            permission_mode=_PERMISSION,
            max_turns=max_iterations or self._max_turns,
            cwd=cwd,  # built-in file/bash tools operate inside this run's workspace
            **self._model_kw(model),
        )
        return await self._collect(prompt=prompt, options=options, on_step=on_step, soft_cap=True)

    def _build_server(self, toolbox: ToolBox, defs: list[dict[str, Any]]) -> Any:
        sdk = self._sdk
        sdk_tools = []
        for d in defs:
            name = d["name"]

            @sdk.tool(name, d["description"], d["input_schema"])
            async def _fn(args: dict[str, Any], _name: str = name) -> dict[str, Any]:
                output = toolbox.dispatch(_name, args or {})
                return {"content": [{"type": "text", "text": output}]}

            sdk_tools.append(_fn)
        return sdk.create_sdk_mcp_server(name="ada", version="0.1.0", tools=sdk_tools)

    def _record_usage(self, msg: Any) -> None:
        u = getattr(msg, "usage", None) or {}
        if isinstance(u, dict):
            in_t, out_t = u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0
        else:
            in_t, out_t = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
        self.usage.add(
            input_tokens=in_t, output_tokens=out_t,
            cost_usd=getattr(msg, "total_cost_usd", 0.0) or 0.0,
        )

    async def aclose(self) -> None:
        return None
