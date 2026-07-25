"""Extract real code entities from a run's workspace into the knowledge graph.

Deterministic (no LLM): walks the generated files and, for Python, uses the ``ast`` module
to record files, the functions/classes they define, and what they import. This turns the
graph from a task/agent skeleton into an actual map of the produced code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .graph import KnowledgeGraph

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build", ".ada_worktrees", ".ada_deps"}
_MAX_FILES = 80
_MAX_DEFS_PER_FILE = 50


def parse_python_outline(
    text: str, *, max_symbols: int = 50
) -> tuple[list[tuple[str, str]], list[tuple[str, int]]]:
    """Outline a Python source: top-level symbols and imports, via ``ast`` (no LLM).

    Returns ``(symbols, imports)`` where symbols are ``(name, kind)`` for top-level
    functions/classes and imports are ``(dotted_module, relative_level)`` —
    ``level`` is 0 for absolute imports, ≥1 for ``from . import x`` style.
    """
    try:
        tree = ast.parse(text)
    except Exception:
        return [], []
    symbols: list[tuple[str, str]] = []
    for node in tree.body:
        if len(symbols) >= max_symbols:
            break
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append((node.name, "function"))
        elif isinstance(node, ast.ClassDef):
            symbols.append((node.name, "class"))
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.module or "", node.level or 0))
    return symbols, imports


# Light JS/TS outline — regex, not a parser (tree-sitter isn't available here).
_JS_PATTERNS = (
    (re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", re.M), "function"),
    (re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", re.M), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)\n]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M), "function"),
    (re.compile(r"^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)", re.M), "const"),
)


def parse_js_outline(text: str, *, max_symbols: int = 50) -> list[tuple[str, str]]:
    """Top-level-ish ``(name, kind)`` symbols for JS/TS/JSX/TSX via light regex."""
    seen: set[str] = set()
    symbols: list[tuple[str, str]] = []
    for pattern, kind in _JS_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                symbols.append((name, kind))
                if len(symbols) >= max_symbols:
                    return symbols
    return symbols


def enrich_kg_from_workspace(kg: KnowledgeGraph, workspace: Path, run_id: str) -> int:
    """Add file/function/class/import facts for the run's generated files. Returns file count."""
    if not workspace.exists():
        return 0
    files = [
        p for p in sorted(workspace.rglob("*"))
        if p.is_file() and not any(d in p.parts for d in _SKIP_DIRS)
    ]
    n = 0
    for p in files[:_MAX_FILES]:
        rel = str(p.relative_to(workspace))
        kg.add_node(rel, "file")
        kg.add_fact(run_id, "produced_file", rel)
        n += 1
        if p.suffix == ".py":
            _extract_python(kg, rel, p)
    return n


def _extract_python(kg: KnowledgeGraph, rel: str, path: Path) -> None:
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except Exception:
        return
    defs = 0
    for node in ast.walk(tree):
        if defs >= _MAX_DEFS_PER_FILE:
            break
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # qualify the symbol id by file so same-named funcs across files don't collapse,
            # and set the node's type (not just an edge attr) so node_types() is correct.
            kg.add_fact(rel, "defines", f"{rel}::{node.name}", obj_type="function", kind="function")
            defs += 1
        elif isinstance(node, ast.ClassDef):
            kg.add_fact(rel, "defines", f"{rel}::{node.name}", obj_type="class", kind="class")
            defs += 1
        elif isinstance(node, ast.Import):
            for alias in node.names:
                kg.add_fact(rel, "imports", alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                kg.add_fact(rel, "imports", node.module.split(".")[0])
