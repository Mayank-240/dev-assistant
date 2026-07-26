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
    runp.add_argument("-p", "--project", action="append", default=None, metavar="SLUG",
                      help="Run inside this project; repeat for a cross-project fan-out")
    runp.add_argument("--stagger", action="store_true",
                      help="Fan-out only: run the first project alone so its lessons inform the rest")
    runp.add_argument("--ingest", action="append", default=[], metavar="FILE",
                      help="Ingest a file into the knowledge base first (repeatable)")
    runp.add_argument("-i", "--interactive", action="store_true",
                      help="Interactive plan mode: propose a plan, refine it in plain English, then run")
    runp.add_argument("--continue", dest="continue_from", metavar="TASK_ID", default=None,
                      help="Re-engage a completed task: continue its workspace + context with this prompt")
    runp.add_argument("-q", "--quiet", action="store_true", help="Hide internal INFO logs")

    projp = sub.add_parser("project", help="Manage projects (the durable unit of work)")
    projsub = projp.add_subparsers(dest="pcmd", required=True)
    p_new = projsub.add_parser("new", help="Create a greenfield project (own git repo)")
    p_new.add_argument("name")
    p_imp = projsub.add_parser("import", help="Import an existing repo (local path: in place; URL: clone)")
    p_imp.add_argument("source", help="Local repo path or git URL")
    p_imp.add_argument("--name", default=None)
    p_imp.add_argument("--ref", default="", help="Branch/tag/sha for URL imports")
    projsub.add_parser("list", help="List projects")
    p_show = projsub.add_parser("show", help="Show a project's status")
    p_show.add_argument("slug")
    p_arch = projsub.add_parser("archive", help="Archive a project (hide, keep data)")
    p_arch.add_argument("slug")
    p_del = projsub.add_parser("delete", help="Delete a project's data (never a local-origin checkout)")
    p_del.add_argument("slug")
    p_del.add_argument("--yes", action="store_true", help="Skip confirmation")
    p_dist = projsub.add_parser(
        "distill", help="Consolidate the project's knowledge graph "
                        "(merge near-duplicate concepts, prune stale edges)")
    p_dist.add_argument("slug")
    p_dist.add_argument("--dry-run", action="store_true",
                        help="Report proposed actions without applying them")

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
    evalp.add_argument("--record-history", action="store_true",
                       help="Append this run's aggregate scores to <data_dir>/benchmarks.jsonl "
                            "for commit-over-commit tracking (composes with --replay etc.)")

    bkp = sub.add_parser("backup", help="Back up / restore the data dir (runs, memory, knowledge)")
    bksub = bkp.add_subparsers(dest="bcmd", required=True)
    bk_create = bksub.add_parser("create", help="Archive the data dir (live-safe sqlite snapshots)")
    bk_create.add_argument("--out", default=None, metavar="DIR",
                           help="Destination directory (default: <data_dir>/backups/)")
    bksub.add_parser("list", help="List backups in <data_dir>/backups/, newest first")
    bk_rest = bksub.add_parser("restore", help="Restore an archive into the data dir")
    bk_rest.add_argument("archive", help="Path to an ada-backup-*.tar.gz archive")
    bk_rest.add_argument("--force", action="store_true",
                         help="Non-empty data dir: move it aside to "
                              "<data_dir>.pre-restore-<ts> and restore anyway")

    attp = sub.add_parser("attend", help="Answer agents' questions/permission requests "
                                         "from the terminal (polls a running server)")
    attp.add_argument("--url", default="http://127.0.0.1:8000",
                      help="Base URL of the running server (default: %(default)s)")
    attp.add_argument("--token", default=None,
                      help="API token (default: the ADA_API_TOKEN env var)")
    attp.add_argument("--interval", type=float, default=5.0, metavar="SECONDS",
                      help="Poll interval (default: %(default)s)")
    attp.add_argument("--once", action="store_true",
                      help="Process currently-open items once and exit (scripting)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "run":
        return _run(args)
    if args.cmd == "project":
        return _project(args)
    if args.cmd == "resume":
        return _resume(args)
    if args.cmd == "server":
        return _serve(args)
    if args.cmd == "eval":
        return _eval(args)
    if args.cmd == "backup":
        return _backup(args)
    if args.cmd == "attend":
        return _attend(args)
    return 1


def _run_multi(args: argparse.Namespace, settings: Settings, slugs: list[str]) -> int:
    """Cross-project fan-out (F3): one child task per project, one rollup."""
    from .orchestration.fanout import run_cross_project

    if settings.requires_api_key and not settings.has_api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set (required for the 'anthropic' backend).",
              file=sys.stderr)
        return 2
    print(f"Cross-project run across: {', '.join(slugs)}"
          + (" (staggered)" if args.stagger else ""))

    def on_event(event: Event) -> None:
        if event.type in ("child_start", "child_done", "status", "brief"):
            print(f"  {event.message}")

    result = asyncio.run(run_cross_project(
        settings, args.prompt, slugs, stagger=args.stagger, on_event=on_event))
    print(f"\n=== ROLLUP ({result['status']}) ===")
    for c in result["children"]:
        line = f"  {c['slug']:<24} {c['status']:<10}"
        if c.get("branch"):
            line += f" branch {c['branch']}"
        if c.get("error"):
            line += f"  ({c['error'][:60]})"
        print(line)
    print(f"Docs: {result['docs_dir']}/rollup.md")
    return 0 if result["status"] == "completed" else 1


def _project(args: argparse.Namespace) -> int:
    from . import projects

    settings = Settings.load()
    try:
        if args.pcmd == "new":
            p = projects.create_project(settings, args.name)
            print(f"Created project '{p['slug']}' ({p.get('origin', 'greenfield')}) "
                  f"at {p.get('root') or '(no checkout)'}")
        elif args.pcmd == "import":
            p = projects.import_project(settings, args.source, name=args.name, ref=args.ref)
            mode = "in place" if p.get("origin") == "local" else "cloned"
            print(f"Imported '{p['slug']}' ({mode}) — root: {p.get('root')}, "
                  f"branch: {p.get('default_branch', '?')}")
        elif args.pcmd == "list":
            for p in projects.list_projects(settings):
                flag = " [archived]" if p.get("archived") else ""
                root = p.get("root") or "(scratch)"
                print(f"{p['slug']:<24} {p.get('origin', '-'):<11} {root}{flag}")
        elif args.pcmd == "show":
            st = projects.project_status(settings, args.slug)
            for k in ("slug", "origin", "root", "branch", "head", "dirty",
                      "last_indexed_commit", "archived"):
                print(f"{k:>20}: {st.get(k, '')}")
        elif args.pcmd == "archive":
            projects.archive_project(settings, args.slug)
            print(f"Archived '{args.slug}'.")
        elif args.pcmd == "delete":
            if not args.yes:
                confirm = input(f"Delete project '{args.slug}' data? [y/N] ").strip().lower()
                if confirm not in ("y", "yes"):
                    print("Aborted.")
                    return 0
            projects.delete_project(settings, args.slug)
            print(f"Deleted '{args.slug}' (local-origin checkouts are never removed).")
        elif args.pcmd == "distill":
            return _distill(settings, args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


def _distill(settings: Settings, args: argparse.Namespace) -> int:
    """`project distill <slug> [--dry-run]`: consolidate the knowledge graph."""
    import dataclasses

    from . import projects
    from .knowledge.distill import distill, distill_report
    from .knowledge.graph import NetworkXKnowledgeGraph

    if projects.get_project(settings, args.slug) is None:
        raise ValueError(f"unknown project: {args.slug}")
    s = dataclasses.replace(settings, project=args.slug)
    kg = NetworkXKnowledgeGraph(s.graph_path)
    report = distill_report(kg)
    before = report["stats_before"]
    mode = "dry run" if args.dry_run else "apply"
    print(f"Knowledge-graph distill for '{args.slug}' ({mode}) — "
          f"{before['nodes']} nodes, {before['edges']} edges")
    if report.get("reason"):
        print(f"Nothing to do: {report['reason']}")
        return 0
    print(f"  merges:  {len(report['merges'])}")
    for m in report["merges"]:
        sim = f", cos {m['similarity']}" if "similarity" in m else ""
        print(f"    keep {m['keep']}  <-  drop {m['drop']}  ({m['reason']}{sim})")
    print(f"  prunes:  {len(report['prunes'])} stale edge(s)")
    for p in report["prunes"]:
        print(f"    {p['src']} -{p['relation']}-> {p['dst']}  "
              f"(weight {p['weight']}, {p['age_days']}d old)")
    print(f"  orphans: {len(report['orphans'])}")
    for n in report["orphans"]:
        print(f"    {n}")
    if args.dry_run:
        print("Dry run: nothing was changed.")
        return 0
    result = distill(kg, report=report)
    after = result["stats_after"]
    print(f"Applied: merged {result['merged']}, pruned {result['pruned']}, "
          f"removed {result['orphans_removed']} orphan(s) — "
          f"now {after['nodes']} nodes, {after['edges']} edges.")
    return 0


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

    def _record_history(suite: str, report_obj, settings_obj: Settings | None = None) -> None:
        """--record-history: append the aggregate scores to <data_dir>/benchmarks.jsonl."""
        from .evals.history import history_path, record_result
        s = settings_obj if settings_obj is not None else Settings.load()
        record_result(s, suite, report_obj)
        print(f"Recorded benchmark entry ({suite}) -> {history_path(s)}", file=sys.stderr)

    # A/B knob comparison: run the suite once per value and compare the arms.
    if args.ab:
        if args.record_history:
            print("NOTE: --record-history is ignored with --ab (per-arm reports are not "
                  "single-suite entries).", file=sys.stderr)
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
            suite = "record-cassettes"
        else:
            d = _P(args.replay) if args.replay else None
            report = replay_eval.run_replay_eval(d, repeat=args.repeat or 1)
            suite = "replay"
        print(_json.dumps([c.to_dict() for c in report.cards], indent=2) if args.json
              else report.summary())
        if args.record_history:
            _record_history(suite, report)
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
    if args.record_history:
        _record_history("golden", report, settings)
    return 0 if report.passed == len(report.cards) else 1


def _backup(args: argparse.Namespace) -> int:
    from . import backup as bkp

    settings = Settings.load()
    try:
        if args.bcmd == "create":
            archive = bkp.create_backup(settings, out_dir=args.out)
            size_mb = archive.stat().st_size / 1e6
            print(f"Backup written: {archive} ({size_mb:.1f} MB)")
            print("Contains the data dir only (runs, memory, knowledge, settings, users, "
                  "push keys — treat as sensitive).")
            print("Workspace checkouts and generated docs are NOT included "
                  "(re-creatable from git).")
        elif args.bcmd == "list":
            rows = bkp.list_backups(settings)
            if not rows:
                print(f"No backups in {settings.data_dir / 'backups'}.")
                return 0
            for b in rows:
                print(f"{b['created']}  {b['size']:>12,} B  {b['path']}")
        elif args.bcmd == "restore":
            print("WARNING: stop the server before restoring, and restart it afterwards —")
            print("a live server writing to the data dir during restore can corrupt both.")
            result = bkp.restore_backup(settings, args.archive, force=args.force)
            if result["moved_aside"]:
                print(f"Previous data dir moved aside to: {result['moved_aside']}")
            print(f"Restored {result['members']} file(s) from {result['archive']} "
                  f"into {result['restored_to']}.")
            print("Restart the server to pick up the restored state.")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


def _attend(args: argparse.Namespace) -> int:
    import os

    from .attend import run_attend

    token = args.token if args.token is not None else os.getenv("ADA_API_TOKEN", "")
    try:
        return run_attend(args.url, token, once=args.once, poll_seconds=args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


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
    plist = getattr(args, "project", None) or []
    if len(plist) > 1:
        return _run_multi(args, settings, plist)
    if plist:
        import dataclasses

        from . import projects
        settings = dataclasses.replace(settings, project=projects.resolve(settings, plist[0]))
        print(f"Project: {settings.project}")
    try:  # apply the active project's policy (F7): task override → policy → defaults
        from . import projects
        settings = projects.effective_settings(settings)
    except Exception:
        pass
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
