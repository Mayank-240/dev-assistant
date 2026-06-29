"""Bounded retry with exponential backoff + jitter for LLM HTTP calls.

The raw providers had no timeout and no retry, so a transient 429/529/5xx aborted the
whole run. ``with_retry`` wraps a single async call: it retries retryable failures
(timeouts, 429, 5xx/529, transport errors), honoring a Retry-After hint when present, and
re-raises a persistent transient failure as ``TransientLLMError`` so the scheduler can
distinguish it from a real review failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from .errors import LLMError, TransientLLMError

logger = logging.getLogger("ada.llm")

T = TypeVar("T")

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
# Deterministic jitter ladder (Math.random is unavailable in some sandboxes; keep it simple).
_BASE_DELAYS = (0.5, 1.5, 4.0, 9.0)


def _status_of(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    try:
        val = headers.get("retry-after") or headers.get("Retry-After")
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "apiconnection", "overloaded", "ratelimit")):
        return True
    status = _status_of(exc)
    return status in _RETRYABLE_STATUS if status is not None else False


async def with_retry(
    call: Callable[[], Awaitable[T]],
    *,
    timeout: float,
    max_retries: int,
    what: str = "LLM call",
) -> T:
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(call(), timeout)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            last = exc
            retryable = True
        except Exception as exc:  # noqa: BLE001 — classify below
            last = exc
            retryable = is_retryable(exc)
            if not retryable:
                raise LLMError(f"{what} failed: {exc}") from exc
        if attempt >= max_retries:
            break
        delay = _retry_after(last) or _BASE_DELAYS[min(attempt, len(_BASE_DELAYS) - 1)]
        logger.warning("%s transient failure (%s); retry %d/%d in %.1fs",
                       what, last, attempt + 1, max_retries, delay)
        await asyncio.sleep(delay)
    raise TransientLLMError(f"{what} failed after {max_retries} retries: {last}")
