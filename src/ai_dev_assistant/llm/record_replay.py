"""Record/replay provider cassettes (Tier 5).

The providers + JSON repair path had zero coverage — exactly where silent breakage hides.
A RecordingProvider captures real structured()/run_agent responses to JSON cassettes; a
ReplayProvider serves them deterministically so the engine can be regression-tested offline
with realistic shapes (instead of only the hand-coded FakeProvider).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from .errors import LLMError
from .provider import LLMProvider
from .usage import UsageTotals

T = TypeVar("T", bound=BaseModel)


def _key(kind: str, *parts: str) -> str:
    h = hashlib.sha256(("|".join([kind, *parts])).encode("utf-8")).hexdigest()
    return f"{kind}-{h[:16]}"


class RecordingProvider:
    """Wraps a real provider; tees every response into a cassette directory."""

    def __init__(self, inner: LLMProvider, cassette_dir: str) -> None:
        self._inner = inner
        self._dir = Path(cassette_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.usage = getattr(inner, "usage", UsageTotals())

    def _save(self, key: str, payload: dict) -> None:
        (self._dir / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False))

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        result = await self._inner.structured(system=system, user=user, schema=schema, model=model,
                                               effort=effort, max_tokens=max_tokens)
        self._save(_key("structured", system, user, schema.__name__),
                   {"schema": schema.__name__, "data": result.model_dump()})
        return result

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=None, workdir=None, on_step=None):
        text = await self._inner.run_agent(
            system_prompt=system_prompt, prompt=prompt, toolbox=toolbox, allowed_tools=allowed_tools,
            model=model, effort=effort, max_tokens=max_tokens, max_iterations=max_iterations,
            workdir=workdir, on_step=on_step)
        self._save(_key("agent", system_prompt, prompt), {"text": text})
        return text

    async def aclose(self):
        await self._inner.aclose()


class ReplayProvider:
    """Serves recorded responses; never touches the network."""

    def __init__(self, cassette_dir: str) -> None:
        self._dir = Path(cassette_dir)
        self.usage = UsageTotals()

    def _load(self, key: str) -> dict:
        path = self._dir / f"{key}.json"
        if not path.is_file():
            raise LLMError(f"no cassette for {key} in {self._dir}")
        return json.loads(path.read_text())

    async def structured(self, *, system, user, schema: Type[T], model, effort=None, max_tokens=4000) -> T:
        rec = self._load(_key("structured", system, user, schema.__name__))
        return schema.model_validate(rec["data"])

    async def run_agent(self, *, system_prompt, prompt, toolbox, allowed_tools, model,
                        effort=None, max_tokens=8000, max_iterations=None, workdir=None, on_step=None):
        rec = self._load(_key("agent", system_prompt, prompt))
        if on_step:
            on_step({"kind": "text", "text": rec["text"][:240]})
        return rec["text"]

    async def aclose(self):
        return None
