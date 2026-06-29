"""Codebase onboarding (Tier 2): a token-bounded map of a real repo, plus auto-indexing
of its source into the knowledge base + graph so the orchestrator can plan against it.

Without this, planning is blind to the codebase (the orchestrator sees only the prompt +
agent roster) and the KB is dormant. This activates both.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import KnowledgeBase
from .extract import enrich_kg_from_workspace
from .graph import KnowledgeGraph

logger = logging.getLogger("ada.repomap")

_SKIP = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build", ".pytest_cache", ".ada_data"}
_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h"}
_ENTRYPOINTS = {"main.py", "app.py", "__main__.py", "cli.py", "index.js", "server.py", "manage.py"}


def build_repo_map(root: Path, *, max_files: int = 200, max_chars: int = 6000) -> str:
    """A compact tree + language inventory + likely entrypoints, bounded in size."""
    if not root.is_dir():
        return ""
    files = [p for p in sorted(root.rglob("*"))
             if p.is_file() and not any(d in p.parts for d in _SKIP)]
    langs: dict[str, int] = {}
    entry: list[str] = []
    lines: list[str] = []
    for p in files[:max_files]:
        rel = p.relative_to(root)
        ext = p.suffix.lower()
        if ext in _CODE_EXT:
            langs[ext] = langs.get(ext, 0) + 1
        if p.name in _ENTRYPOINTS:
            entry.append(str(rel))
        lines.append(str(rel))
    inv = ", ".join(f"{k}×{v}" for k, v in sorted(langs.items(), key=lambda kv: -kv[1]))
    out = [f"Repository map ({len(files)} files; languages: {inv or 'n/a'}):"]
    if entry:
        out.append("Likely entrypoints: " + ", ".join(entry[:8]))
    out.append("Files:")
    out.append("\n".join(lines))
    text = "\n".join(out)
    return text[:max_chars] + ("\n…(truncated)" if len(text) > max_chars else "")


def onboard(kb: KnowledgeBase, kg: KnowledgeGraph, root: Path, run_id: str,
            *, max_files: int = 120) -> dict[str, int]:
    """Index a repo's source into the KB (for kb_search) and KG (entities). Returns counts."""
    if not root.is_dir():
        return {"files": 0, "chunks": 0}
    n_files = enrich_kg_from_workspace(kg, root, run_id)
    chunks = 0
    indexed = 0
    for p in sorted(root.rglob("*")):
        if indexed >= max_files:
            break
        if not p.is_file() or any(d in p.parts for d in _SKIP) or p.suffix.lower() not in _CODE_EXT:
            continue
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        rel = str(p.relative_to(root))
        kb.reingest(f"repo:{rel}", text)  # idempotent: replaces prior chunks for this doc
        chunks += max(1, len(text) // 800)
        indexed += 1
    logger.info("onboarded repo: %d files into KG, %d source files into KB", n_files, indexed)
    return {"files": n_files, "chunks": chunks}
