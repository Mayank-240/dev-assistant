"""Tools exposed to agents during their tool-use loop.

Each tool is a small, side-effecting capability over the shared subsystems (memory,
knowledge base, knowledge graph, message bus, filesystem). A ToolBox is built per
subtask so tools are bound to that subtask's scope and the calling agent.

Tier 2 adds real file-mutation, search, execution, and git tools — all rooted at the
run's workspace (the same root reads and writes use), with a secret-file denylist and a
symlink-escape guard. Every dispatch is optionally redacted + audited (Tier 5).

T3–T5 add symbol navigation (symbols / find_references), delegation to peer specialists
(delegate, via ctx.spawn), and an SSRF-guarded, gated web fetch (web_fetch).
"""

from __future__ import annotations

import asyncio
import fnmatch
import html
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..knowledge.base import KnowledgeBase
from ..knowledge.extract import parse_js_outline, parse_python_outline
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
    allow_web: bool = False              # gates web_fetch (env ADA_ALLOW_WEB=true is the fallback)
    # Project-policy globs (workspace-relative, e.g. "infra/**", "*.lock") that the
    # mutating file tools (write_file/edit_file/apply_patch) refuse to touch (F7).
    protected_paths: tuple[str, ...] = ()
    # (agent_name, task) -> result. Wired by the engine with depth limiting; None disables delegate.
    spawn: Callable[[str, str], Awaitable[str]] | None = None


_MAX_FILE_CHARS = 8000
_MAX_GREP_CHARS = 8000
_MAX_DELEGATE_TASK_CHARS = 4000
_WEB_TIMEOUT = 15.0
_WEB_MAX_BYTES = 100_000
# Files the symbols index outlines (Python via ast, the rest via the light JS regex).
_SOURCE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
# Files find_references reports hits in — source code, not prose/artifacts.
_REF_EXTS = _SOURCE_EXTS + (".java", ".go", ".rb", ".rs", ".c", ".h", ".cpp", ".hpp",
                            ".cs", ".php", ".swift", ".kt", ".scala", ".sh")
_MAX_SYMBOL_FILES = 200
# Where install_packages puts per-run deps (importable via PYTHONPATH, see _deps_env).
_DEPS_DIR = ".ada_deps"
# A plain PEP 508-ish requirement: name, optional extras, optional version specifiers.
# Deliberately excludes flags, URLs, paths, and markers — those all reach pip as-is.
_REQ_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"          # distribution name
    r"(?:\[[A-Za-z0-9._, -]+\])?"                             # optional extras
    r"(?:\s*(?:==|!=|<=|>=|~=|===|<|>)\s*[A-Za-z0-9.*+!_-]+"  # optional version spec(s)
    r"(?:\s*,\s*(?:==|!=|<=|>=|~=|===|<|>)\s*[A-Za-z0-9.*+!_-]+)*)?$"
)
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


def _matches_protected(rel: str, patterns: tuple[str, ...]) -> str | None:
    """The first protected-path pattern matching this workspace-relative path, or None.

    Patterns are fnmatch globs against the POSIX-style relative path. Directory
    prefixes match too, so both "infra" and "infra/**" block "infra/x/y.tf".
    """
    parts = [p for p in Path(rel).as_posix().split("/") if p not in ("", ".")]
    posix = "/".join(parts)
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    for pat in patterns:
        p = str(pat).strip().strip("/")
        if not p:
            continue
        base = p[:-3] if p.endswith("/**") else p
        if fnmatch.fnmatch(posix, p) or any(fnmatch.fnmatch(pre, base) for pre in prefixes):
            return pat
    return None


def _patch_paths(patch: str) -> list[str]:
    """Target paths named by a unified diff's `--- a/<path>` / `+++ b/<path>` lines."""
    paths: list[str] = []
    for line in patch.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        p = line[4:].split("\t", 1)[0].strip().strip('"')
        if not p or p == "/dev/null":
            continue
        if p.startswith(("a/", "b/")):
            p = p[2:]
        if p not in paths:
            paths.append(p)
    return paths


def _valid_requirement(spec: str) -> bool:
    """True only for a plain requirement (name[extras]specifiers) — never a pip flag,
    URL, VCS ref, or path, all of which would hand pip attacker-chosen behavior."""
    s = spec.strip()
    if not s or s.startswith("-"):
        return False
    if "/" in s or "\\" in s or "://" in s or "git+" in s:
        return False
    return bool(_REQ_RE.match(s))


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _await_from_sync(awaitable: Awaitable[str]) -> str:
    """Resolve an awaitable from inside a synchronous tool handler.

    Both providers invoke ToolBox.dispatch synchronously on the event-loop thread
    (anthropic_provider's tool loop; the SDK's in-process MCP tool functions), so when a
    loop is already running here the coroutine is executed on a fresh loop in a worker
    thread and the caller blocks on the result — the same trade-off the subprocess-backed
    tools (run_command/run_tests) already make. With no running loop (plain sync call
    sites, tests) asyncio.run() suffices. The awaitable must therefore be loop-agnostic:
    it must not touch primitives bound to the outer loop.
    """
    async def _wrap() -> str:
        return await awaitable

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_wrap())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _wrap()).result()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects: the Location is reported to the model instead, so a
    public host can never bounce a fetch onto a private/metadata address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # makes the opener raise HTTPError for the 3xx


def _urlopen_no_redirect(url: str, timeout: float):
    """Open a URL with redirects disabled (module-level so tests can monkeypatch it)."""
    request = urllib.request.Request(url, headers={"User-Agent": "ai-dev-assistant"})
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _resolve_addrs(host: str) -> list[str]:
    """Every address the host resolves to — the SSRF gate must check them all. The later
    urlopen re-resolves, so a DNS-rebinding TOCTOU remains (accepted, documented)."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _blocked_address(addr: str) -> bool:
    """True for private/loopback/link-local (incl. 169.254.169.254 metadata)/reserved/
    multicast/unspecified addresses — anything an SSRF must not reach. Fails closed."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])  # strip any IPv6 scope id
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1 must not slip through
    if mapped is not None:
        ip = mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


def _strip_html(text: str) -> str:
    """Naive tag strip — enough to turn a page into readable text, not a parser."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


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
            "symbols": self._symbols,
            "find_references": self._find_references,
            "delegate": self._delegate,
            "web_fetch": self._web_fetch,
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

    def _deny_protected(self, rel: str, action: str = "write") -> str | None:
        """A DENIED refusal when the workspace-relative path hits a protected-path
        policy glob, else None. The exact "DENIED: protected path" prefix is load-
        bearing: the web Permissions panel greps audit lines for "DENIED:"."""
        pat = _matches_protected(rel, self._ctx.protected_paths)
        if pat is None:
            return None
        return (f"DENIED: protected path '{Path(rel).as_posix()}' matches project "
                f"policy pattern '{pat}' — {action} refused.")

    @contextmanager
    def _deps_env(self):
        """Expose <workspace>/.ada_deps on PYTHONPATH while a child process runs, so
        packages from install_packages are importable (the scrubbed env keeps PYTHONPATH)."""
        ws = self._ctx.workspace
        deps = (ws / _DEPS_DIR) if ws is not None else None
        if deps is None or not deps.is_dir():
            yield
            return
        prev = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(deps) + ((os.pathsep + prev) if prev else "")
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = prev

    def _run_with_tag(self, argv: list[str], cwd: Path, timeout: float, **kwargs: Any):
        """run_command_sync, tagging the child's process group with this subtask's scope
        so a cancelled run can kill everything via terminate_tag(tag). The tag kwarg
        lands in a concurrent change to execution.py — until both are integrated, fall
        back to an untagged call."""
        from ..execution import run_command_sync

        try:
            return run_command_sync(argv, cwd, timeout, tag=self._ctx.task_scope, **kwargs)
        except TypeError:
            return run_command_sync(argv, cwd, timeout, **kwargs)

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
            return "DENIED: path escapes the workspace or is a protected/secret file."
        if target.is_symlink():
            return "ERROR: refusing to read a symlink."
        if not target.is_file():
            return f"ERROR: no such file: {rel}"
        lines = target.read_text(errors="replace").splitlines()
        total = len(lines)
        try:
            offset = max(1, int(args.get("offset") or 1))
            limit = int(args["limit"]) if args.get("limit") else None
        except (TypeError, ValueError):
            return "ERROR: 'offset' and 'limit' must be integers."
        if offset > total and total:
            return f"ERROR: offset {offset} is past the end of the file ({total} lines)."
        chunk = lines[offset - 1:offset - 1 + limit] if limit else lines[offset - 1:]
        text = "\n".join(chunk)
        shown = len(chunk)
        if len(text) > _MAX_FILE_CHARS:
            text = text[:_MAX_FILE_CHARS]
            shown = text.count("\n") + 1
            text += (f"\n...[truncated at {_MAX_FILE_CHARS} chars — "
                     f"call again with offset={offset + shown} to continue]")
        header = f"[lines {offset}-{offset + shown - 1} of {total}]\n" \
            if (offset > 1 or shown < total) else ""
        from ..security.redaction import untrusted

        return untrusted(header + text, source=f"file:{rel}")

    def _write_file(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        content = args.get("content", "")
        if not rel:
            return "ERROR: 'path' is required."
        target = self._resolve(rel)
        if target is None:
            return "DENIED: path escapes the workspace or is a protected/secret file."
        denied = self._deny_protected(str(target.relative_to(self._ctx.base_dir.resolve())))
        if denied is not None:
            return denied
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if isinstance(content, str) else str(content))
        return f"Wrote {len(content)} chars to {rel}."

    def _edit_file(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        target = self._resolve(rel)
        if target is None:
            return f"DENIED: cannot edit {rel} (protected, or escapes workspace)."
        denied = self._deny_protected(str(target.relative_to(self._ctx.base_dir.resolve())),
                                      action="edit")
        if denied is not None:
            return denied
        if not target.is_file():
            return f"ERROR: cannot edit {rel} (missing, protected, or escapes workspace)."
        text = target.read_text(errors="replace")
        if not old:
            target.write_text(text + new)
            return f"Edited {rel}."
        count = text.count(old)
        if count == 0:
            return "ERROR: 'old' string not found in the file; no change made."
        replace_all = bool(args.get("replace_all", False))
        if count > 1 and not replace_all:
            return (f"ERROR: 'old' occurs {count} times in {rel}; no change made. "
                    "Provide a longer, unique 'old' string or set replace_all=true.")
        target.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return f"Edited {rel} ({count if replace_all else 1} replacement(s))."

    def _apply_patch(self, args: dict[str, Any]) -> str:
        patch = str(args.get("patch", ""))
        if not patch.strip():
            return "ERROR: 'patch' is required (unified diff)."
        if self._ctx.workspace is None:
            return "ERROR: no workspace configured."
        # Refuse BEFORE applying anything if the diff touches any protected path.
        for path in _patch_paths(patch):
            denied = self._deny_protected(path, action="patch")
            if denied is not None:
                return denied
        from ..execution import run_command_sync

        pfile = self._ctx.workspace / ".ada_patch.diff"
        pfile.write_text(patch)
        # --3way survives context drift when blob info is available; plain apply is the
        # fallback for patches with no index lines (or non-git workspaces).
        res = run_command_sync(["git", "apply", "--3way", "--whitespace=nowarn", str(pfile)],
                               self._ctx.workspace, 30)
        if not res.passed:
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
        try:
            context = max(0, min(10, int(args.get("context") or 0)))
        except (TypeError, ValueError):
            return "ERROR: 'context' must be an integer."
        include = str(args.get("include", "")).strip() or None
        if shutil.which("rg"):
            out = self._grep_rg(pattern, root, context, include)
        else:
            out = self._grep_py(pattern, root, context, include)
        if len(out) > _MAX_GREP_CHARS:
            out = out[:_MAX_GREP_CHARS] + "\n…(truncated — narrow the pattern or path)"
        from ..security.redaction import untrusted

        return untrusted(out, source="grep")

    def _grep_rg(self, pattern: str, root: Path, context: int, include: str | None) -> str:
        from ..execution import run_command_sync

        cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns", "300",
               "--max-filesize", "1M"]
        if context:
            cmd += ["-C", str(context)]
        if include:
            cmd += ["--glob", include]
        cmd += ["-e", pattern]
        base = self._ctx.base_dir.resolve()
        if root != base:  # no path arg when searching the root → clean relative output
            cmd += ["--", str(root.relative_to(base))]
        res = run_command_sync(cmd, base, 30)
        if res.return_code == 0 and res.stdout.strip():
            return res.stdout.rstrip()
        if res.return_code == 1:
            return f"No matches for '{pattern}'."
        return f"ERROR: grep failed: {(res.stderr or res.stdout)[:300]}"

    def _grep_py(self, pattern: str, root: Path, context: int, include: str | None) -> str:
        """Regex fallback matching rg's path:line:/path-line- output when rg is absent."""
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: invalid regex: {exc}"
        base = self._ctx.base_dir.resolve()
        hits: list[str] = []
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for p in files:
            rel = p.relative_to(base)
            # Mirror rg defaults: skip hidden/ignored dirs, secret files, huge files.
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            if _is_secret(str(rel)) or p.is_symlink():
                continue
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                lines = p.read_text(errors="replace").splitlines()
            except Exception:
                continue
            if include and not (fnmatch.fnmatch(p.name, include) or fnmatch.fnmatch(str(rel), include)):
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    lo, hi = max(1, i - context), min(len(lines), i + context)
                    for j in range(lo, hi + 1):
                        sep = ":" if j == i else "-"
                        hits.append(f"{rel}{sep}{j}{sep}{lines[j - 1][:300]}")
                    if sum(len(h) for h in hits) > _MAX_GREP_CHARS:
                        return "\n".join(hits)
        return "\n".join(hits) if hits else f"No matches for '{pattern}'."

    def _run_command(self, args: dict[str, Any]) -> str:
        if not self._ctx.allow_run_command or self._ctx.workspace is None:
            return "ERROR: run_command is disabled for this run."
        cmd = args.get("command", "")
        argv = cmd if isinstance(cmd, list) else shlex.split(str(cmd))
        if not argv:
            return "ERROR: 'command' is required."
        with self._deps_env():
            res = self._run_with_tag(argv, self._ctx.workspace, self._ctx.verify_timeout,
                                     sandbox=self._ctx.sandbox != "none",
                                     cpu_seconds=self._ctx.sandbox_cpu, mem_mb=self._ctx.sandbox_mem)
        tag = "TIMEOUT" if res.timed_out else ("OK" if res.passed else "FAIL")
        out = (res.stdout or "") + (("\n[stderr]\n" + res.stderr) if res.stderr else "")
        return f"$ {res.command}\nexit={res.return_code} [{tag}]\n{out[-3000:]}"

    def _install_packages(self, args: dict[str, Any]) -> str:
        if not self._ctx.allow_run_command or self._ctx.workspace is None:
            return "ERROR: install_packages is disabled for this run."
        pkgs = args.get("packages", [])
        pkgs = [str(p) for p in pkgs] if isinstance(pkgs, list) else shlex.split(str(pkgs))
        if not pkgs:
            return "ERROR: 'packages' is required."
        bad = [p for p in pkgs if not _valid_requirement(p)]
        if bad:
            return ("ERROR: refusing to install non-plain requirement(s): "
                    f"{', '.join(repr(b) for b in bad)}. Only 'name[extras]==version'-style "
                    "specs are allowed — no flags, URLs, VCS refs, paths, or markers.")
        # Installs go to the workspace-local .ada_deps dir (never the host interpreter)
        # with a scrubbed env so no API keys reach pip or package build backends. Network
        # stays available — pip needs it; the env scrub is the containment here.
        deps = self._ctx.workspace / _DEPS_DIR
        res = self._run_with_tag(
            [sys.executable, "-m", "pip", "install", "--target", str(deps),
             "--disable-pip-version-check", *pkgs],
            self._ctx.workspace, max(60.0, self._ctx.verify_timeout),
            sandbox=True, cpu_seconds=max(300, self._ctx.sandbox_cpu),
            mem_mb=max(2048, self._ctx.sandbox_mem))
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
        from ..execution import detect_test_command

        if self._ctx.workspace is None:
            return "No workspace configured for this run."
        paths = args.get("paths") or None
        if paths is not None:
            paths = [str(p) for p in paths] if isinstance(paths, list) else shlex.split(str(paths))
            for p in paths:
                if self._resolve(p) is None:
                    return f"DENIED: test path escapes the workspace: {p}"
        cmd = detect_test_command(self._ctx.workspace, paths)
        if cmd is None:
            return "No runnable tests detected in the workspace yet."
        keyword = str(args.get("keyword", "")).strip()
        if keyword and "pytest" in cmd:
            cmd += ["-k", keyword]
        with self._deps_env():
            res = self._run_with_tag(cmd, self._ctx.workspace, self._ctx.verify_timeout,
                                     sandbox=self._ctx.sandbox != "none",
                                     cpu_seconds=self._ctx.sandbox_cpu, mem_mb=self._ctx.sandbox_mem)
        status = "PASS" if res.passed else ("TIMEOUT" if res.timed_out else "FAIL")
        out = res.stdout or ""
        if res.stderr:
            out += "\n[stderr]\n" + res.stderr
        return (f"$ {res.command}\nexit={res.return_code} [{status}] in {res.duration:.1f}s\n"
                f"--- output (tail) ---\n{out[-2500:]}")

    # ---- symbol navigation (T3) ----
    def _symbols(self, args: dict[str, Any]) -> str:
        from ..security.redaction import untrusted

        rel = str(args.get("path", "")).strip()
        if not rel:
            return untrusted(self._symbols_index(), source="symbols:workspace")
        target = self._resolve(rel)
        if target is None:
            return "DENIED: path escapes the workspace or is a protected/secret file."
        if target.is_symlink():
            return "ERROR: refusing to read a symlink."
        if not target.is_file():
            return f"ERROR: no such file: {rel}"
        suffix = target.suffix.lower()
        text = target.read_text(errors="replace")
        if suffix == ".py":
            syms, imports = parse_python_outline(text)
        elif suffix in _SOURCE_EXTS:
            syms, imports = parse_js_outline(text), []
        else:
            return f"ERROR: unsupported file type for symbols: {rel} (Python/JS/TS only)."
        lines = [f"{rel}:"]
        if syms:
            lines += [f"  {kind} {name}" for name, kind in syms]
        else:
            lines.append("  (no top-level symbols)")
        mods = sorted({("." * lvl) + mod for mod, lvl in imports if mod or lvl})
        if mods:
            lines.append("  imports: " + ", ".join(mods[:25]))
        out = "\n".join(lines)
        if len(out) > _MAX_FILE_CHARS:
            out = out[:_MAX_FILE_CHARS] + "\n…(truncated)"
        return untrusted(out, source=f"symbols:{rel}")

    def _symbols_index(self) -> str:
        """Whole-workspace outline, files ranked by import fan-in (how many other files
        import a module matching the file's stem), then by symbol count. Bounded."""
        base = self._ctx.base_dir.resolve()
        entries: list[tuple[str, list[tuple[str, str]], set[str]]] = []
        for p in sorted(base.rglob("*")):
            if len(entries) >= _MAX_SYMBOL_FILES:
                break
            if not p.is_file() or p.is_symlink() or p.suffix.lower() not in _SOURCE_EXTS:
                continue
            rel = p.relative_to(base)
            if any(part.startswith(".") or part in ("__pycache__", "node_modules")
                   for part in rel.parts):
                continue
            if _is_secret(str(rel)):
                continue
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                text = p.read_text(errors="replace")
            except Exception:
                continue
            if p.suffix.lower() == ".py":
                syms, imports = parse_python_outline(text)
                comps = {c for mod, _lvl in imports for c in (mod or "").split(".") if c}
            else:
                syms, comps = parse_js_outline(text), set()
            entries.append((str(rel), syms, comps))
        if not entries:
            return "(no Python/JS/TS source files found in the workspace)"

        def stem(rel: str) -> str:
            path = Path(rel)
            return path.parent.name if path.stem == "__init__" else path.stem

        fan_in = {rel: sum(1 for other, _s, comps in entries if other != rel and stem(rel) in comps)
                  for rel, _syms, _comps in entries}
        entries.sort(key=lambda e: (-fan_in[e[0]], -len(e[1]), e[0]))
        lines = ["Workspace symbol index (ranked by import fan-in; pass 'path' for one file):"]
        used = len(lines[0])
        for rel, syms, _comps in entries:
            block = [rel + (f"  [imported by {fan_in[rel]}]" if fan_in[rel] else "")]
            if syms:
                block += [f"  {kind} {name}" for name, kind in syms[:20]]
            else:
                block.append("  (no top-level symbols)")
            chunk = "\n".join(block)
            if used + len(chunk) > _MAX_FILE_CHARS:
                lines.append("…(truncated — pass a path to outline a single file)")
                break
            lines.append(chunk)
            used += len(chunk) + 1
        return "\n".join(lines)

    def _find_references(self, args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            return "ERROR: 'name' is required."
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
            return "ERROR: 'name' must be a plain identifier (letters, digits, _ or $)."
        pattern = rf"\b{re.escape(name)}\b"
        root = self._ctx.base_dir.resolve()
        raw = (self._grep_rg(pattern, root, 0, None) if shutil.which("rg")
               else self._grep_py(pattern, root, 0, None))
        if raw.startswith("ERROR"):
            return raw
        groups: dict[str, list[str]] = {}
        for line in raw.splitlines():
            path, _, rest = line.partition(":")
            lineno, _, snippet = rest.partition(":")
            if not lineno.isdigit() or Path(path).suffix.lower() not in _REF_EXTS:
                continue
            groups.setdefault(path, []).append(f"  {lineno}: {snippet.strip()[:200]}")
        if not groups:
            return f"No references to '{name}' found in source files."
        out_lines: list[str] = []
        used = 0
        for path in sorted(groups):
            chunk = "\n".join([f"{path} ({len(groups[path])} match(es)):"] + groups[path])
            if used + len(chunk) > _MAX_GREP_CHARS:
                out_lines.append("…(truncated — use grep with a narrower path instead)")
                break
            out_lines.append(chunk)
            used += len(chunk) + 1
        from ..security.redaction import untrusted

        return untrusted("\n".join(out_lines), source=f"find_references:{name}")

    # ---- delegation (T4) ----
    def _delegate(self, args: dict[str, Any]) -> str:
        agent = str(args.get("agent", "")).strip()
        task = str(args.get("task", "")).strip()
        if not agent or not task:
            return "ERROR: 'agent' and 'task' are both required."
        if self._ctx.spawn is None:
            return "delegation unavailable in this run"
        task = task[:_MAX_DELEGATE_TASK_CHARS]
        return str(_await_from_sync(self._ctx.spawn(agent, task)))

    # ---- web (T5) ----
    def _web_fetch(self, args: dict[str, Any]) -> str:
        if not (self._ctx.allow_web or _env_true("ADA_ALLOW_WEB")):
            return "web access disabled (set ADA_ALLOW_WEB=true)"
        url = str(args.get("url", "")).strip()
        if not url:
            return "ERROR: 'url' is required."
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"ERROR: only http/https URLs are allowed (got '{parsed.scheme or 'no scheme'}')."
        host = parsed.hostname
        if not host:
            return "ERROR: URL has no host."
        try:
            max_bytes = int(args.get("max_bytes") or _WEB_MAX_BYTES)
        except (TypeError, ValueError):
            return "ERROR: 'max_bytes' must be an integer."
        max_bytes = max(1, min(max_bytes, _WEB_MAX_BYTES))
        try:
            addrs = _resolve_addrs(host)
        except OSError as exc:
            return f"ERROR: could not resolve host '{host}': {exc}"
        blocked = [a for a in addrs if _blocked_address(a)]
        if blocked or not addrs:
            return (f"ERROR: refusing to fetch {url}: host resolves to a private/loopback/"
                    f"link-local/reserved address ({blocked[0] if blocked else 'unresolvable'}).")
        try:
            resp = _urlopen_no_redirect(url, _WEB_TIMEOUT)
        except urllib.error.HTTPError as exc:
            location = (exc.headers.get("Location") if exc.headers else None) or "(no Location)"
            if exc.code in (301, 302, 303, 307, 308):
                return (f"Redirect ({exc.code}) to {location} — not followed. Fetch the "
                        "target URL directly if it is a public host.")
            return f"ERROR: HTTP {exc.code} fetching {url}."
        except Exception as exc:
            return f"ERROR: fetch failed: {exc}"
        try:
            data = resp.read(max_bytes + 1)
            headers = getattr(resp, "headers", None)
            ctype = str(headers.get("Content-Type") or "") if headers is not None else ""
        finally:
            getattr(resp, "close", lambda: None)()
        truncated = len(data) > max_bytes
        text = data[:max_bytes].decode("utf-8", errors="replace")
        if "html" in ctype.lower() or "<html" in text[:2000].lower():
            text = _strip_html(text)
        if truncated:
            text += f"\n…[truncated at {max_bytes} bytes]"
        from ..security.redaction import untrusted

        return untrusted(text, source=url)


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
        "description": "Read a UTF-8 text file from the workspace (relative path). Long files "
                       "are windowed: pass offset (1-based line) and limit (line count) to read "
                       "further sections.",
        "input_schema": _obj({"path": {"type": "string"},
                              "offset": {"type": "integer",
                                         "description": "1-based line to start from"},
                              "limit": {"type": "integer",
                                        "description": "max lines to return"}}, ["path"]),
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file in the workspace with the given content (relative path).",
        "input_schema": _obj({"path": {"type": "string"}, "content": {"type": "string"}},
                             ["path", "content"]),
    },
    {
        "name": "edit_file",
        "description": "Replace an occurrence of 'old' with 'new' in a workspace file (or append "
                       "'new' if 'old' is empty). Fails if 'old' matches more than once — make it "
                       "unique or set replace_all to change every occurrence.",
        "input_schema": _obj({"path": {"type": "string"}, "old": {"type": "string"},
                              "new": {"type": "string"},
                              "replace_all": {"type": "boolean",
                                              "description": "replace every occurrence of 'old'"}},
                             ["path", "new"]),
    },
    {
        "name": "apply_patch",
        "description": "Apply a unified diff (git apply --3way, falling back to plain apply) to "
                       "the workspace. Use for multi-file edits.",
        "input_schema": _obj({"patch": {"type": "string"}}, ["patch"]),
    },
    {
        "name": "list_dir",
        "description": "List files and directories under a workspace path (default: the root).",
        "input_schema": _obj({"path": {"type": "string"}}, []),
    },
    {
        "name": "grep",
        "description": "Regex-search workspace files (ripgrep-backed when available); returns "
                       "matching path:line: text with optional context lines.",
        "input_schema": _obj({"pattern": {"type": "string",
                                          "description": "regular expression to search for"},
                              "path": {"type": "string"},
                              "context": {"type": "integer",
                                          "description": "lines of context around each match (0-10)"},
                              "include": {"type": "string",
                                          "description": "only search files matching this glob, "
                                                         "e.g. '*.py'"}}, ["pattern"]),
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the workspace and return its exit code + output. "
                       "Runs with a scrubbed env (no secrets), resource limits, and a hard "
                       "timeout — but NOT filesystem or network isolation. Use for builds/tools.",
        "input_schema": _obj({"command": {"type": "string"}}, ["command"]),
    },
    {
        "name": "install_packages",
        "description": "pip-install plain PyPI requirements (name[extras]==version only — no "
                       "flags/URLs/paths) into the workspace's .ada_deps dir; run_command and "
                       "run_tests put it on PYTHONPATH. Uses a scrubbed env but has network access.",
        "input_schema": _obj({"packages": {"type": "array", "items": {"type": "string"},
                                           "description": "requirement specs, e.g. ['requests>=2.31']"}},
                             ["packages"]),
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
                       "Use this to objectively verify code you or a teammate wrote. Optionally "
                       "narrow the run to specific test files and/or a pytest -k expression.",
        "input_schema": _obj({"paths": {"type": "array", "items": {"type": "string"},
                                        "description": "workspace-relative test files to run"},
                              "keyword": {"type": "string",
                                          "description": "pytest -k expression to filter tests"}}, []),
    },
    {
        "name": "symbols",
        "description": "Outline code symbols. Pass 'path' for one file's functions/classes/"
                       "imports (Python via ast, JS/TS via light parsing), or omit it for a "
                       "ranked whole-workspace symbol index. Faster than grep for navigation.",
        "input_schema": _obj({"path": {"type": "string",
                                       "description": "workspace-relative file to outline; "
                                                      "omit for the whole-workspace index"}}, []),
    },
    {
        "name": "find_references",
        "description": "Find definition and usage sites of an identifier across the workspace's "
                       "source files (word-boundary regex search); returns matches grouped "
                       "per file as path then line: text.",
        "input_schema": _obj({"name": {"type": "string",
                                       "description": "identifier to look up, e.g. a function "
                                                      "or class name"}}, ["name"]),
    },
    {
        "name": "delegate",
        "description": "Delegate a self-contained piece of work to another specialist agent by "
                       "name and get its result back. Use when the work turns out much bigger "
                       "than planned. The task description must stand alone — include all "
                       "needed context (max ~4000 chars).",
        "input_schema": _obj({"agent": {"type": "string",
                                        "description": "specialist agent name, e.g. 'coder'"},
                              "task": {"type": "string",
                                       "description": "self-contained task description"}},
                             ["agent", "task"]),
    },
    {
        "name": "web_fetch",
        "description": "Fetch a public http(s) URL and return its text (HTML is tag-stripped; "
                       "output bounded). Disabled unless web access is enabled for the run. "
                       "Private/internal addresses are refused and redirects are not followed.",
        "input_schema": _obj({"url": {"type": "string"},
                              "max_bytes": {"type": "integer",
                                            "description": "response byte cap "
                                                           "(default and max 100000)"}},
                             ["url"]),
    },
]
