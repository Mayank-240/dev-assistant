"""Command-line entrypoint: submit a task, stream progress, report where docs landed."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import Settings
from .engine import Engine
from .llm.errors import LLMError
from .orchestration.events import Event
from .orchestration.task import RunStatus


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-dev-assistant",
        description="Multi-agent AI dev assistant: orchestrate specialized agents over a task.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    runp = sub.add_parser("run", help="Run a task end-to-end")
    runp.add_argument("prompt", help="The task to perform, in quotes")
    runp.add_argument("--ingest", action="append", default=[], metavar="FILE",
                      help="Ingest a file into the knowledge base first (repeatable)")
    runp.add_argument("-q", "--quiet", action="store_true", help="Hide internal INFO logs")

    servep = sub.add_parser("serve", help="Launch the web UI")
    servep.add_argument("--host", default="127.0.0.1")
    servep.add_argument("--port", type=int, default=8000)
    servep.add_argument("--reload", action="store_true",
                        help="Auto-restart the server when source files change (dev)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "run":
        return _run(args)
    if args.cmd == "serve":
        return _serve(args)
    return 1


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.load()
    print(f"AI Dev Assistant UI → http://{args.host}:{args.port}  (backend: {settings.llm_backend})")
    if args.reload:
        # The reloader re-imports the app in a subprocess, so it needs an import
        # string + factory (not a prebuilt instance). Watch only the package source.
        print("Auto-reload enabled — source changes restart the server.")
        uvicorn.run(
            "ai_dev_assistant.web.server:create_app",
            factory=True, host=args.host, port=args.port, log_level="warning",
            reload=True, reload_dirs=[str(Path(__file__).resolve().parent)],
            reload_excludes=["*/web/static/*"],  # static assets are served live; no restart needed
        )
    else:
        from .web.server import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
    return 0


def _fmt(event: Event) -> str:
    t, d = event.type, event.data
    if t == "subtask_start":
        return f"  ▶ {event.message}"
    if t == "subtask_review":
        mark = "✓" if d.get("passed") else "✗"
        return f"  {mark} {event.message}"
    if t == "message":
        return f"  ✉ {event.message}: {d.get('content', '')}"
    if t in ("brief", "done"):
        return ""  # printed in the final summary instead
    return event.message


def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    settings = Settings.load()
    if settings.requires_api_key and not settings.has_api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set (required for the 'anthropic' backend). "
              "Use ADA_LLM_BACKEND=claude_sdk to run via your Claude Code login instead.", file=sys.stderr)
        return 2
    return asyncio.run(_run_async(args, settings))


async def _run_async(args: argparse.Namespace, settings: Settings) -> int:
    print(f"Backend: {settings.llm_backend}")
    engine = Engine(settings)

    def on_event(event: Event) -> None:
        line = _fmt(event)
        if line:
            print(line)

    try:
        for path in args.ingest:
            p = Path(path)
            if p.is_file():
                n = engine.ingest_doc(p.name, p.read_text(errors="replace"))
                print(f"Ingested {p.name} into KB ({n} chunks).")
            else:
                print(f"WARNING: --ingest file not found: {path}", file=sys.stderr)

        run, brief, out_dir = await engine.run(args.prompt, on_event=on_event)
    except LLMError as exc:
        print(f"\nRun failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.aclose()

    passed = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
    print("\n=== BRIEF ===")
    print(brief.tldr)
    if brief.key_points:
        print("\nKey points:")
        for point in brief.key_points:
            print(f"  - {point}")
    print(f"\n{passed}/{len(run.subtasks)} subtasks passed.")
    print(f"Docs:  {out_dir}/  (plan.md · report.md · brief.md)")
    print(f"Index: {settings.docs_dir / 'INDEX.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
