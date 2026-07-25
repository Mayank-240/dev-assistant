"""Tests for llm/resilience.py — bounded retry with backoff for LLM calls.

Offline: fake async callables raise exception shapes matching what HTTP client
libraries produce (status_code attribute, response.headers for Retry-After).
Backoff sleeps are intercepted so tests run instantly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ai_dev_assistant.llm.errors import LLMError, TransientLLMError
from ai_dev_assistant.llm.resilience import is_retryable, with_retry


class FakeHTTPError(Exception):
    """Mimics an httpx/anthropic-style API error carrying an HTTP status."""

    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        if retry_after is not None:
            self.response = SimpleNamespace(
                status_code=status, headers={"retry-after": str(retry_after)})


@pytest.fixture
def sleeps(monkeypatch):
    """Intercept backoff sleeps; records requested delays, returns immediately."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def _instant(delay, *args, **kwargs):
        recorded.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _instant)
    return recorded


def _failing_then_ok(n_failures: int, make_exc):
    calls = {"count": 0}

    async def call():
        calls["count"] += 1
        if calls["count"] <= n_failures:
            raise make_exc()
        return "ok"

    return call, calls


# ---- retryable failure N times, then success ----
async def test_succeeds_after_transient_failures(sleeps):
    call, calls = _failing_then_ok(2, lambda: FakeHTTPError(429))
    result = await with_retry(call, timeout=5, max_retries=3)
    assert result == "ok"
    assert calls["count"] == 3          # 2 failures + 1 success
    assert len(sleeps) == 2             # slept once per retried failure


async def test_overloaded_529_is_retried(sleeps):
    call, calls = _failing_then_ok(1, lambda: FakeHTTPError(529))
    assert await with_retry(call, timeout=5, max_retries=2) == "ok"
    assert calls["count"] == 2


# ---- Retry-After hint ----
async def test_honors_retry_after_header(sleeps):
    call, _ = _failing_then_ok(1, lambda: FakeHTTPError(429, retry_after=7))
    assert await with_retry(call, timeout=5, max_retries=2) == "ok"
    assert sleeps == [7.0]


# ---- exhausted retries ----
async def test_exhausted_retries_raise_transient_error(sleeps):
    call, calls = _failing_then_ok(99, lambda: FakeHTTPError(503))
    with pytest.raises(TransientLLMError):
        await with_retry(call, timeout=5, max_retries=2, what="review call")
    assert calls["count"] == 3          # initial attempt + 2 retries
    assert len(sleeps) == 2


# ---- non-retryable errors propagate immediately ----
async def test_non_retryable_error_raises_immediately(sleeps):
    call, calls = _failing_then_ok(99, lambda: FakeHTTPError(400))
    with pytest.raises(LLMError) as exc_info:
        await with_retry(call, timeout=5, max_retries=3)
    assert not isinstance(exc_info.value, TransientLLMError)
    assert calls["count"] == 1
    assert sleeps == []


async def test_value_error_propagates_as_llm_error_without_retry(sleeps):
    call, calls = _failing_then_ok(99, lambda: ValueError("bad schema"))
    with pytest.raises(LLMError):
        await with_retry(call, timeout=5, max_retries=3)
    assert calls["count"] == 1


# ---- timeouts are retryable ----
async def test_timeout_is_retried_then_terminal():
    attempts = {"count": 0}

    async def hangs_forever():
        attempts["count"] += 1
        await asyncio.Event().wait()

    with pytest.raises(TransientLLMError):
        # real (tiny) timeouts; backoff delays skipped via monkeypatched ladder
        import ai_dev_assistant.llm.resilience as res

        old = res._BASE_DELAYS
        res._BASE_DELAYS = (0.0, 0.0, 0.0, 0.0)
        try:
            await with_retry(hangs_forever, timeout=0.05, max_retries=1)
        finally:
            res._BASE_DELAYS = old
    assert attempts["count"] == 2


# ---- classification ----
def test_is_retryable_taxonomy():
    assert is_retryable(FakeHTTPError(429))
    assert is_retryable(FakeHTTPError(503))
    assert is_retryable(ConnectionError("reset"))
    assert is_retryable(TimeoutError())

    class RateLimitError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    assert is_retryable(RateLimitError("slow down"))   # by type-name heuristic
    assert is_retryable(APIConnectionError("dns"))
    assert not is_retryable(FakeHTTPError(400))
    assert not is_retryable(FakeHTTPError(401))
    assert not is_retryable(ValueError("nope"))
