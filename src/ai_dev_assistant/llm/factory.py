"""Pick an LLM provider based on settings."""

from __future__ import annotations

from ..config import Settings
from .errors import LLMError
from .provider import LLMProvider


def get_provider(settings: Settings) -> LLMProvider:
    backend = settings.llm_backend
    if backend == "claude_sdk":
        from .claude_sdk_provider import ClaudeSdkProvider

        return ClaudeSdkProvider(settings)
    if backend == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    raise LLMError(f"Unknown ADA_LLM_BACKEND '{backend}' (use 'claude_sdk' or 'anthropic').")
