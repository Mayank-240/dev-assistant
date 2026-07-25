"""Codebase onboarding (Tier 2): a token-bounded map of a real repo, plus auto-indexing
of its source into the knowledge base + graph so the orchestrator can plan against it.

Without this, planning is blind to the codebase (the orchestrator sees only the prompt +
agent roster) and the KB is dormant. This activates both.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from pathlib import Path

from .base import KnowledgeBase
from .extract import enrich_kg_from_workspace, parse_js_outline, parse_python_outline
from .graph import KnowledgeGraph

logger = logging.getLogger("ada.repomap")

_SKIP = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build", ".pytest_cache", ".ada_data", ".ada_worktrees", ".ada_deps"}
_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h"}
_JS_EXT = {".js", ".ts", ".tsx", ".jsx"}
_ENTRYPOINTS = {"main.py", "app.py", "__main__.py", "cli.py", "index.js", "server.py", "manage.py"}

# Hard caps so the map stays well under ~2s even on large repos.
_MAX_WALK = 5000          # files collected before the walk stops
_MAX_SOURCE_PARSED = 300  # source files whose symbols/imports get extracted
_MAX_READ_BYTES = 40_000  # per-file read cap for symbol extraction
_SYMBOLS_SHOWN = 8        # symbols rendered per file line
_SUMMARY_RESERVE = 90     # chars kept back for the "…N more files omitted" line

_WORD = re.compile(r"[A-Za-z0-9]+")


def _query_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for raw in _WORD.findall(text):
        out.add(raw.lower())
        for part in re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw).split():
            out.add(part.lower())  # split camelCase so "RepoMap" matches "repo map"
    return out


def _walk(root: Path) -> list[Path]:
    """Root-relative files, skip dirs pruned, capped at _MAX_WALK."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP)
        for name in sorted(filenames):
            files.append(Path(dirpath, name).relative_to(root))
            if len(files) >= _MAX_WALK:
                return files
    return files


def _resolve_module(dotted: str, modules: dict[str, Path]) -> Path | None:
    """Match a dotted import to a repo file, falling back to its package prefixes."""
    while dotted:
        if dotted in modules:
            return modules[dotted]
        if "." not in dotted:
            return None
        dotted = dotted.rsplit(".", 1)[0]
    return None


def build_repo_map(workspace: Path, max_chars: int = 6000, query: str | None = None) -> str:
    """A ranked, symbol-annotated repo map bounded to ``max_chars``.

    Files are ranked by degree centrality on the Python import graph (being imported
    weighs more than importing), boosted by token overlap with ``query`` when given,
    with a small bonus for likely entrypoints. The top files render as
    ``path — key symbols`` lines, most relevant first, followed by a compact count of
    what was omitted — never a silent alphabetical cut, and never mid-line.
    """
    root = Path(workspace)
    if not root.is_dir():
        return ""
    files = _walk(root)

    # Symbol + import extraction (Python via ast, JS/TS via light regex), byte-capped.
    symbols: dict[Path, list[str]] = {}
    imports: dict[Path, list[tuple[str, int]]] = {}
    parsed = 0
    for p in files:
        ext = p.suffix.lower()
        if parsed >= _MAX_SOURCE_PARSED or (ext != ".py" and ext not in _JS_EXT):
            continue
        try:
            with open(root / p, "rb") as fh:
                text = fh.read(_MAX_READ_BYTES).decode("utf-8", errors="replace")
        except OSError:
            continue
        if ext == ".py":
            syms, imps = parse_python_outline(text)
            symbols[p] = [name for name, _kind in syms]
            imports[p] = imps
        else:
            symbols[p] = [name for name, _kind in parse_js_outline(text)]
        parsed += 1

    # Python import graph → degree centrality (in-edges weighted over out-edges).
    modules: dict[str, Path] = {}
    for p in imports:
        parts = list(p.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules[".".join(parts)] = p
    degree: Counter[Path] = Counter()
    for p, imps in imports.items():
        package = list(p.parts[:-1])
        for mod, level in imps:
            if level:  # relative import: anchor at the file's package
                base = package[: len(package) - (level - 1)] if level > 1 else package
                dotted = ".".join([*base, *[s for s in mod.split(".") if s]])
            else:
                dotted = mod
            target = _resolve_module(dotted, modules)
            if target is not None and target != p:
                degree[target] += 2  # imported-by: strong signal of a load-bearing file
                degree[p] += 1       # importer: weaker signal (hubs like cli/main)

    # Rank: centrality + query overlap (+ entrypoint bonus); ties break by path.
    max_degree = max(degree.values(), default=0) or 1
    q_tokens = _query_tokens(query) if query else set()
    scored: list[tuple[float, Path]] = []
    for p in files:
        score = degree.get(p, 0) / max_degree
        if q_tokens:
            f_tokens = _query_tokens(str(p)) | _query_tokens(" ".join(symbols.get(p, ())))
            score += 2.0 * len(q_tokens & f_tokens) / len(q_tokens)
        if p.name in _ENTRYPOINTS:
            score += 0.2
        scored.append((score, p))
    scored.sort(key=lambda item: (-item[0], str(item[1])))

    # Render within budget, whole lines only, then summarize the remainder.
    langs = Counter(p.suffix.lower() for p in files if p.suffix.lower() in _CODE_EXT)
    inv = ", ".join(f"{k}×{v}" for k, v in sorted(langs.items(), key=lambda kv: -kv[1]))
    out_lines = [f"Repository map ({len(files)} files; languages: {inv or 'n/a'}):"]
    entry = [str(p) for p in files if p.name in _ENTRYPOINTS]
    if entry:
        out_lines.append("Likely entrypoints: " + ", ".join(entry[:8]))
    out_lines.append("Ranked files (most relevant first):")
    used = sum(len(line) + 1 for line in out_lines)
    shown = 0
    for _score, p in scored:
        syms = symbols.get(p) or []
        line = str(p)
        if syms:
            line += " — " + ", ".join(syms[:_SYMBOLS_SHOWN]) + (", …" if len(syms) > _SYMBOLS_SHOWN else "")
        if used + len(line) + 1 > max_chars - _SUMMARY_RESERVE:
            break
        out_lines.append(line)
        used += len(line) + 1
        shown += 1
    omitted = len(scored) - shown
    if omitted > 0:
        counts = Counter((p.suffix.lower() or "other") for _score, p in scored[shown:])
        detail = ", ".join(f"{n}×{ext}" for ext, n in counts.most_common(4))
        out_lines.append(f"…{omitted} more files omitted ({detail})")
    while out_lines and len("\n".join(out_lines)) > max_chars:
        out_lines.pop()  # tiny budgets: drop whole lines rather than cut one mid-line
    return "\n".join(out_lines)


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
