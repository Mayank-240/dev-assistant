"""Tests for the llm-layer improvements.

Covers: the ToolDispatcher protocol breaking the llm→tools layering inversion (A3),
the unified budget policy + token heuristic + line-boundary truncation (C3), prompt-cache
breakpoints on the anthropic backend (C4), and per-turn budget enforcement (R3).

Offline: the Anthropic transport is faked at the AsyncAnthropic client boundary (see
tests/test_resilience.py for the same style of exception/transport fakes).
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.context import BudgetPolicy, assemble, budget_for, estimate_tokens


# ---- A3: ToolDispatcher protocol -------------------------------------------------------

def test_toolbox_satisfies_tool_dispatcher_structurally():
    """ToolBox conforms to the Protocol without inheriting from it (isinstance-free)."""
    from ai_dev_assistant.llm.provider import ToolDispatcher
    from ai_dev_assistant.tools.registry import ToolBox  # imported only inside this test

    assert ToolDispatcher is not None
    for member in ("dispatch", "definitions"):
        assert callable(getattr(ToolBox, member)), f"ToolBox lacks Protocol member {member}"


def test_llm_layer_does_not_import_tools_package():
    """Importing every llm module must not (transitively) pull in the tools package."""
    code = (
        "import sys; "
        "import ai_dev_assistant.llm.provider, ai_dev_assistant.llm.client, "
        "ai_dev_assistant.llm.anthropic_provider, ai_dev_assistant.llm.claude_sdk_provider, "
        "ai_dev_assistant.llm.factory; "
        "bad = [m for m in sys.modules if m.startswith('ai_dev_assistant.tools')]; "
        "assert not bad, bad"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---- C3: budget policy -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_ambient_budget_override(monkeypatch):
    monkeypatch.delenv("ADA_CONTEXT_BUDGET_TOKENS", raising=False)


def test_budget_for_family_defaults():
    opus = budget_for("claude-opus-4-8")
    sonnet = budget_for("claude-sonnet-4-5")
    haiku = budget_for("claude-haiku-4-5")
    unknown = budget_for("some-future-model")
    assert isinstance(opus, BudgetPolicy)
    assert opus.context_budget_tokens > sonnet.context_budget_tokens > haiku.context_budget_tokens
    # unknown models get a sane middle-of-the-road default
    assert unknown.context_budget_tokens == sonnet.context_budget_tokens
    for p in (opus, sonnet, haiku, unknown):
        assert p.max_part_chars > 0 and p.reviewer_budget_tokens > 0


def test_budget_for_reviewer_role_uses_reviewer_budget():
    agent = budget_for("claude-opus-4-8")
    reviewer = budget_for("claude-opus-4-8", role="reviewer")
    assert reviewer.context_budget_tokens == agent.reviewer_budget_tokens
    assert reviewer.context_budget_tokens < agent.context_budget_tokens


def test_budget_for_env_override(monkeypatch):
    monkeypatch.setenv("ADA_CONTEXT_BUDGET_TOKENS", "1234")
    assert budget_for("claude-opus-4-8").context_budget_tokens == 1234
    assert budget_for("whatever", role="reviewer").context_budget_tokens == 1234


def test_budget_for_malformed_env_override_ignored(monkeypatch):
    baseline = budget_for("claude-haiku-4-5").context_budget_tokens
    monkeypatch.setenv("ADA_CONTEXT_BUDGET_TOKENS", "lots")
    assert budget_for("claude-haiku-4-5").context_budget_tokens == baseline


# ---- C3: estimate_tokens heuristic -----------------------------------------------------

def test_estimate_tokens_code_denser_than_prose():
    prose = "The quick brown fox jumps over the lazy dog and keeps on running. " * 20
    code = 'def f(x):\n    return {"a": [x + 1, x * 2], "b": (x, x)}\n' * 20
    assert estimate_tokens(prose) == len(prose) // 4          # prose branch: ~4 chars/token
    assert estimate_tokens(code) == int(len(code) / 3.5)      # code branch: ~3.5 chars/token
    # per-char, code costs more tokens than prose
    assert estimate_tokens(code) / len(code) > estimate_tokens(prose) / len(prose)
    assert estimate_tokens("") == 1


# ---- C3: assemble truncates at line boundaries -----------------------------------------

def test_assemble_truncates_at_line_boundary():
    log_lines = "\n".join(f"line {i}: some content here" for i in range(2000))
    out = assemble([("Task", "do the thing"), ("Log", log_lines)], budget_tokens=500)
    assert "do the thing" in out                              # small parts survive intact
    assert "…(truncated to fit context budget)" in out
    head = out.split("\n…(truncated to fit context budget)")[0]
    last_kept_line = head.rsplit("\n", 1)[-1]
    # the kept portion ends with a *complete* line from the original, never mid-line
    assert last_kept_line in log_lines.splitlines()


def test_assemble_single_giant_line_still_truncates():
    # no newlines to break on — falls back to a hard cut, budget still respected
    out = assemble([("Task", "do it"), ("Dep", "x" * 100_000)], budget_tokens=500)
    assert "do it" in out and len(out) < 10_000


# ---- fakes for the anthropic backend ---------------------------------------------------

class FakeToolDispatcher:
    """Satisfies ToolDispatcher structurally without touching tools/registry."""

    def __init__(self):
        self.dispatched: list[tuple[str, dict]] = []

    def definitions(self, names=None):
        return [{"name": "echo", "description": "echo back",
                 "input_schema": {"type": "object", "properties": {}}}]

    def dispatch(self, name, tool_input):
        self.dispatched.append((name, tool_input))
        return "ok"


class FakeMessagesAPI:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _resp(*, stop_reason, blocks, usage=None):
    return SimpleNamespace(
        content=blocks, stop_reason=stop_reason,
        usage=usage or SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _anthropic_provider(fake_messages):
    from ai_dev_assistant.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(Settings(anthropic_api_key="test-key"))
    provider._client._client = SimpleNamespace(messages=fake_messages)
    return provider


# ---- C4: cache_control breakpoints, stable across loop turns ---------------------------

async def test_cache_control_on_system_and_large_user_prefix_stable_across_turns():
    big_prompt = "Task context line with details.\n" * 200   # > 4000 chars
    fake = FakeMessagesAPI([
        _resp(stop_reason="tool_use", blocks=[
            SimpleNamespace(type="text", text="working"),
            SimpleNamespace(type="tool_use", id="t1", name="echo", input={}),
        ]),
        _resp(stop_reason="end_turn", blocks=[SimpleNamespace(type="text", text="done")]),
    ])
    provider = _anthropic_provider(fake)
    tools = FakeToolDispatcher()

    out = await provider.run_agent(
        system_prompt="You are helpful.", prompt=big_prompt, toolbox=tools,
        allowed_tools=["echo"], model="claude-opus-4-8",
    )
    assert out == "done"
    assert len(fake.calls) == 2
    assert tools.dispatched == [("echo", {})]

    for call in fake.calls:
        # breakpoint 1: system block
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
        # breakpoint 2: first user content block (the large stable prefix)
        first_msg = call["messages"][0]
        assert first_msg["role"] == "user"
        assert first_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert first_msg["content"][0]["text"] == big_prompt

    # identical prefix bytes across turns → cache hit; only the tail grows
    assert fake.calls[0]["messages"][0] == fake.calls[1]["messages"][0]
    assert fake.calls[0]["system"] == fake.calls[1]["system"]
    assert len(fake.calls[1]["messages"]) == 3  # user, assistant, tool_result


def test_small_user_prompt_gets_no_cache_breakpoint():
    from ai_dev_assistant.llm.client import _cacheable_messages

    msgs = [{"role": "user", "content": "short prompt"}]
    assert _cacheable_messages(msgs) == msgs  # unchanged: below the min cacheable size


# ---- R3: per-turn budget enforcement ---------------------------------------------------

async def test_run_agent_stops_when_budget_exceeded_mid_loop(caplog):
    # opus pricing: 1M output tokens on turn 1 ≈ $75, far over the $0.05 cap
    fake = FakeMessagesAPI([
        _resp(stop_reason="tool_use", blocks=[
            SimpleNamespace(type="text", text="partial work"),
            SimpleNamespace(type="tool_use", id="t1", name="echo", input={}),
        ], usage=SimpleNamespace(input_tokens=0, output_tokens=1_000_000)),
        _resp(stop_reason="end_turn", blocks=[SimpleNamespace(type="text", text="never reached")]),
    ])
    provider = _anthropic_provider(fake)

    with caplog.at_level("WARNING", logger="ada.llm"):
        out = await provider.run_agent(
            system_prompt="sys", prompt="p", toolbox=FakeToolDispatcher(),
            allowed_tools=["echo"], model="claude-opus-4-8", max_cost_usd=0.05,
        )
    assert out.endswith("[stopped: budget]")
    assert "partial work" in out
    assert len(fake.calls) == 1                      # the second turn never happened
    assert any("budget" in r.message.lower() for r in caplog.records)


async def test_run_agent_without_budget_runs_to_completion():
    fake = FakeMessagesAPI([
        _resp(stop_reason="end_turn", blocks=[SimpleNamespace(type="text", text="fin")]),
    ])
    provider = _anthropic_provider(fake)
    out = await provider.run_agent(
        system_prompt="sys", prompt="p", toolbox=FakeToolDispatcher(),
        allowed_tools=["echo"], model="claude-opus-4-8",
    )
    assert out == "fin"


async def test_sdk_provider_refuses_to_start_over_budget(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from ai_dev_assistant.llm.claude_sdk_provider import ClaudeSdkProvider

    p = ClaudeSdkProvider(Settings(workspace_dir=tmp_path / "ws"))
    p.usage.add(cost_usd=1.0)
    out = await p.run_agent(
        system_prompt="s", prompt="p", toolbox=FakeToolDispatcher(),
        allowed_tools=[], model="claude-opus-4-8", max_cost_usd=0.5,
    )
    assert out == "[stopped: budget]"


async def test_base_agent_forwards_budget_only_when_set():
    from ai_dev_assistant.agents.base import AgentProfile, BaseAgent

    seen: dict = {}

    class RecordingProvider:
        async def run_agent(self, **kw):
            seen.clear()
            seen.update(kw)
            return "ok"

    agent = BaseAgent(AgentProfile(name="a", description="d", when_to_use="w"), "sys", "model-x")
    await agent.run(task_text="t", context="", toolbox=FakeToolDispatcher(),
                    provider=RecordingProvider())
    assert "max_cost_usd" not in seen  # legacy wrapper providers keep working

    await agent.run(task_text="t", context="c", toolbox=FakeToolDispatcher(),
                    provider=RecordingProvider(), max_cost_usd=1.5)
    assert seen["max_cost_usd"] == 1.5
