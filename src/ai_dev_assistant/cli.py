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
    runp.add_argument("-i", "--interactive", action="store_true",
                      help="Interactive plan mode: propose a plan, refine it in plain English, then run")
    runp.add_argument("--continue", dest="continue_from", metavar="TASK_ID", default=None,
                      help="Re-engage a completed task: continue its workspace + context with this prompt")
    runp.add_argument("-q", "--quiet", action="store_true", help="Hide internal INFO logs")

    resumep = sub.add_parser("resume", help="Resume an interrupted run from its checkpoints")
    resumep.add_argument("task_id", help="The interrupted run's task id")
    resumep.add_argument("-q", "--quiet", action="store_true", help="Hide internal INFO logs")

    servep = sub.add_parser("server", help="Launch the web UI")
    servep.add_argument("--host", default="127.0.0.1")
    servep.add_argument("--port", type=int, default=8000)
    servep.add_argument("--reload", action="store_true",
                        help="Auto-restart the server when source files change (dev)")

    evalp = sub.add_parser("eval", help="Run the golden-task eval harness and score the assistant")
    evalp.add_argument("--only", action="append", default=[], metavar="TASK_ID",
                       help="Run only these golden task ids (repeatable)")
    evalp.add_argument("--json", action="store_true", help="Emit the scorecard as JSON")
    evalp.add_argument("--repeat", type=int, default=None, metavar="N",
                       help="Samples per task (variance handling; default 1 / ADA_EVAL_REPEAT)")
    evalp.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                       help="Wall-clock cap per attempt (default 600 / ADA_EVAL_TASK_TIMEOUT)")
    evalp.add_argument("--replay", nargs="?", const="", default=None, metavar="DIR",
                       help="Run the offline replay eval over committed cassettes (no LLM needed)")
    evalp.add_argument("--record-cassettes", nargs="?", const="", default=None, metavar="DIR",
                       help="Regenerate the offline replay cassettes (deterministic, no LLM needed)")
    evalp.add_argument("--ab", metavar="ADA_KNOB=V1,V2", default=None,
                       help="A/B compare the suite under different values of one ADA_* knob "
                            "(combine with --replay for an offline smoke)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "run":
        return _run(args)
    if args.cmd == "resume":
        return _resume(args)
    if args.cmd == "server":
        return _serve(args)
    if args.cmd == "eval":
        return _eval(args)
    return 1


def _resume(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(message)s", stream=sys.stderr)
    settings = Settings.load()
    engine = Engine(settings)
    row = engine.runs.get(args.task_id)
    if row is None:
        print(f"ERROR: unknown task id {args.task_id}", file=sys.stderr)
        return 2
    if row.get("status") == "completed":
        print(f"Task {args.task_id} already completed — nothing to resume.", file=sys.stderr)
        return 0

    async def go() -> int:
        def on_event(event: Event) -> None:
            line = _fmt(event)
            if line:
                print(line)
        try:
            run, brief, out_dir = await engine.run(
                row.get("prompt") or "", task_id=args.task_id, resume=True, on_event=on_event)
        except LLMError as exc:
            print(f"\nResume failed: {exc}", file=sys.stderr)
            return 1
        finally:
            await engine.aclose()
        passed = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
        print(f"\nResumed run finished: {passed}/{len(run.subtasks)} subtasks passed. Docs: {out_dir}/")
        return 0

    return asyncio.run(go())


def _eval(args: argparse.Namespace) -> int:
    import json as _json

    from .evals.harness import run_eval_sync

    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)

    # A/B knob comparison: run the suite once per value and compare the arms.
    if args.ab:
        from .evals.ab import run_ab
        knob, _, vals = args.ab.partition("=")
        values = [v for v in vals.split(",") if v]
        replay = args.replay is not None
        settings = Settings.load()
        if not replay and settings.requires_api_key and not settings.has_api_key:
            print("ERROR: A/B eval needs a working LLM backend (or add --replay for an "
                  "offline smoke).", file=sys.stderr)
            return 2
        try:
            report = run_ab(settings, knob.strip(), values, only=args.only or None,
                            repeat=args.repeat or 1, task_timeout=args.timeout, replay=replay)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(_json.dumps(report.to_dict(), indent=2) if args.json else report.summary())
        return 0

    # Offline modes: replay committed cassettes / regenerate them — no LLM needed.
    if args.record_cassettes is not None or args.replay is not None:
        from pathlib import Path as _P

        from .evals import replay_eval
        if args.record_cassettes is not None:
            d = _P(args.record_cassettes) if args.record_cassettes else None
            report = replay_eval.record_cassettes(d)
        else:
            d = _P(args.replay) if args.replay else None
            report = replay_eval.run_replay_eval(d, repeat=args.repeat or 1)
        print(_json.dumps([c.to_dict() for c in report.cards], indent=2) if args.json
              else report.summary())
        return 0 if report.passed == len(report.cards) else 1

    settings = Settings.load()
    if settings.requires_api_key and not settings.has_api_key:
        print("ERROR: the eval harness needs a working LLM backend (set a key or use claude_sdk).",
              file=sys.stderr)
        return 2
    report = run_eval_sync(settings, only=args.only or None,
                           repeat=args.repeat, task_timeout=args.timeout)
    if args.json:
        print(_json.dumps([c.to_dict() for c in report.cards], indent=2))
    else:
        print(report.summary())
    return 0 if report.passed == len(report.cards) else 1


def _serve(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.load()
    # Let the app factory see the bind host (S8): a non-loopback bind with no
    # ADA_API_TOKEN auto-generates a token; loopback stays auth-free by default.
    os.environ["ADA_BIND_HOST"] = args.host
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
        uvicorn.run(create_app(settings, host=args.host), host=args.host, port=args.port,
                    log_level="warning")
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


def _print_plan(plan) -> None:
    title = (getattr(plan, "title", "") or "").strip()
    print("\n=== PROPOSED PLAN ===")
    if title:
        print(f"Title: {title}")
    print(f"Approach: {plan.summary}\n")
    for st in plan.subtasks:
        deps = ", ".join(st.depends_on) or "—"
        print(f"  [{st.id}] ({st.agent})  {st.title}")
        print(f"        depends on: {deps}")
        for c in st.acceptance_criteria:
            print(f"        ✓ {c}")
    print()


async def _interactive_plan(engine, prompt: str, continue_from: str | None = None):
    """Propose a plan, let the user refine it in plain English, then approve (or abort)."""
    print("Planning…")
    try:
        plan = await engine.make_plan(prompt, continue_from=continue_from)
    except LLMError as exc:
        print(f"Planning failed: {exc}", file=sys.stderr)
        return None
    while True:
        _print_plan(plan)
        print("Refine in plain English (e.g. 'add a security review step'), or press Enter to RUN, "
              "or 'q' to abort.")
        try:
            instruction = (await asyncio.to_thread(input, "plan> ")).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if instruction.lower() in ("q", "quit", "abort"):
            return None
        if instruction == "" or instruction.lower() in ("run", "go", "approve", "ok", "y", "yes"):
            return plan
        print("Refining…")
        try:
            plan = await engine.refine_plan(prompt, plan, instruction)
        except LLMError as exc:
            print(f"Refine failed: {exc}", file=sys.stderr)


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

        plan = None
        cont = getattr(args, "continue_from", None)
        if cont:
            print(f"Continuing task {cont} — its workspace and outcome will be carried forward.")
        if getattr(args, "interactive", False):
            plan = await _interactive_plan(engine, args.prompt, cont)
            if plan is None:
                print("Aborted — no run started.")
                return 0

        run, brief, out_dir = await engine.run(args.prompt, plan=plan, continue_from=cont, on_event=on_event)
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
