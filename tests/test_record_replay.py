"""Tests for llm/record_replay.py — RecordingProvider tees responses to cassettes;
ReplayProvider serves them deterministically offline; a replay miss raises clearly."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from ai_dev_assistant.llm.errors import LLMError
from ai_dev_assistant.llm.record_replay import RecordingProvider, ReplayProvider


class Verdictish(BaseModel):
    message: str
    score: int


class FakeInner:
    """Offline stand-in for a real provider; counts calls so replay isolation is provable."""

    def __init__(self) -> None:
        self.structured_calls = 0
        self.agent_calls = 0

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        self.structured_calls += 1
        return schema(message=f"reply to {user}", score=88)

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=None, workdir=None,
                        on_step=None):
        self.agent_calls += 1
        return f"agent output for: {prompt}"

    async def aclose(self):
        return None


async def _record_three_calls(cassette_dir) -> tuple[FakeInner, Verdictish, str, str]:
    inner = FakeInner()
    rec = RecordingProvider(inner, str(cassette_dir))
    v = await rec.structured(system="sys", user="rate this", schema=Verdictish, model="m")
    a1 = await rec.run_agent(system_prompt="you are a coder", prompt="write foo()",
                             toolbox=None, allowed_tools=[], model="m")
    a2 = await rec.run_agent(system_prompt="you are a coder", prompt="write bar()",
                             toolbox=None, allowed_tools=[], model="m")
    await rec.aclose()
    return inner, v, a1, a2


async def test_recording_writes_cassette_files(tmp_path):
    inner, v, a1, a2 = await _record_three_calls(tmp_path / "cassettes")
    assert inner.structured_calls == 1 and inner.agent_calls == 2
    assert v.message == "reply to rate this" and v.score == 88

    files = sorted((tmp_path / "cassettes").glob("*.json"))
    assert len(files) == 3
    kinds = {f.name.split("-")[0] for f in files}
    assert kinds == {"structured", "agent"}

    payloads = [json.loads(f.read_text()) for f in files]
    structured = [p for p in payloads if "schema" in p]
    agents = [p for p in payloads if "text" in p]
    assert len(structured) == 1 and len(agents) == 2
    assert structured[0] == {"schema": "Verdictish", "data": {"message": "reply to rate this", "score": 88}}
    assert {a["text"] for a in agents} == {"agent output for: write foo()",
                                           "agent output for: write bar()"}


async def test_replay_returns_identical_outputs_without_inner_provider(tmp_path):
    cassettes = tmp_path / "cassettes"
    inner, v, a1, a2 = await _record_three_calls(cassettes)
    inner_calls_after_recording = (inner.structured_calls, inner.agent_calls)

    replay = ReplayProvider(str(cassettes))
    rv = await replay.structured(system="sys", user="rate this", schema=Verdictish, model="m")
    ra1 = await replay.run_agent(system_prompt="you are a coder", prompt="write foo()",
                                 toolbox=None, allowed_tools=[], model="m")
    ra2 = await replay.run_agent(system_prompt="you are a coder", prompt="write bar()",
                                 toolbox=None, allowed_tools=[], model="m")
    await replay.aclose()

    assert rv == v
    assert ra1 == a1 and ra2 == a2
    # replay never touched the recorded provider
    assert (inner.structured_calls, inner.agent_calls) == inner_calls_after_recording


async def test_replay_invokes_on_step_with_recorded_text(tmp_path):
    cassettes = tmp_path / "cassettes"
    await _record_three_calls(cassettes)
    replay = ReplayProvider(str(cassettes))
    steps: list[dict] = []
    text = await replay.run_agent(system_prompt="you are a coder", prompt="write foo()",
                                  toolbox=None, allowed_tools=[], model="m",
                                  on_step=steps.append)
    assert steps and steps[0]["kind"] == "text"
    assert steps[0]["text"] in text or text.startswith(steps[0]["text"])


async def test_replay_miss_raises_clear_error(tmp_path):
    cassettes = tmp_path / "cassettes"
    await _record_three_calls(cassettes)
    replay = ReplayProvider(str(cassettes))

    with pytest.raises(LLMError, match="no cassette"):
        await replay.run_agent(system_prompt="you are a coder", prompt="write NEVER_RECORDED()",
                               toolbox=None, allowed_tools=[], model="m")
    with pytest.raises(LLMError, match="no cassette"):
        await replay.structured(system="sys", user="unseen prompt", schema=Verdictish, model="m")


async def test_replay_on_empty_dir_misses(tmp_path):
    replay = ReplayProvider(str(tmp_path / "nothing-here"))
    with pytest.raises(LLMError, match="no cassette"):
        await replay.structured(system="s", user="u", schema=Verdictish, model="m")
