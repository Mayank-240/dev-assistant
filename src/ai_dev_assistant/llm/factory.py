"""Pick an LLM provider based on settings."""

from __future__ import annotations

from ..config import Settings
from .errors import LLMError
from .provider import LLMProvider


def get_provider(settings: Settings) -> LLMProvider:
    # Replay mode serves recorded cassettes offline (Tier 5) — no real backend needed.
    if settings.replay_dir:
        from .record_replay import ReplayProvider

        return ReplayProvider(settings.replay_dir)

    backend = settings.llm_backend
    if backend == "claude_sdk":
        from .claude_sdk_provider import ClaudeSdkProvider

        provider: LLMProvider = ClaudeSdkProvider(settings)
    elif backend == "anthropic":
        from .anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(settings)
    else:
        raise LLMError(f"Unknown ADA_LLM_BACKEND '{backend}' (use 'claude_sdk' or 'anthropic').")

    # Record mode tees real responses into cassettes for later replay/regression tests.
    if settings.record_dir:
        from .record_replay import RecordingProvider

        return RecordingProvider(provider, settings.record_dir)
    return provider
