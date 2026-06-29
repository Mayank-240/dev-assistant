"""The Claude SDK backend forwards per-role effort into ClaudeAgentOptions."""

from __future__ import annotations

import pytest

from ai_dev_assistant.config import Settings


def test_sdk_effort_threading(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from ai_dev_assistant.llm.claude_sdk_provider import ClaudeSdkProvider

    p = ClaudeSdkProvider(Settings(workspace_dir=tmp_path / "ws"))
    # valid efforts are forwarded as the SDK's `effort` option
    for e in ("low", "medium", "high", "xhigh", "max"):
        assert p._effort_kw(e) == {"effort": e}
    # unknown / unset is dropped, never erroring a run
    assert p._effort_kw(None) == {}
    assert p._effort_kw("bogus") == {}


def test_sdk_effort_matches_options_literal():
    """Guard the threading vocabulary against the SDK's actual Literal so they can't drift."""
    sdk = pytest.importorskip("claude_agent_sdk")
    import dataclasses
    import typing

    from ai_dev_assistant.llm.claude_sdk_provider import ClaudeSdkProvider

    field = {f.name: f for f in dataclasses.fields(sdk.ClaudeAgentOptions)}["effort"]
    literal = typing.get_type_hints(sdk.ClaudeAgentOptions)["effort"]
    allowed = set(typing.get_args(typing.get_args(literal)[0]))  # strip Optional, then Literal
    assert ClaudeSdkProvider._VALID_EFFORT == allowed
