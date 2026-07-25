"""LLM provider backed by the Claude Agent SDK.

Uses the local Claude Code login (no ANTHROPIC_API_KEY required). Our agent tools are
exposed to the SDK as an in-process MCP server; structured calls instruct the model to
emit JSON which we validate ourselves (the SDK has no version-stable structured mode).
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from ..config import Settings
from . import pricing
from .errors import LLMError
from .jsonout import parse_model
from .provider import ToolDispatcher
from .usage import UsageTotals

logger = logging.getLogger("ada.llm")

T = TypeVar("T", bound=BaseModel)

# "default" (not "bypassPermissions"): our allowed_tools list pre-approves the MCP tools
# and whitelisted built-ins, the PreToolUse hook below vetoes built-in file access outside
# the workspace, and anything that would otherwise prompt interactively is denied because
# no can_use_tool handler is registered (headless runs fail closed).
_PERMISSION = "default"

# Path-like inputs of the SDK's built-in file tools, per each tool's input schema.
# "pattern"/"glob" entries are glob expressions — only their literal (pre-wildcard)
# prefix is boundary-checked.
_TOOL_PATH_ARGS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "LS": ("path",),
    "Glob": ("path", "pattern"),
    "Grep": ("path", "glob"),
}

# Local mirror of the spirit of tools/registry._SECRET_GLOBS. Defined here on purpose:
# the llm layer must not import from tools.registry.
_SECRET_FILE_GLOBS = (".env", ".env.*", "*.pem", "id_rsa*", "*.key", "credentials*")
_SECRET_DIRS = (".aws", ".ssh")


def _glob_literal_prefix(pattern: str) -> str:
    """The leading part of a glob pattern before any wildcard — the only part that can
    anchor the search outside the workspace (e.g. "/etc/**" or "../*.py")."""
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i]
    return pattern


def _resolve(raw: str, workspace: Path) -> Path:
    """Resolve a tool-supplied path like the tool itself would: ~ expansion, workspace-
    relative if not absolute, then realpath (symlinks in the existing prefix are followed;
    a not-yet-existing tail is resolved via its parent)."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = workspace / p
    return p.resolve()


def _is_secret(rel: Path) -> bool:
    if any(d in rel.parts for d in _SECRET_DIRS):
        return True
    return any(fnmatch.fnmatch(rel.name, g) for g in _SECRET_FILE_GLOBS)


def check_builtin_tool_use(
    tool_name: str, tool_input: dict[str, Any], workspace: Path
) -> tuple[bool, str]:
    """Decide whether a built-in SDK tool call may run: (allowed, deny_reason).

    Every path-like argument must resolve (realpath, ``..`` and symlinks included)
    inside ``workspace``, and must not target the local secret denylist. Tools without
    path-like arguments (MCP tools, TodoWrite, ...) pass through untouched.
    """
    arg_names = _TOOL_PATH_ARGS.get(tool_name)
    if arg_names is None:
        return True, ""
    ws = Path(workspace).resolve()
    for key in arg_names:
        raw = (tool_input or {}).get(key)
        if not isinstance(raw, str) or not raw:
            continue
        value = raw
        if key in ("pattern", "glob"):
            value = _glob_literal_prefix(value)
            if not value:  # purely relative wildcard — anchored by the (checked) path arg
                continue
        resolved = _resolve(value, ws)
        if not (resolved == ws or resolved.is_relative_to(ws)):
            return False, (
                f"{tool_name} {key}={raw!r} resolves to {resolved}, outside the run "
                f"workspace {ws}. Use paths inside the workspace."
            )
        rel = resolved.relative_to(ws)
        if _is_secret(rel):
            return False, f"{tool_name} {key}={raw!r} matches the secret-file denylist."
    return True, ""


def budget_gate(cost_usd: float, max_cost_usd: float | None) -> tuple[bool, str]:
    """Decide whether the run may spend another tool call: (allowed, deny_reason).

    Denies once the recorded spend reaches ``max_cost_usd`` (no cap: always allow).
    The reason tells the model to wrap up, so the turn ends with a summary of the work
    done instead of burning further tool turns.
    """
    if max_cost_usd is None or cost_usd < max_cost_usd:
        return True, ""
    return False, (
        f"budget exhausted (${cost_usd:.4f} spent >= ${max_cost_usd:.4f} cap): "
        "finish now with a summary of what you completed."
    )


def _deny(reason: str) -> dict[str, Any]:
    """PreToolUse hook output that vetoes the pending tool call."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


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
        # Workspace the built-in file tools are confined to. cwd alone is NOT a boundary
        # (built-ins accept absolute paths) — the PreToolUse hook built from
        # check_builtin_tool_use() is what actually enforces confinement.
        self._cwd = str(Path(settings.workspace_dir).resolve())
        Path(self._cwd).mkdir(parents=True, exist_ok=True)
        # True once any ResultMessage arrived without total_cost_usd and we had to
        # estimate the cost from token counts (see _record_usage).
        self.cost_estimated = False

    # Built-in SDK tools we deny unless they're on the allowlist (Bash + web are the
    # dangerous ones: arbitrary host code execution and SSRF/prompt-injection vectors).
    _ALL_BUILTINS = ("Read", "Write", "Edit", "Bash", "Glob", "Grep", "LS", "WebSearch", "WebFetch",
                     "NotebookEdit", "TodoWrite")

    def _disallowed_builtins(self) -> list[str]:
        return [t for t in self._ALL_BUILTINS if t not in self._allowed_builtins]

    def _model_kw(self, model: str | None = None) -> dict[str, Any]:
        chosen = model or self._model
        return {"model": chosen} if chosen else {}

    # The SDK's ClaudeAgentOptions.effort is Literal['low','medium','high','xhigh','max'] —
    # forward the per-role effort so reasoning depth actually scales on this backend too.
    _VALID_EFFORT = frozenset({"low", "medium", "high", "xhigh", "max"})

    def _effort_kw(self, effort: str | None) -> dict[str, Any]:
        return {"effort": effort} if effort in self._VALID_EFFORT else {}

    def _confinement_hooks(
        self, workspace: str, max_cost_usd: float | None = None
    ) -> dict[str, list[Any]]:
        """PreToolUse hooks: workspace confinement plus (optionally) the budget gate.

        Per the SDK's semantics, a PreToolUse hook gates every matching tool call
        regardless of allowed_tools / permission_mode — unlike can_use_tool, which only
        fires for calls that would prompt (and requires a streaming prompt). The
        confinement matcher targets the built-in file tools; the budget matcher uses
        ``matcher=None``, which the hooks protocol treats as "match every tool", so it
        also covers Bash/web and our MCP toolbox tools.
        """
        ws = Path(workspace)

        async def _gate(hook_input: dict[str, Any], _tool_use_id: Any, _ctx: Any) -> dict[str, Any]:
            ok, reason = check_builtin_tool_use(
                hook_input.get("tool_name", ""), hook_input.get("tool_input") or {}, ws
            )
            return {} if ok else _deny(reason)

        matchers = [self._sdk.HookMatcher(matcher="|".join(sorted(_TOOL_PATH_ARGS)), hooks=[_gate])]
        if max_cost_usd is not None:

            async def _budget(hook_input: dict[str, Any], _tool_use_id: Any, _ctx: Any) -> dict[str, Any]:
                ok, reason = budget_gate(self.usage.cost_usd, max_cost_usd)
                if not ok:
                    logger.warning("Budget gate denied %s: %s",
                                   hook_input.get("tool_name", "?"), reason)
                return {} if ok else _deny(reason)

            matchers.append(self._sdk.HookMatcher(matcher=None, hooks=[_budget]))
        return {"PreToolUse": matchers}

    async def _collect(
        self, *, prompt: str, options: Any, on_step=None, soft_cap: bool = False,
        model: str | None = None,
    ) -> str:
        sdk = self._sdk
        result: str | None = None
        last_assistant = ""
        try:
            async for msg in sdk.query(prompt=prompt, options=options):
                if isinstance(msg, sdk.ResultMessage):
                    result = getattr(msg, "result", None) or result
                    self._record_usage(msg, model=model)
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
        max_cost_usd: float | None = None,  # accepted for parity; a single call isn't budget-gated
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
            **self._effort_kw(effort),
        )
        attempt_user = user
        last_err: Exception | None = None
        for _ in range(2):
            text = await self._collect(prompt=attempt_user, options=options,
                                       model=model or self._model)
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
        self, *, system_prompt: str, prompt: str, toolbox: ToolDispatcher, allowed_tools: list[str],
        model: str, effort: str | None = None, max_tokens: int = 8000, max_iterations: int | None = None,
        workdir: str | None = None, on_step=None, max_cost_usd: float | None = None,
    ) -> str:
        """Run the SDK-driven agent loop.

        Budget enforcement (R3), even though the SDK owns the loop: (1) refuse to start
        when the accumulated cost already meets ``max_cost_usd``; (2) mid-run, a
        catch-all PreToolUse hook (see _confinement_hooks) consults the live usage
        totals and denies every further tool call — built-ins and MCP toolbox alike —
        once the recorded spend reaches the cap. The model can still generate text to
        finish its current turn, so an over-budget run ends with a summary instead of
        burning more tool turns. Spend is recorded as result messages arrive
        (_record_usage), so the tail of one uncharged stretch can still overshoot.
        """
        if max_cost_usd is not None and self.usage.cost_usd >= max_cost_usd:
            logger.warning(
                "Cost budget exceeded before SDK agent start ($%.4f >= $%.4f); refusing to start.",
                self.usage.cost_usd, max_cost_usd,
            )
            return "[stopped: budget]"
        defs = toolbox.definitions(allowed_tools)
        server = self._build_server(toolbox, defs)
        mcp_allowed = [f"mcp__ada__{d['name']}" for d in defs]
        cwd = workdir or self._cwd
        Path(cwd).mkdir(parents=True, exist_ok=True)
        # Allow our MCP tools plus the whitelisted built-ins (Read/Write/Edit/Glob/Grep by
        # default); deny Bash + web unless explicitly enabled, so a normal run can't execute
        # arbitrary host commands or reach the network. The whitelisted built-ins are then
        # gated per-call by the PreToolUse hook: every path argument must resolve inside
        # this run's workspace and off the secret denylist, since cwd by itself does not
        # stop absolute-path access.
        options = self._sdk.ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={"ada": server},
            allowed_tools=mcp_allowed + list(self._allowed_builtins),
            disallowed_tools=self._disallowed_builtins(),
            permission_mode=_PERMISSION,
            hooks=self._confinement_hooks(cwd, max_cost_usd=max_cost_usd),
            max_turns=max_iterations or self._max_turns,
            cwd=cwd,  # working directory only — confinement is enforced by the hook above
            **self._model_kw(model),
            **self._effort_kw(effort),
        )
        return await self._collect(prompt=prompt, options=options, on_step=on_step,
                                   soft_cap=True, model=model or self._model)

    def _build_server(self, toolbox: ToolDispatcher, defs: list[dict[str, Any]]) -> Any:
        sdk = self._sdk
        sdk_tools = []
        for d in defs:
            name = d["name"]

            @sdk.tool(name, d["description"], d["input_schema"])
            async def _fn(args: dict[str, Any], _name: str = name) -> dict[str, Any]:
                # dispatch_async lets attention tools (ask_operator/request_permission)
                # await the operator without blocking the event loop.
                if hasattr(toolbox, "dispatch_async"):
                    output = await toolbox.dispatch_async(_name, args or {})
                else:
                    output = toolbox.dispatch(_name, args or {})
                return {"content": [{"type": "text", "text": output}]}

            sdk_tools.append(_fn)
        return sdk.create_sdk_mcp_server(name="ada", version="0.1.0", tools=sdk_tools)

    def _record_usage(self, msg: Any, model: str | None = None) -> None:
        u = getattr(msg, "usage", None) or {}
        if isinstance(u, dict):
            in_t, out_t = u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0
        else:
            in_t, out_t = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
        cost_usd = getattr(msg, "total_cost_usd", None)
        if cost_usd is None:
            # A missing cost must not be booked as $0 — the budget cap would never trip.
            # Estimate from token counts instead, and flag the totals as estimated.
            cost_usd = pricing.cost(model or self._model, in_t, out_t)
            self.cost_estimated = True
            logger.warning(
                "SDK ResultMessage has no total_cost_usd; estimated $%.6f from "
                "%d in / %d out tokens (model=%s)",
                cost_usd, in_t, out_t, model or self._model,
            )
        self.usage.add(input_tokens=in_t, output_tokens=out_t, cost_usd=cost_usd)

    async def aclose(self) -> None:
        return None
