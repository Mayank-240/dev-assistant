"""Per-run span tracing (Tier 5 observability).

Records a span per phase / LLM call / tool dispatch to a JSONL file so 'why did this run
take 3 minutes / cost $5 / which tool kept erroring' is answerable. Append-only; no
external tracing backend required.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Tracer:
    def __init__(self, path: Path | None, *, enabled: bool = True) -> None:
        self._path = Path(path) if path else None
        self._enabled = enabled and self._path is not None
        self._seq = 0
        if self._enabled:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("")  # truncate any prior run's trace

    def event(self, kind: str, name: str, **fields: Any) -> None:
        if not self._enabled:
            return
        self._seq += 1
        rec = {"seq": self._seq, "ts": time.time(), "kind": kind, "name": name, **fields}
        with self._path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    @contextmanager
    def span(self, kind: str, name: str, **fields: Any):
        start = time.time()
        status = "ok"
        try:
            yield
        except Exception as exc:  # record failed spans instead of swallowing them silently
            status = "error"
            fields["error"] = str(exc)[:200]
            raise
        finally:
            self.event(kind, name, status=status, duration=round(time.time() - start, 3), **fields)
