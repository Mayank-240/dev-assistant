"""Tools exposed to agents during their tool-use loop.

Each tool is a small, side-effecting capability over the shared subsystems (memory,
knowledge base, knowledge graph, message bus, filesystem). A ToolBox is built per
subtask so tools are bound to that subtask's scope and the calling agent.
"""

from __future__ import annotations

import json
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
    base_dir: Path
    workspace: Path | None = None       # this run's sandbox dir (for run_tests)
    verify_timeout: float = 120.0


_MAX_FILE_CHARS = 8000


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
            "send_message": self._send_message,
            "read_messages": self._read_messages,
            "blackboard_write": self._blackboard_write,
            "blackboard_read": self._blackboard_read,
            "run_tests": self._run_tests,
        }

    # ---- schema exposed to the model ----
    def definitions(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        wanted = names or list(self._handlers.keys())
        return [d for d in _TOOL_DEFS if d["name"] in wanted]

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"ERROR: unknown tool '{name}'"
        try:
            return handler(tool_input or {})
        except Exception as exc:  # tools must never crash the agent loop
            return f"ERROR: tool '{name}' failed: {exc}"

    # ---- handlers ----
    def _recall(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        entries = self._ctx.memory.recall(self._ctx.task_scope, query, top_k=5)
        longterm = self._ctx.memory.recall("longterm", query, top_k=3)
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
        hits = self._ctx.kb.search(str(args.get("query", "")), top_k=5)
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
        base = self._ctx.base_dir.resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base):
            return "ERROR: path escapes the working directory."
        if not target.is_file():
            return f"ERROR: no such file: {rel}"
        text = target.read_text(errors="replace")
        if len(text) > _MAX_FILE_CHARS:
            text = text[:_MAX_FILE_CHARS] + "\n...[truncated]"
        return text

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
        "description": "Read a UTF-8 text file from the working directory (relative path).",
        "input_schema": _obj({"path": {"type": "string"}}, ["path"]),
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
