"""Shared LLM error type."""

from __future__ import annotations


class LLMError(RuntimeError):
    pass


class TransientLLMError(LLMError):
    """A retryable failure (timeout, 429, 5xx/529, transport) — distinct from a real
    review failure, so the scheduler retries it with backoff instead of burning the
    substantive retry budget and permanently failing a subtask."""
