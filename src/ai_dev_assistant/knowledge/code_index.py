"""Semantic code index: chunked, embedded source retrieval for a project workspace.

Complements ``repo_map.py`` (which ranks *files* by import centrality for a map) by
indexing file *contents* in overlapping line windows so a natural-language query can
pull back the exact region of code that answers it. Reuses the existing embedding +
vector machinery end to end: ``get_embedder`` (hash backend = lexical-only quality,
same degradation convention as memory/KB) and ``VectorStore.search_hybrid`` (cosine +
lexical RRF; embedder down -> lexical leg alone).

Storage is a per-project SQLite database ``<projects_dir>/<project>/code_index.db``,
sited next to that project's ``memory.db``. Chunks live in the standard ``vectors``
table under namespace "code" with ``ref_id = "<relpath>::<start>-<end>"`` (1-based,
inclusive line span), so path+span round-trip without an extra table. A small
``files`` table records (path, mtime, size) for incremental reindexing.

Failure policy: ``index_workspace``/``search``/``retrieval_context`` never raise —
unreadable files are skipped, an unavailable embedder degrades to lexical-only rows,
and a corrupt database is deleted and rebuilt on open.

Engine wiring happens NEXT phase: ``retrieval_context`` already renders the block the
engine will append as an *untrusted* context part (see its docstring for the exact
format).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from ..config import Settings
from ..memory.embeddings import get_embedder
from ..memory.vector import VectorStore

logger = logging.getLogger("ada.code_index")

_NAMESPACE = "code"

# Chunking: ~60-line windows stepping 50 (10 lines of overlap so a definition split
# across a boundary is still whole in one of the two windows).
_WINDOW = 60
_OVERLAP = 10
_STEP = _WINDOW - _OVERLAP

# Skip rules. Directory exclusions are the union of engine._INTERNAL_DIRS and
# repo_map._SKIP (kept literal here so this module stays import-light and the two
# owners can evolve independently).
_SKIP_DIRS = {
    ".git", "__pycache__", ".ada_worktrees", ".ada_deps", ".ada_data",
    "node_modules", ".venv", "venv", "dist", "build", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
}
_LOCKFILES = {
    "uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Pipfile.lock", "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum",
    "flake.lock",
}
_MAX_FILE_BYTES = 400_000   # larger files are almost never hand-written source
_MAX_LINE_CHARS = 2000      # any longer single line = minified/generated -> skip file

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    chunks INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vectors (
    namespace TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    text TEXT,
    PRIMARY KEY (namespace, ref_id)
);
"""


def chunk_text(text: str) -> list[tuple[int, int, str]]:
    """Split ``text`` into (start_line, end_line, chunk) windows — 1-based inclusive.

    Windows are ``_WINDOW`` lines stepping ``_STEP`` (``_OVERLAP`` lines shared with
    the previous window). Whitespace-only windows are dropped.
    """
    lines = text.splitlines()
    n = len(lines)
    out: list[tuple[int, int, str]] = []
    start = 0
    while start < n:
        window = lines[start:start + _WINDOW]
        chunk = "\n".join(window)
        if chunk.strip():
            out.append((start + 1, min(start + _WINDOW, n), chunk))
        if start + _WINDOW >= n:
            break
        start += _STEP
    return out


def _eligible_text(path: Path) -> str | None:
    """The file's text, or None when it should be skipped (binary/lockfile/minified/
    oversized/unreadable)."""
    if path.name in _LOCKFILES:
        return None
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        blob = path.read_bytes()
    except OSError:
        return None
    if not blob or b"\x00" in blob:  # empty or binary (NUL never appears in source)
        return None
    text = blob.decode("utf-8", errors="replace")
    if any(len(line) > _MAX_LINE_CHARS for line in text.splitlines()):
        return None  # minified/generated bundle — noise, not source
    return text


class CodeIndex:
    """Per-project semantic index over a workspace's source files.

    ``CodeIndex(settings, project)`` opens (creating or, if corrupt, rebuilding)
    ``<projects_dir>/<project>/code_index.db``. All public methods degrade instead
    of raising. ``embedder`` is an injection point for tests; by default the
    process-wide cached embedder is used.
    """

    def __init__(self, settings: Settings, project: str | None = None, *,
                 embedder=None) -> None:
        self._settings = settings
        slug = project or settings.project or "default"
        self._db_path = settings.projects_dir / slug / "code_index.db"
        self._lock = threading.Lock()
        self._embedder = embedder if embedder is not None else get_embedder(settings)
        self._conn = self._connect()
        self._vectors = VectorStore(self._conn, self._embedder, self._lock)

    def _connect(self) -> sqlite3.Connection:
        """Open the database; a corrupt file is deleted and rebuilt; a hostile
        filesystem falls back to an in-memory database (index lost, never a crash)."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (0, 1):
            try:
                conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=5000")
                except sqlite3.DatabaseError:
                    pass
                conn.executescript(_SCHEMA)
                conn.commit()
                return conn
            except sqlite3.DatabaseError:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                if attempt == 0:  # corrupt db: drop and rebuild from scratch
                    logger.warning("code_index.db corrupt — rebuilding: %s", self._db_path)
                    for suffix in ("", "-wal", "-shm"):
                        Path(str(self._db_path) + suffix).unlink(missing_ok=True)
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    # ---- indexing ----

    def index_workspace(self, root: Path | str, *, max_files: int = 2000) -> dict[str, int]:
        """Incrementally (re)index every eligible file under ``root``.

        Files whose (path, mtime, size) match the last index pass are left alone;
        changed files are re-chunked and re-embedded (old chunks replaced); files that
        vanished from disk are pruned from the index. Returns
        ``{"indexed": n, "skipped": n, "unchanged": n}``.
        """
        out = {"indexed": 0, "skipped": 0, "unchanged": 0}
        try:
            root = Path(root)
            if not root.is_dir():
                return out
            known = self._known_files()
            seen: set[str] = set()
            capped = False
            for path in self._walk(root):
                if out["indexed"] + out["unchanged"] >= max_files:
                    capped = True  # partial pass: unvisited files are NOT deletions
                    break
                rel = str(path.relative_to(root))
                try:
                    stat = path.stat()
                except OSError:
                    out["skipped"] += 1
                    continue
                prior = known.get(rel)
                if prior is not None and prior == (stat.st_mtime, stat.st_size):
                    seen.add(rel)
                    out["unchanged"] += 1
                    continue
                text = _eligible_text(path)
                if text is None:
                    out["skipped"] += 1
                    continue  # not seen: a previously-indexed file gone binary is pruned
                self._index_file(rel, text, stat.st_mtime, stat.st_size)
                seen.add(rel)
                out["indexed"] += 1
            if not capped:  # prune deletions only after a complete pass
                for stale in set(known) - seen:
                    self._prune(stale)
        except Exception:  # noqa: BLE001 — indexing is best-effort by contract
            logger.debug("index_workspace degraded", exc_info=True)
        return out

    def _walk(self, root: Path) -> list[Path]:
        """Sorted files under root, internal/dependency directories pruned."""
        out: list[Path] = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and not _SKIP_DIRS.intersection(path.relative_to(root).parts):
                out.append(path)
        return out

    def _index_file(self, rel: str, text: str, mtime: float, size: int) -> None:
        chunks = chunk_text(text)
        self._vectors.delete_prefix(_NAMESPACE, rel + "::")
        try:
            vectors = self._embedder.embed([c for _s, _e, c in chunks]) if chunks else []
        except Exception:  # noqa: BLE001 — embedder down: lexical-only rows (dim 0
            vectors = [[] for _ in chunks]  # never matches a query dim -> lexical leg)
        for (start, end, chunk), vec in zip(chunks, vectors):
            self._vectors.add_vector(_NAMESPACE, f"{rel}::{start}-{end}", vec, chunk)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO files(path, mtime, size, chunks) VALUES (?, ?, ?, ?)",
                (rel, mtime, size, len(chunks)),
            )
            self._conn.commit()

    def _known_files(self) -> dict[str, tuple[float, int]]:
        rows = self._conn.execute("SELECT path, mtime, size FROM files").fetchall()
        return {r["path"]: (float(r["mtime"]), int(r["size"])) for r in rows}

    def _prune(self, rel: str) -> None:
        self._vectors.delete_prefix(_NAMESPACE, rel + "::")
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE path = ?", (rel,))
            self._conn.commit()

    # ---- retrieval ----

    def search(self, query: str, top_k: int = 8) -> list[dict]:
        """Hybrid (cosine + lexical RRF) retrieval over indexed chunks.

        Returns ``[{path, start_line, end_line, text, score}]`` best-first; the query
        is embedded once inside ``search_hybrid``. Never raises: an unavailable
        embedder means the lexical leg alone ranks, and any storage error returns [].
        """
        try:
            hits = self._vectors.search_hybrid(_NAMESPACE, query, top_k=top_k)
        except Exception:  # noqa: BLE001 — retrieval is best-effort by contract
            logger.debug("search degraded", exc_info=True)
            return []
        out: list[dict] = []
        for ref_id, score, text in hits:
            path, _sep, span = ref_id.rpartition("::")
            try:
                start_s, _dash, end_s = span.partition("-")
                start, end = int(start_s), int(end_s)
            except ValueError:
                continue  # unparseable ref (foreign row) — drop rather than guess
            out.append({"path": path, "start_line": start, "end_line": end,
                        "text": text, "score": round(float(score), 4)})
        return out

    def retrieval_context(self, query: str, budget_chars: int = 2000) -> str | None:
        """A budget-bounded "relevant code" block for the engine's context assembly.

        Exact format (the engine appends this verbatim as an UNTRUSTED context part
        next phase — retrieved file content must never be treated as instructions)::

            relevant code (from the project code index; may be stale):
            --- <path>:<start_line>-<end_line> ---
            <chunk text>
            --- <path>:<start_line>-<end_line> ---
            <chunk text, tail-clipped to fit with a trailing "…">

        One ``--- path:start-end ---`` header per retrieved chunk, best match first.
        The whole string is <= ``budget_chars``; a chunk that only partially fits is
        clipped at a line boundary and suffixed with "…"; a chunk whose header+first
        line don't fit is dropped. Returns None when nothing was retrieved or the
        budget can't fit a single chunk line.
        """
        hits = self.search(query, top_k=8)
        if not hits:
            return None
        parts = ["relevant code (from the project code index; may be stale):"]
        used = len(parts[0]) + 1
        for hit in hits:
            header = f"--- {hit['path']}:{hit['start_line']}-{hit['end_line']} ---"
            remaining = budget_chars - used - len(header) - 1
            if remaining <= 0:
                break
            lines = hit["text"].splitlines()
            body: list[str] = []
            body_len = 0
            for i, line in enumerate(lines):
                cost = len(line) + 1
                if body_len + cost > remaining - (2 if i < len(lines) - 1 else 0):
                    if body:
                        body.append("…")
                        body_len += 2
                    break
                body.append(line)
                body_len += cost
            if not body or body == ["…"]:
                continue  # not even one real line fits — try a (shorter) later chunk
            parts.append(header)
            parts.extend(body)
            used += len(header) + 1 + body_len
        if len(parts) == 1:
            return None
        return "\n".join(parts)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
