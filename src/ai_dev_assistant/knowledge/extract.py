"""Extract real code entities from a run's workspace into the knowledge graph.

Deterministic (no LLM): walks the generated files and, for Python, uses the ``ast`` module
to record files, the functions/classes they define, and what they import. This turns the
graph from a task/agent skeleton into an actual map of the produced code.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .graph import KnowledgeGraph

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build"}
_MAX_FILES = 80
_MAX_DEFS_PER_FILE = 50


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
            kg.add_fact(rel, "defines", node.name, kind="function")
            defs += 1
        elif isinstance(node, ast.ClassDef):
            kg.add_fact(rel, "defines", node.name, kind="class")
            defs += 1
        elif isinstance(node, ast.Import):
            for alias in node.names:
                kg.add_fact(rel, "imports", alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                kg.add_fact(rel, "imports", node.module.split(".")[0])
