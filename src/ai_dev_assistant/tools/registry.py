"""Tools exposed to agents during their tool-use loop.

Each tool is a small, side-effecting capability over the shared subsystems (memory,
knowledge base, knowledge graph, message bus, filesystem). A ToolBox is built per
subtask so tools are bound to that subtask's scope and the calling agent.

Tier 2 adds real file-mutation, search, execution, and git tools — all rooted at the
run's workspace (the same root reads and writes use), with a secret-file denylist and a
symlink-escape guard. Every dispatch is optionally redacted + audited (Tier 5).
"""

from __future__ import annotations

import fnmatch
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..knowledge.base import KnowledgeBase
from ..knowledge.graph import NetworkXKnowledgeGraph
from ..memory.store import MemoryStore
from ..orchestration.message_bus import MessageBus


@dataclass
class ToolContext:
    memory: MemoryStore
    kb: KnowledgeBase
    kg: NetworkXKnowledgeGraph
    bus: MessageBus
    agent_name: str
    task_scope: str
    base_dir: Path                       # the run workspace — reads AND writes are rooted here
    workspace: Path | None = None       # this run's sandbox dir (for run_tests)
    verify_timeout: float = 120.0
    redact: bool = True
    audit: Any = None                    # security.redaction.AuditLog | None
    allow_run_command: bool = True
    sandbox: str = "subprocess"
    sandbox_cpu: int = 60
    sandbox_mem: int = 1024


_MAX_FILE_CHARS = 8000
# Files agents must never read — secrets live here.
_SECRET_GLOBS = ("*.env", ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
                 "*credentials*", "*.secret", ".npmrc", ".netrc")
_SECRET_DIRS = (".ssh", ".aws", ".gnupg")


def _is_secret(rel: str) -> bool:
    parts = Path(rel).parts
    if any(d in parts for d in _SECRET_DIRS):
        return True
    name = Path(rel).name
    return any(fnmatch.fnmatch(name, g) for g in _SECRET_GLOBS)


class ToolBox:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self._handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "recall": self._recall,
            "remember": self._remember,
            "kb_search": self._kb_search,
            "kg_write": self._kg_write,
            "kg_query": self._kg_query,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "apply_patch": self._apply_patch,
            "list_dir": self._list_dir,
            "grep": self._grep,
            "run_command": self._run_command,
            "install_packages": self._install_packages,
            "git_status": self._git_status,
            "git_diff": self._git_diff,
            "send_message": self._send_message,
            "read_messages": self._read_messages,
            "blackboard_write": self._blackboard_write,
            "blackboard_read": self._blackboard_read,
            "run_tests": self._run_tests,
        }

    # ---- schema exposed to the model ----
    def definitions(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        wanted = names or list(self._handlers.keys())
        defs = [d for d in _TOOL_DEFS if d["name"] in wanted]
        if not self._ctx.allow_run_command:
            defs = [d for d in defs if d["name"] not in ("run_command", "install_packages")]
        return defs

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"ERROR: unknown tool '{name}'"
        try:
            out = handler(tool_input or {})
        except Exception as exc:  # tools must never crash the agent loop
            out = f"ERROR: tool '{name}' failed: {exc}"
        if self._ctx.redact:
            from ..security.redaction import redact

            out = redact(out)
        if self._ctx.audit is not None:
            self._ctx.audit.record(agent=self._ctx.agent_name, tool=name,
                                   args=tool_input or {}, outcome=out[:80])
        return out

    # ---- path safety ----
    def _resolve(self, rel: str) -> Path | None:
        base = self._ctx.base_dir.resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base):
            return None
        if _is_secret(str(Path(rel))):
            return None
        return target

    # ---- handlers ----
    def _recall(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        entries = self._ctx.memory.recall(self._ctx.task_scope, query, top_k=5)
        longterm = self._ctx.memory.recall("longterm", query, top_k=3, min_score=0.1)
        lines = [f"- ({e.score:.2f}) {e.content}" for e in entries]
        lines += [f"- [longterm] {e.content}" for e in longterm]
        return "\n".join(lines) if lines else "No relevant memories."

    def _remember(self, args: dict[str, Any]) -> str:
        content = str(args.get("content", "")).strip()
        if not content:
            return "ERROR: 'content' is required."
        self._ctx.memory.remember(
            self._ctx.task_scope, content, metadata={"author": self._ctx.agent_name}
        )
        return "Stored to memory."

    def _kb_search(self, args: dict[str, Any]) -> str:
        hits = self._ctx.kb.search(str(args.get("query", "")), top_k=5, min_score=0.1)
        if not hits:
            return "No knowledge-base results."
        return "\n".join(f"- ({h.score:.2f}) [{h.ref_id}] {h.text[:300]}" for h in hits)

    def _kg_write(self, args: dict[str, Any]) -> str:
        subject = str(args.get("subject", "")).strip()
        relation = str(args.get("relation", "")).strip()
        obj = str(args.get("object", "")).strip()
        if not (subject and relation and obj):
            return "ERROR: 'subject', 'relation', and 'object' are all required."
        self._ctx.kg.add_fact(subject, relation, obj, source=self._ctx.agent_name)
        return f"Recorded: ({subject}) -[{relation}]-> ({obj})"

    def _kg_query(self, args: dict[str, Any]) -> str:
        node = str(args.get("node", "")).strip()
        triples = self._ctx.kg.facts_about(node)
        if not triples:
            return f"No facts about '{node}'."
        return "\n".join(f"- ({t.subject}) -[{t.relation}]-> ({t.object})" for t in triples)

    def _read_file(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        if not rel:
            return "ERROR: 'path' is required."
        target = self._resolve(rel)
        if target is None:
            return "ERROR: path escapes the workspace or is a protected/secret file."
        if target.is_symlink():
            return "ERROR: refusing to read a symlink."
        if not target.is_file():
            return f"ERROR: no such file: {rel}"
        text = target.read_text(errors="replace")
        if len(text) > _MAX_FILE_CHARS:
            text = text[:_MAX_FILE_CHARS] + "\n...[truncated]"
        return text

    def _write_file(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        content = args.get("content", "")
        if not rel:
            return "ERROR: 'path' is required."
        target = self._resolve(rel)
        if target is None:
            return "ERROR: path escapes the workspace or is a protected/secret file."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if isinstance(content, str) else str(content))
        return f"Wrote {len(content)} chars to {rel}."

    def _edit_file(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        target = self._resolve(rel)
        if target is None or not target.is_file():
            return f"ERROR: cannot edit {rel} (missing, protected, or escapes workspace)."
        text = target.read_text(errors="replace")
        if old and old not in text:
            return "ERROR: 'old' string not found in the file; no change made."
        target.write_text(text.replace(old, new, 1) if old else text + new)
        return f"Edited {rel}."

    def _apply_patch(self, args: dict[str, Any]) -> str:
        patch = str(args.get("patch", ""))
        if not patch.strip():
            return "ERROR: 'patch' is required (unified diff)."
        if self._ctx.workspace is None:
            return "ERROR: no workspace configured."
        from ..execution import run_command_sync

        pfile = self._ctx.workspace / ".ada_patch.diff"
        pfile.write_text(patch)
        res = run_command_sync(["git", "apply", "--whitespace=nowarn", str(pfile)],
                               self._ctx.workspace, 30)
        pfile.unlink(missing_ok=True)
        if res.passed:
            return "Patch applied."
        return f"ERROR applying patch: {res.stderr or res.stdout}"

    def _list_dir(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", ".")).strip() or "."
        target = self._resolve(rel)
        if target is None or not target.is_dir():
            return f"ERROR: not a directory: {rel}"
        items = []
        for p in sorted(target.iterdir()):
            if p.name in ("__pycache__", ".git"):
                continue
            items.append(f"{'d' if p.is_dir() else 'f'} {p.relative_to(self._ctx.base_dir)}")
        return "\n".join(items) or "(empty)"

    def _grep(self, args: dict[str, Any]) -> str:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return "ERROR: 'pattern' is required."
        sub = str(args.get("path", ".")).strip() or "."
        root = self._resolve(sub)
        if root is None or not root.exists():
            return f"ERROR: bad path: {sub}"
        hits: list[str] = []
        roots = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for p in roots:
            if any(d in p.parts for d in ("__pycache__", ".git")) or p.stat().st_size > 1_000_000:
                continue
            try:
                for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    if pattern in line:
                        hits.append(f"{p.relative_to(self._ctx.base_dir)}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 100:
                            return "\n".join(hits) + "\n…(truncated)"
            except Exception:
                continue
        return "\n".join(hits) if hits else f"No matches for '{pattern}'."

    def _run_command(self, args: dict[str, Any]) -> str:
        if not self._ctx.allow_run_command or self._ctx.workspace is None:
            return "ERROR: run_command is disabled for this run."
        cmd = args.get("command", "")
        argv = cmd if isinstance(cmd, list) else shlex.split(str(cmd))
        if not argv:
            return "ERROR: 'command' is required."
        from ..execution import run_command_sync

        res = run_command_sync(argv, self._ctx.workspace, self._ctx.verify_timeout,
                               sandbox=self._ctx.sandbox != "none",
                               cpu_seconds=self._ctx.sandbox_cpu, mem_mb=self._ctx.sandbox_mem)
        tag = "TIMEOUT" if res.timed_out else ("OK" if res.passed else "FAIL")
        out = (res.stdout or "") + (("\n[stderr]\n" + res.stderr) if res.stderr else "")
        return f"$ {res.command}\nexit={res.return_code} [{tag}]\n{out[-3000:]}"

    def _install_packages(self, args: dict[str, Any]) -> str:
        if not self._ctx.allow_run_command or self._ctx.workspace is None:
            return "ERROR: install_packages is disabled for this run."
        import sys

        pkgs = args.get("packages", [])
        pkgs = pkgs if isinstance(pkgs, list) else shlex.split(str(pkgs))
        if not pkgs:
            return "ERROR: 'packages' is required."
        from ..execution import run_command_sync

        res = run_command_sync([sys.executable, "-m", "pip", "install", *pkgs],
                               self._ctx.workspace, max(60.0, self._ctx.verify_timeout), sandbox=False)
        return f"exit={res.return_code}\n{(res.stdout or res.stderr)[-1500:]}"

    def _git_status(self, args: dict[str, Any]) -> str:
        if self._ctx.workspace is None:
            return "No workspace."
        from .. import vcs

        return vcs.status(self._ctx.workspace) or "(clean)"

    def _git_diff(self, args: dict[str, Any]) -> str:
        if self._ctx.workspace is None:
            return "No workspace."
        from .. import vcs

        return vcs.diff(self._ctx.workspace) or "(no changes)"

    def _send_message(self, args: dict[str, Any]) -> str:
        recipient = args.get("recipient") or None
        content = str(args.get("content", "")).strip()
        if not content:
            return "ERROR: 'content' is required."
        self._ctx.bus.send(self._ctx.agent_name, recipient, content)
        return f"Message sent to {recipient or 'everyone'}."

    def _read_messages(self, args: dict[str, Any]) -> str:
        msgs = self._ctx.bus.inbox(self._ctx.agent_name)
        if not msgs:
            return "Inbox empty."
        return "\n".join(f"- from {m.sender}: {m.content}" for m in msgs)

    def _blackboard_write(self, args: dict[str, Any]) -> str:
        key = str(args.get("key", "")).strip()
        value = str(args.get("value", ""))
        if not key:
            return "ERROR: 'key' is required."
        self._ctx.memory.blackboard_put(key, value, self._ctx.agent_name)
        return f"Blackboard['{key}'] set."

    def _blackboard_read(self, args: dict[str, Any]) -> str:
        key = str(args.get("key", "")).strip()
        value = self._ctx.memory.blackboard_get(key)
        return value if value is not None else f"No blackboard entry for '{key}'."

    def _run_tests(self, args: dict[str, Any]) -> str:
        from ..execution import run_workspace_tests_sync

        if self._ctx.workspace is None:
            return "No workspace configured for this run."
        res = run_workspace_tests_sync(self._ctx.workspace, self._ctx.verify_timeout)
        if res is None:
            return "No runnable tests detected in the workspace yet."
        status = "PASS" if res.passed else ("TIMEOUT" if res.timed_out else "FAIL")
        out = res.stdout or ""
        if res.stderr:
            out += "\n[stderr]\n" + res.stderr
        return (f"$ {res.command}\nexit={res.return_code} [{status}] in {res.duration:.1f}s\n"
                f"--- output (tail) ---\n{out[-2500:]}")


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "recall",
        "description": "Semantically search this task's working memory for relevant prior notes.",
        "input_schema": _obj({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "remember",
        "description": "Save an important note or finding to this task's working memory.",
        "input_schema": _obj({"content": {"type": "string"}}, ["content"]),
    },
    {
        "name": "kb_search",
        "description": "Search the knowledge base (ingested docs/code) for relevant context.",
        "input_schema": _obj({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "kg_write",
        "description": "Record a fact in the knowledge graph as a (subject, relation, object) triple.",
        "input_schema": _obj(
            {
                "subject": {"type": "string"},
                "relation": {"type": "string"},
                "object": {"type": "string"},
            },
            ["subject", "relation", "object"],
        ),
    },
    {
        "name": "kg_query",
        "description": "List known facts about an entity in the knowledge graph.",
        "input_schema": _obj({"node": {"type": "string"}}, ["node"]),
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace (relative path).",
        "input_schema": _obj({"path": {"type": "string"}}, ["path"]),
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file in the workspace with the given content (relative path).",
        "input_schema": _obj({"path": {"type": "string"}, "content": {"type": "string"}},
                             ["path", "content"]),
    },
    {
        "name": "edit_file",
        "description": "Replace the first occurrence of 'old' with 'new' in a workspace file "
                       "(or append 'new' if 'old' is empty).",
        "input_schema": _obj({"path": {"type": "string"}, "old": {"type": "string"},
                              "new": {"type": "string"}}, ["path", "new"]),
    },
    {
        "name": "apply_patch",
        "description": "Apply a unified diff (git apply) to the workspace. Use for multi-file edits.",
        "input_schema": _obj({"patch": {"type": "string"}}, ["patch"]),
    },
    {
        "name": "list_dir",
        "description": "List files and directories under a workspace path (default: the root).",
        "input_schema": _obj({"path": {"type": "string"}}, []),
    },
    {
        "name": "grep",
        "description": "Search workspace files for a substring; returns matching path:line: text.",
        "input_schema": _obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the sandboxed workspace (scrubbed env + resource "
                       "limits) and return its exit code + output. Use for builds/tools.",
        "input_schema": _obj({"command": {"type": "string"}}, ["command"]),
    },
    {
        "name": "install_packages",
        "description": "pip-install one or more Python packages into the environment for this run.",
        "input_schema": _obj({"packages": {"type": "string"}}, ["packages"]),
    },
    {
        "name": "git_status",
        "description": "Show the workspace's git status (changed files).",
        "input_schema": _obj({}, []),
    },
    {
        "name": "git_diff",
        "description": "Show a diffstat of the workspace's uncommitted changes.",
        "input_schema": _obj({}, []),
    },
    {
        "name": "send_message",
        "description": "Send a message to another agent by name, or omit recipient to broadcast.",
        "input_schema": _obj(
            {"recipient": {"type": "string"}, "content": {"type": "string"}}, ["content"]
        ),
    },
    {
        "name": "read_messages",
        "description": "Read and clear your inbox of messages from other agents.",
        "input_schema": _obj({}, []),
    },
    {
        "name": "blackboard_write",
        "description": "Write a value to the shared blackboard so other agents can read it.",
        "input_schema": _obj({"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    },
    {
        "name": "blackboard_read",
        "description": "Read a value other agents left on the shared blackboard.",
        "input_schema": _obj({"key": {"type": "string"}}, ["key"]),
    },
    {
        "name": "run_tests",
        "description": "Run the project's tests in the workspace and return the exit code + output. "
                       "Use this to objectively verify code you or a teammate wrote.",
        "input_schema": _obj({}, []),
    },
]
