"""Full per-subtask agent transcripts: provider capture + the transcript endpoint.

The engine persists every agent_step event to docs_dir/<task>/events.jsonl; the
endpoint filters that log per subtask. Provider tests fake the model transports
(same style as tests/test_llm_layer.py) and assert the new tool_result / result
step kinds are emitted unclipped enough to be useful (~4k chars).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.web.server import create_app

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


# ---- GET /api/runs/{task_id}/transcript/{subtask_id} -----------------------------------

def _client(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws",
    )
    return TestClient(create_app(settings, api_token="")), settings


def _write_events(settings, task_id, events):
    d = settings.docs_dir / task_id
    d.mkdir(parents=True, exist_ok=True)
    with (d / "events.jsonl").open("w") as fh:
        for i, ev in enumerate(events):
            fh.write(json.dumps({"seq": i, "ts": 1000.0 + i, "message": "", **ev}) + "\n")


def test_transcript_returns_ordered_steps_for_one_subtask(tmp_path):
    c, settings = _client(tmp_path)
    _write_events(settings, "run-1", [
        {"type": "status", "data": {}},
        {"type": "agent_step", "data": {"id": "s1", "agent": "coder", "kind": "thinking", "text": "plan it"}},
        {"type": "agent_step", "data": {"id": "s2", "agent": "tester", "kind": "text", "text": "other subtask"}},
        {"type": "agent_step", "data": {"id": "s1", "agent": "coder", "kind": "tool",
                                        "tool": "write_file", "input": '{"path": "a.py"}'}},
        {"type": "agent_step", "data": {"id": "s1", "agent": "coder", "kind": "tool_result",
                                        "tool": "write_file", "text": "wrote a.py", "is_error": False}},
        {"type": "subtask_review", "data": {"id": "s1", "passed": True}},
        {"type": "agent_step", "data": {"id": "s1", "agent": "coder", "kind": "result", "text": "done"}},
    ])
    body = c.get("/api/runs/run-1/transcript/s1").json()
    assert body["agent"] == "coder"
    assert body["count"] == 4
    assert [s["kind"] for s in body["steps"]] == ["thinking", "tool", "tool_result", "result"]
    tr = body["steps"][2]
    assert tr["tool"] == "write_file" and tr["text"] == "wrote a.py" and tr["is_error"] is False
    # the other subtask's transcript is independent
    other = c.get("/api/runs/run-1/transcript/s2").json()
    assert other["agent"] == "tester" and other["count"] == 1


def test_transcript_unknown_subtask_or_task_is_empty_not_error(tmp_path):
    c, settings = _client(tmp_path)
    _write_events(settings, "run-1", [
        {"type": "agent_step", "data": {"id": "s1", "agent": "coder", "kind": "text", "text": "hi"}},
    ])
    for url in ("/api/runs/run-1/transcript/nope", "/api/runs/no-such-task/transcript/s1"):
        r = c.get(url)
        assert r.status_code == 200
        assert r.json() == {"agent": "", "count": 0, "steps": []}


# ---- claude_sdk provider: tool results + final result surface as steps -----------------

def _sdk_provider_with_stream(tmp_path, messages):
    sdk = pytest.importorskip("claude_agent_sdk")
    from ai_dev_assistant.llm.claude_sdk_provider import ClaudeSdkProvider

    p = ClaudeSdkProvider(Settings(workspace_dir=tmp_path / "ws"))

    async def fake_query(*, prompt, options):
        for m in messages:
            yield m

    p._sdk = SimpleNamespace(
        query=fake_query, ResultMessage=sdk.ResultMessage,
        AssistantMessage=sdk.AssistantMessage, UserMessage=sdk.UserMessage,
    )
    return p, sdk


async def test_sdk_collect_emits_tool_results_and_final_result(tmp_path):
    sdk = pytest.importorskip("claude_agent_sdk")
    long_thought = "t" * 5000
    p, sdk = _sdk_provider_with_stream(tmp_path, [
        sdk.AssistantMessage(model="m", content=[
            sdk.ThinkingBlock(thinking=long_thought, signature="sig"),
            sdk.ToolUseBlock(id="tu1", name="write_file", input={"path": "a.py"}),
            sdk.TextBlock(text="working"),
        ]),
        sdk.UserMessage(content=[
            sdk.ToolResultBlock(tool_use_id="tu1",
                                content=[{"type": "text", "text": "wrote a.py"}], is_error=False),
        ]),
        sdk.AssistantMessage(model="m", content=[
            sdk.ToolUseBlock(id="tu2", name="bash", input={"cmd": "boom"}),
        ]),
        sdk.UserMessage(content=[
            sdk.ToolResultBlock(tool_use_id="tu2", content="command failed", is_error=True),
        ]),
        sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                          num_turns=2, session_id="s", total_cost_usd=0.01, result="final answer"),
    ])
    steps: list[dict] = []
    out = await p._collect(prompt="p", options=None, on_step=steps.append, model="m")
    assert out == "final answer"
    assert [s["kind"] for s in steps] == [
        "thinking", "tool", "text", "tool_result", "tool", "tool_result", "result"]
    # transcript clip is generous (~4k), not the old 240-char ticker clip
    assert len(steps[0]["text"]) == 4001 and steps[0]["text"].endswith("…")
    # tool results are attributed via the tool_use_id -> name map
    ok = steps[3]
    assert ok == {"kind": "tool_result", "tool": "write_file", "text": "wrote a.py", "is_error": False}
    err = steps[5]
    assert err["tool"] == "bash" and err["text"] == "command failed" and err["is_error"] is True
    assert steps[6] == {"kind": "result", "text": "final answer"}


# ---- anthropic provider: mirror the same step kinds (best-effort) ----------------------

class _FakeToolDispatcher:
    def definitions(self, names=None):
        return [{"name": "echo", "description": "echo back",
                 "input_schema": {"type": "object", "properties": {}}}]

    def dispatch(self, name, tool_input):
        return "echoed: ok"


class _FakeMessagesAPI:
    def __init__(self, responses):
        self._responses = list(responses)

    async def create(self, **kwargs):
        return self._responses.pop(0)


def _resp(*, stop_reason, blocks):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


async def test_anthropic_loop_emits_tool_result_and_result_steps():
    from ai_dev_assistant.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(Settings(anthropic_api_key="test-key"))
    provider._client._client = SimpleNamespace(messages=_FakeMessagesAPI([
        _resp(stop_reason="tool_use", blocks=[
            SimpleNamespace(type="text", text="working"),
            SimpleNamespace(type="tool_use", id="t1", name="echo", input={}),
        ]),
        _resp(stop_reason="end_turn", blocks=[SimpleNamespace(type="text", text="done")]),
    ]))
    steps: list[dict] = []
    out = await provider.run_agent(
        system_prompt="sys", prompt="p", toolbox=_FakeToolDispatcher(),
        allowed_tools=["echo"], model="claude-opus-4-8", on_step=steps.append,
    )
    assert out == "done"
    assert [s["kind"] for s in steps] == ["text", "tool", "tool_result", "text", "result"]
    tr = steps[2]
    assert tr["tool"] == "echo" and tr["text"] == "echoed: ok" and tr["is_error"] is False
    assert steps[4]["text"] == "done"
