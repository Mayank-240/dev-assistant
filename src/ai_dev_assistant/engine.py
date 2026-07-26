"""The engine wires every subsystem together and runs a task end to end.

Flow: (optional) materialize a real repo into the workspace -> orchestrator makes a plan
(grounded in a repo map + prior lessons + agent track record) -> scheduler runs subtasks
in parallel through the session pool with degrade-on-partial reliability -> each result is
verified by an objective-gated reviewer -> the run is summarized, reflected on (lessons),
indexed into the KB/KG, documented, and (optionally) delivered as a git branch.

Progress is reported as structured ``Event`` objects so both the CLI and the web UI can
consume the same stream.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from . import vcs
from .agents.orchestrator import Orchestrator
from .agents.reflector import Reflector
from .agents.registry import build_agents
from .agents.reviewer import Reviewer
from .config import Settings
from .context import assemble as assemble_context
from .context import budget_for
from .docs.writer import write_task_docs
from .knowledge.base import KnowledgeBase
from .knowledge.code_index import CodeIndex
from .knowledge.extract import enrich_kg_from_workspace
from .knowledge.graph import NetworkXKnowledgeGraph
from .knowledge.repo_map import build_repo_map, onboard
from .llm.factory import get_provider
from .llm.schemas import BriefDoc, Plan, SubTask, Verdict
from .memory.store import MemoryStore, ScopedMemory
from .memory.store import _open_project_store as _open_sibling_store  # never-create wrapper
from .orchestration.events import Event, status
from .projects import slugify
from .orchestration.message_bus import MessageBus
from .orchestration.run_control import RunControl
from .orchestration.run_store import RunStore
from .orchestration.scheduler import BudgetExceeded, Scheduler
from .orchestration.session_pool import Session, SessionPool
from .orchestration.task import (
    SATISFYING, DAGError, RunStatus, SubTaskState, TaskRun, new_task_id,
)
from .orchestration.trace import Tracer
from .security.redaction import AuditLog, redact, untrusted
from .tools.registry import ToolBox, ToolContext
from .verification import apply_objective_gate, capture_baseline, gather_signals
from .workspaces import get_workspace, workspace_of

logger = logging.getLogger("ada.engine")

_SUMMARY_SYSTEM = (
    "You summarize a completed multi-agent task run for a human who wants the story in ten "
    "seconds. Be concrete about what was actually produced and the outcome. The TL;DR is two "
    "or three sentences; key_points are the few things that matter."
)

EventFn = Callable[[Event], None]
_MAX_REPAIRS = 2  # adaptive-replan safety cap per run
# Engine-internal dirs inside the run workspace that must never leak into context,
# diffs, or the reviewer's ground truth (sibling worktrees, vendored deps).
_INTERNAL_DIRS = {".git", "__pycache__", ".ada_worktrees", ".ada_deps"}
# Workspace-aware context (sibling recall): caps that keep the borrowed section small
# and lower-priority than the project's own recall/KB parts.
_SIBLING_PROJECTS_MAX = 3      # query at most this many sibling projects
_SIBLING_ITEM_CHARS = 300      # per-item excerpt cap (matches the KB-hit excerpt size)
_SIBLING_SECTION_CHARS = 1500  # total budget for the whole sibling section

# Wall-clock cap on the run-start code-index pass; past it the run proceeds while the
# indexing thread finishes in the background (retrieval sees a partial index until then).
_CODE_INDEX_MAX_SECONDS = 60.0

# Upstream result digests: dependent subtasks inherit each completed upstream's result
# TEXT (not just its files via the worktree merge) as one bounded context part.
_UPSTREAM_ITEM_CHARS = 500     # per-upstream digest cap
_UPSTREAM_SECTION_CHARS = 1500  # whole "Upstream results" part cap


def upstream_results_part(run: TaskRun, state: SubTaskState) -> tuple[str, str] | None:
    """One "Upstream results" context part for a subtask with dependencies.

    Per upstream, one entry prefixed ``[<upstream id> <title>]``: a digest of its
    result text (~500 chars each, ~1500 total) for completed upstreams — with a
    caveat marker for soft-passes — or a status line (no text inherited) for
    failed/blocked ones. Returns None for dependency-free subtasks, so plans without
    depends_on assemble prompts byte-identical to the pre-feature behavior.
    """
    deps = state.spec.depends_on
    if not deps:
        return None
    lines: list[str] = []
    used = 0
    for dep_id in deps:
        dep = run.subtasks.get(dep_id)
        if dep is None:
            continue
        prefix = f"[{dep_id} {dep.spec.title}]"
        if dep.status in SATISFYING and (dep.result or "").strip():
            digest = dep.result.strip()
            if len(digest) > _UPSTREAM_ITEM_CHARS:
                digest = digest[:_UPSTREAM_ITEM_CHARS].rstrip() + "…"
            caveat = (" (passed with caveats — verify before relying on it)"
                      if dep.status is RunStatus.PASSED_WITH_CAVEATS else "")
            entry = f"{prefix}{caveat} {digest}"
        else:
            entry = f"{prefix} status: {dep.status.value} — no result inherited"
        remaining = _UPSTREAM_SECTION_CHARS - used
        if remaining <= len(prefix):  # not even the prefix fits — stop cleanly
            break
        if len(entry) > remaining:
            entry = entry[:remaining - 1].rstrip() + "…"
        lines.append(entry)
        used += len(entry) + 1
    if not lines:
        return None
    return ("Upstream results", "\n".join(lines))


@dataclasses.dataclass(frozen=True)
class RunContext:
    """Per-run identifiers and channels threaded through the phases (A1)."""
    rid: str
    run_ws: Path
    emit: EventFn


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.provider = get_provider(settings)
        # Memory: a per-project store + the shared global store. Recall reads both;
        # writes go to settings.memory_scope. The KG is project-scoped only.
        self.project_memory = MemoryStore(settings)  # settings.db_path = active project
        self.global_memory = MemoryStore(settings, db_path=settings.global_db_path)
        self.memory = ScopedMemory(self.project_memory, self.global_memory,
                                   write_scope=settings.memory_scope)
        self.kb = KnowledgeBase(self.project_memory.vectors)
        self.kg = NetworkXKnowledgeGraph(settings.graph_path)
        self.bus = MessageBus()
        self.agents = build_agents(settings)
        for name in self.agents:
            self.bus.register(name)
        self.bus.register("orchestrator")
        self.bus.register("reviewer")
        self.orchestrator = Orchestrator(settings, self.provider)
        self.reviewer = Reviewer(settings, self.provider)
        self.reflector = Reflector(settings, self.provider)
        self.runs = RunStore(settings.data_dir / "runs.db")
        self.control: RunControl | None = None  # set by the web layer for pause/steer
        self.last_pool_stats: dict[str, int] = {}
        self._mem_writes = 0
        self._last_msg_idx = 0
        self._msg_lock = asyncio.Lock()
        self._activity: dict[str, list[dict[str, Any]]] = {}  # subtask id -> agent steps
        self._cost_by_subtask: dict[str, dict[str, Any]] = {}
        self._changed_by_subtask: dict[str, list[str]] = {}  # subtask id -> files it touched
        self._merge_by_subtask: dict[str, str] = {}  # subtask id -> its merge commit sha
        self._baseline: dict[str, Any] = {}  # pre-run test/lint snapshot (V3 delta gating)
        # Semantic code retrieval: built at run start ONLY for runs on a real repo
        # workspace with the setting on; None means no retrieval part is ever added,
        # which is what keeps greenfield/eval prompts byte-identical (cassette rule).
        self._code_index: CodeIndex | None = None
        self._repairs = 0
        self._tracer: Tracer | None = None
        self._audit: AuditLog | None = None
        # Secrets are scrubbed at every durable boundary (memory/KB/docs/events), not just
        # tool output (S7).
        self._scrub = redact if settings.redact_secrets else (lambda t: t)

    def ingest_doc(self, doc_id: str, text: str) -> int:
        return self.kb.ingest(doc_id, text)

    async def make_plan(self, prompt: str, *, continue_from: str | None = None) -> Plan:
        """Just the orchestrator step — used by the plan-approval flow."""
        repo_context = ""
        if continue_from:
            parent_ws = self.settings.run_workspace(continue_from)
            rmap = await asyncio.to_thread(build_repo_map, parent_ws)
            repo_context = self._continuation_summary(continue_from) + (rmap or "")
        stats, recent = self._routing_signals()
        return await self.orchestrator.make_plan(
            prompt, self.agents, prior_knowledge=self._recall_prior(prompt),
            repo_context=repo_context, track_record=self._track_record_text(),
            agent_stats=stats, project_recent=recent,
        )

    async def refine_plan(self, prompt: str, plan: Plan, instruction: str) -> Plan:
        """Revise a proposed plan from a natural-language instruction (interactive plan mode)."""
        return await self.orchestrator.refine_plan(prompt, plan, instruction, self.agents)

    def _continuation_summary(self, parent_id: str) -> str:
        """Framing that tells the orchestrator this run continues a prior task."""
        parent = self.runs.get(parent_id) or {}
        return (
            "This task CONTINUES a previously-completed task — build on the EXISTING files in "
            "the workspace, do not redo work that's already done.\n"
            f"Original task: {parent.get('prompt', '')}\n"
            f"Previous outcome: {parent.get('summary', '')}\n\n"
        )

    # ---- phase: workspace setup (A1) — repo materialization / continuation / baseline ----
    async def _setup_workspace(self, ctx: RunContext, *, continue_from: str | None,
                               resume: bool) -> str:
        run_ws, rid, emit = ctx.run_ws, ctx.rid, ctx.emit
        repo_context = ""
        checkout = self._project_checkout() if not self.settings.repo_backed else None
        if checkout is not None:
            # Project task (F2): worktree off the durable checkout. Idempotent add covers
            # resume; a continuation bases its branch on the parent task's branch.
            emit(status(f"Preparing worktree from project '{self.settings.project}'…"))
            try:
                try:
                    from . import projects as _projects
                    ref = await asyncio.to_thread(
                        _projects.refresh_index, self.settings, self.settings.project,
                        self.kb, self.kg)
                    if ref.get("files"):
                        emit(status(f"Project index refreshed: {ref['files']} file(s) "
                                    f"({ref.get('mode', '?')})."))
                except Exception as exc:  # indexing is best-effort
                    logger.info("project index refresh skipped: %s", exc)
                base = f"ada/{continue_from}" if continue_from else ""
                info = await asyncio.to_thread(
                    vcs.task_worktree_add, checkout, run_ws, rid, base=base)
                emit(status(f"Worktree ready on {info['branch']}."))
                repo_context = await asyncio.to_thread(build_repo_map, run_ws)
                if continue_from:
                    repo_context = self._continuation_summary(continue_from) + repo_context
            except Exception as exc:
                run_ws.mkdir(parents=True, exist_ok=True)
                emit(status(f"Project worktree unavailable ({exc}); continuing greenfield."))
        elif resume:
            pass  # the workspace already holds the interrupted run's state — don't re-materialize
        elif self.settings.repo_backed:
            emit(status("Materializing repository into the workspace…"))
            try:
                info = await asyncio.to_thread(
                    vcs.materialize, dest=run_ws, repo_url=self.settings.repo_url,
                    repo_path=self.settings.repo_path, repo_ref=self.settings.repo_ref)
                emit(status(f"Repository ready ({info['mode']} @ {info.get('head', '')[:8]})."))
                # Heavy indexing runs off the event loop (W3) so the server stays live.
                repo_context = await asyncio.to_thread(build_repo_map, run_ws)
                onb = await asyncio.to_thread(onboard, self.kb, self.kg, run_ws, rid or "run")
                if onb["files"]:
                    emit(status(f"Onboarded {onb['files']} repo file(s) into the KB + graph."))
            except Exception as exc:  # never let repo setup abort the whole run
                emit(status(f"Repository setup failed ({exc}); continuing greenfield."))
        elif continue_from:
            # Re-engagement: carry the prior task's workspace + outcome forward.
            repo_context = await self._prepare_continuation(continue_from, run_ws, rid, emit)

        # ---- V3: pre-run baseline so verification gates on the DELTA, not absolute state ----
        if self.settings.objective_review or self.settings.verify_run_tests:
            try:
                self._baseline = await asyncio.to_thread(
                    capture_baseline, run_ws, run_tests=self.settings.verify_run_tests,
                    lint=self.settings.lint_check, timeout=self.settings.verify_timeout,
                    static=(self.settings.objective_static
                            and self.settings.objective_review))
                base_t = self._baseline.get("tests")
                if base_t is not None:
                    emit(status(f"Baseline: workspace tests {'pass' if base_t.passed else 'FAIL'} "
                                "before any changes — verification gates on the delta."))
            except Exception as exc:
                emit(status(f"Baseline capture skipped: {exc}"))
        return repo_context

    def _project_checkout(self) -> Path | None:
        """The active project's durable repo checkout, if it has one (PLAN F1)."""
        try:
            from . import projects as _projects
            return _projects.project_checkout(self.settings, self.settings.project)
        except Exception:
            return None

    async def _start_code_index(self, run_ws: Path, emit: EventFn) -> None:
        """Build/refresh the per-project semantic code index at run start (best-effort).

        Indexing runs in a worker thread, time-capped: past the cap the run proceeds
        while the thread keeps filling the store in the background — later subtasks
        simply retrieve over whatever is indexed by then. Any failure is logged and
        the run continues without code retrieval (``self._code_index`` still set, so
        an incrementally-built index from a prior run keeps serving).
        """
        try:
            index = CodeIndex(self.settings)
        except Exception as exc:  # noqa: BLE001 — retrieval is a convenience, never fatal
            logger.warning("code index unavailable: %s", exc)
            return
        self._code_index = index
        try:
            stats = await asyncio.wait_for(
                asyncio.to_thread(index.index_workspace, run_ws),
                timeout=_CODE_INDEX_MAX_SECONDS)
            if stats.get("indexed"):
                emit(status(f"Code index: {stats['indexed']} file(s) (re)indexed."))
        except asyncio.TimeoutError:
            emit(status("Code index still building — retrieval will use what's ready."))
        except Exception as exc:  # noqa: BLE001
            logger.warning("code indexing failed: %s", exc)

    async def _prepare_continuation(self, parent_id: str, run_ws: Path, rid: str, emit: EventFn) -> str:
        """Carry the parent task's workspace + context forward into this run."""
        parent_ws = self.settings.run_workspace(parent_id)
        if parent_ws.is_dir():
            emit(status(f"Re-engaging task {parent_id}: carrying its workspace forward…"))
            try:
                await asyncio.to_thread(
                    shutil.copytree, parent_ws, run_ws, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache"))
            except Exception as exc:  # never let a copy hiccup abort the run
                emit(status(f"Could not copy prior workspace ({exc}); continuing fresh."))
            try:
                await asyncio.to_thread(onboard, self.kb, self.kg, run_ws, rid)
            except Exception:
                pass
        rmap = await asyncio.to_thread(build_repo_map, run_ws)
        return self._continuation_summary(parent_id) + (rmap or "")

    async def run(
        self,
        prompt: str,
        *,
        plan: Plan | None = None,
        task_id: str | None = None,
        title: str | None = None,
        continue_from: str | None = None,
        resume: bool = False,
        on_event: EventFn | None = None,
    ) -> tuple[TaskRun, BriefDoc, Path]:
        base_emit: EventFn = on_event or (lambda _e: None)
        self._activity = {}
        self._cost_by_subtask = {}
        self._changed_by_subtask = {}
        self._merge_by_subtask = {}
        self._baseline = {}
        self._repairs = 0
        if self._code_index is not None:  # an earlier run on this engine built one
            self._code_index.close()
            self._code_index = None

        rid = task_id or new_task_id()  # fix the id now so workspace/trace paths are per-run
        # Events stream to the durable log as they happen (R2) — a crashed or cancelled run
        # keeps everything up to the failure, which is exactly the run you need to debug.
        events_path = self.settings.docs_dir / rid / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        seq = 0

        def emit(e: Event) -> None:
            nonlocal seq
            try:
                with events_path.open("a") as fh:
                    fh.write(json.dumps({"seq": seq, **e.to_dict()}, ensure_ascii=False,
                                        default=str) + "\n")
            except Exception:
                pass
            seq += 1
            base_emit(e)

        checkout = self._project_checkout() if not self.settings.repo_backed else None
        if checkout is not None:
            # Project task (F2): the workspace is a git worktree OUTSIDE the checkout,
            # on branch ada/<rid>. It persists after the run — review, files, continue,
            # and resume all read it; it's removed when the task is deleted.
            # Resolved absolute: git resolves relative worktree paths against -C, which
            # silently nests them (observed in production with ADA_WORKSPACE_DIR=workspace).
            run_ws = (self.settings.workspace_dir / self.settings.project
                      / "worktrees" / rid).resolve()
        else:
            run_ws = self.settings.run_workspace(rid).resolve()
            run_ws.mkdir(parents=True, exist_ok=True)  # agents (and repo materialize) write here
        self.last_run_workspace = run_ws
        self._tracer = Tracer(self.settings.docs_dir / rid / "trace.jsonl", enabled=self.settings.trace)
        self._audit = AuditLog(self.settings.docs_dir / rid / "audit.jsonl", enabled=self.settings.audit_log)
        # Bind the command sandbox to this run's settings (S5): every
        # run_command(_sync) below — tools, verification, workspace tests —
        # picks up the tier/image/network policy without threading it through.
        from .execution import configure_sandbox
        configure_sandbox(self.settings.sandbox, self.settings.sandbox_image,
                          self.settings.sandbox_allow_network)

        # ---- R1: resuming an interrupted run reuses its workspace + persisted plan ----
        if resume and task_id and plan is None:
            stored = self.runs.get_plan(task_id)
            if not stored:
                # Silently re-planning a "resume" corrupts the run (new subtask ids get
                # matched against old checkpoints). Fail loudly instead.
                emit(Event("error", "Cannot resume: no checkpointed plan for this task.",
                           {"message": "no stored plan — the run cannot be resumed"}))
                raise RuntimeError(f"cannot resume {task_id}: no checkpointed plan")
            plan = Plan.model_validate_json(stored)
            emit(status(f"Resuming task {task_id} from its checkpointed plan."))

        ctx = RunContext(rid=rid, run_ws=run_ws, emit=emit)
        repo_context = await self._setup_workspace(ctx, continue_from=continue_from, resume=resume)

        # Semantic code retrieval (strictly conditional, cassette-safe): only a run on
        # a real repo workspace — a project checkout or a repo-backed materialization,
        # the same runs that get run-level repo-map context — builds the index.
        # Greenfield/eval runs never construct it, so their prompts stay byte-identical
        # whether the setting is on or off.
        if self.settings.code_retrieval and (checkout is not None or self.settings.repo_backed):
            await self._start_code_index(run_ws, emit)

        if plan is None:
            prior = self._recall_prior(prompt)
            if prior:
                emit(status(f"Recalled {len(prior.splitlines())} relevant lesson(s) from past runs."))
            emit(status("Orchestrator: decomposing the task…"))
            with self._tracer.span("phase", "plan"):
                stats, recent = self._routing_signals()
                plan = await self.orchestrator.make_plan(
                    prompt, self.agents, prior_knowledge=prior, repo_context=repo_context,
                    track_record=self._track_record_text(),
                    agent_stats=stats, project_recent=recent)
        else:
            plan = Orchestrator._sanitize(plan, self.agents)  # validate an edited/approved plan

        run = TaskRun.from_plan(prompt, plan, rid)
        try:
            if len(run.subtasks) > self.settings.max_plan_subtasks:
                raise DAGError(f"plan too large: {len(run.subtasks)} subtasks "
                               f"(cap {self.settings.max_plan_subtasks})")
            run.validate()  # structural DAG check (cycles / dup ids / dangling deps)
        except Exception as exc:
            emit(Event("error", f"Invalid plan: {exc}", {"message": str(exc)}))
            self.runs.set_status(run.id, "failed")
            raise

        final_title = (title or "").strip() or (getattr(plan, "title", "") or "").strip() or None
        self.runs.start(run.id, prompt, title=final_title, project=self.settings.project)
        try:
            self.runs.save_plan(run.id, plan.model_dump_json())  # resumable (R1)
        except Exception:
            pass
        if resume:
            restored = self._restore_checkpoints(run)
            if restored:
                emit(status(f"Resuming: {restored} completed subtask(s) restored from checkpoint; "
                            "the rest will run."))
        if continue_from:
            self.runs.set_parent(run.id, continue_from)
        review_tgt = ""
        if checkout is not None:
            try:
                from . import projects as _projects
                review_tgt = _projects.review_target(self.settings, self.settings.project)
            except Exception:
                pass
            try:
                self.runs.set_run_branch(run.id, f"ada/{rid}", review_tgt)
            except Exception:
                pass
        self._seed_kg(run)
        # Effective permissions snapshot (F4): what this task may spend, touch, and use.
        emit(Event("policy", "Effective policy resolved.", {
            "policy": {
                "budget_usd": self.settings.budget_usd,
                "effort": self.settings.agent_effort,
                "git_mode": self.settings.git_mode,
                "protected_paths": list(self.settings.protected_paths),
                "sandbox": self.settings.sandbox,
                "allow_web": self.settings.allow_web,
                "allow_run_command": self.settings.allow_run_command,
                "max_plan_subtasks": self.settings.max_plan_subtasks,
                "worktree_per_subtask": self.settings.worktree_per_subtask,
            },
            "tools_by_agent": {n: list(a.profile.tools) for n, a in self.agents.items()},
            "review_target": review_tgt,
            "task_branch": f"ada/{rid}" if checkout is not None else "",
        }))
        emit(Event("plan", f"Plan ready: {len(run.subtasks)} subtask(s).", {
            "summary": plan.summary,
            "subtasks": [
                {
                    "id": st.id, "title": st.title, "agent": st.agent,
                    "depends_on": st.depends_on, "rationale": st.rationale,
                    "acceptance_criteria": st.acceptance_criteria,
                }
                for st in plan.subtasks
            ],
        }))

        if resume:
            # Replay restored subtasks into the event stream so the live view shows
            # them completed with their verdicts — not stranded as "queued".
            for st in run.subtasks.values():
                if st.status in (RunStatus.PASSED, RunStatus.PASSED_WITH_CAVEATS):
                    v = st.verdict
                    emit(Event("subtask_start", f"{st.id} [{st.agent}] {st.spec.title}", {
                        "id": st.id, "agent": st.agent, "title": st.spec.title,
                        "restored": True}))
                    emit(Event("subtask_review",
                               f"{st.id} passed (score {v.score if v else '—'}) — restored",
                               {"id": st.id, "passed": True,
                                "score": v.score if v else 100,
                                "attempts": st.attempts,
                                "reasons": list(v.reasons if v else [])
                                + ["Restored from a previous session's checkpoint."],
                                "objective_note": "restored from checkpoint",
                                "result": self._scrub(st.result or "")[:12000],
                                "restored": True}))

        pool = SessionPool(
            max_concurrent=self.settings.max_concurrent_sessions,
            idle_ttl=self.settings.session_idle_ttl,
            reaper_interval=self.settings.reaper_interval,
            agent_provider=lambda name: self.agents[name],
        )
        pool.start()
        scheduler = Scheduler(
            pool=pool,
            execute=self._make_execute(run, emit, run_ws),
            verify=self._make_verify(run, emit, run_ws),
            max_retries=self.settings.max_retries,
            degrade_on_partial=self.settings.degrade_on_partial,
            transient_retries=self.settings.llm_max_retries,
            replan=self._make_replan(run, emit) if self.settings.adaptive_replan else None,
            gate=self.control.gate if self.control else None,
            on_retry=self._make_on_retry(emit),
        )
        over_budget = False
        finalized = False
        try:
            try:
                await scheduler.run(run)
            except BudgetExceeded:
                over_budget = True
            finally:
                await pool.stop()
            self.last_pool_stats = {"created": pool.created_total, "reaped": pool.reaped_total}
            emit(Event("sessions", f"Sessions: {pool.created_total} spawned, {pool.reaped_total} reaped.",
                       {"created": pool.created_total, "reaped": pool.reaped_total}))

            brief, out_dir = await self._finalize_run(
                run, ctx, over_budget=over_budget, final_title=final_title,
                prompt=prompt, pool=pool)
            finalized = True
            return run, brief, out_dir
        except asyncio.CancelledError:
            # Deep cancellation (R5): kill any child process groups this run spawned.
            try:
                from .execution import terminate_tag
                killed = terminate_tag(run.id)
                if killed:
                    logger.info("cancelled run %s: killed %d child process group(s)", run.id, killed)
            except Exception:
                pass
            self.runs.set_status(run.id, "cancelled")
            raise
        finally:
            self._checkpoint_all(run)  # persist final subtask states for --resume (R1)
            if not finalized:
                # Any non-terminal run after an unexpected failure is marked failed, not left
                # stranded 'running'.
                cur = self.runs.get(run.id) or {}
                if cur.get("status") in (None, "running"):
                    self.runs.set_status(run.id, "failed")

    # ---- phase: finalize (A1) — objective tests, delivery, summary, learning, docs ----
    async def _finalize_run(self, run: TaskRun, ctx: RunContext, *,
                            over_budget: bool, final_title: str | None, prompt: str,
                            pool: SessionPool) -> tuple[BriefDoc, Path]:
        run_ws, emit = ctx.run_ws, ctx.emit
        # Objective verification: run the generated tests in the run's workspace.
        if self.settings.verify_run_tests:
            from .execution import run_workspace_tests
            emit(status("Running generated tests in the workspace…"))
            try:
                try:
                    run.execution = await run_workspace_tests(
                        run_ws, self.settings.verify_timeout, tag=run.id)
                except TypeError:  # execution.py without tag support
                    run.execution = await run_workspace_tests(run_ws, self.settings.verify_timeout)
            except Exception as exc:  # never let test execution abort the run
                run.execution = None
                emit(status(f"Test execution skipped: {exc}"))
            if run.execution is not None:
                base_t = self._baseline.get("tests")
                pre_existing = (not run.execution.passed and base_t is not None
                                and not base_t.passed)
                self.kg.add_fact(run.id, "tests", "passed" if run.execution.passed else "failed",
                                 layer="run", run_id=run.id)
                emit(Event(
                    "execution",
                    f"Tests {'passed' if run.execution.passed else 'failed'}: {run.execution.command}"
                    + (" (failures pre-date this run)" if pre_existing else ""),
                    {"ran": True, "passed": run.execution.passed, "command": run.execution.command,
                     "return_code": run.execution.return_code,
                     "duration": round(run.execution.duration, 1), "timed_out": run.execution.timed_out,
                     "pre_existing_failures": pre_existing},
                ))
            else:
                emit(Event("execution", "No runnable tests detected in the workspace.", {"ran": False}))

        # Enrich the knowledge graph with real code entities from the generated files.
        n_files = await asyncio.to_thread(enrich_kg_from_workspace, self.kg, run_ws, run.id)
        if n_files:
            emit(status(f"Indexed {n_files} generated file(s) into the knowledge graph."))

        # ---- Delivery: project tasks live on their ada/<id> branch; policy decides ----
        checkout = self._project_checkout()
        if checkout is not None:
            branch = f"ada/{run.id}"
            try:
                from . import projects as _projects
                target = _projects.review_target(self.settings, self.settings.project)
            except Exception:
                target = ""
            if (self.settings.git_mode == "merge" and target
                    and run.rollup_status() == "completed"):
                try:
                    mres = await asyncio.to_thread(
                        vcs.merge_branch, checkout, branch, target,
                        message=f"ada: accept task {run.id}")
                except Exception as exc:
                    mres = {"merged": False, "error": str(exc)}
                if mres.get("merged"):
                    emit(Event("git", f"Auto-merged {branch} into {target} "
                               f"({mres.get('commit', '')[:8]}).",
                               {"branch": branch, "merged_into": target,
                                "commit": mres.get("commit", "")}))
                else:
                    emit(status(f"Auto-merge into {target} skipped "
                                f"({mres.get('error') or 'conflict'}) — branch kept for review."))
            else:
                emit(Event("git", f"Task branch {branch} ready for review"
                           + (f" (accepts merge into {target})" if target else "") + ".",
                           {"branch": branch, "review_target": target}))
        elif self.settings.git_finalize:
            try:
                branch = f"{self.settings.git_branch_prefix}{run.id}"
                info = await asyncio.to_thread(
                    vcs.finalize, run_ws, branch=branch, message=f"ADA: {final_title or prompt[:60]}")
                emit(Event("git", f"Committed on branch {info['branch']} ({info['commit']}).",
                           {"branch": info["branch"], "commit": info["commit"]}))
            except Exception as exc:
                emit(status(f"Git finalize skipped: {exc}"))

        emit(status("Documenting the run (report + brief)…"))
        with self._tracer.span("phase", "summarize"):
            try:
                brief = await self._summarize(run)
            except Exception as exc:  # always produce docs, even if the summary call fails
                passed_n = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
                brief = BriefDoc(
                    tldr=f"Completed {passed_n}/{len(run.subtasks)} subtasks. (Auto-summary unavailable: {exc})",
                    key_points=[f"{s.id} [{s.agent}] {s.spec.title}: {s.status.value}" for s in run.subtasks.values()],
                    status="completed with errors",
                )

        # ---- Tier 4: learn from this run (outcome-aware) ----
        await self._reflect_and_learn(run, brief, emit)
        await asyncio.to_thread(self._index_artifacts, run, brief, run_ws)
        self._record_agent_outcomes(run)
        self.kg.save()

        out_dir = write_task_docs(self.settings, run, brief, self.bus.history,
                                  run.execution, activity=self._activity)

        passed = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
        tests = "n/a" if run.execution is None else ("passed" if run.execution.passed else "failed")
        usage = getattr(self.provider, "usage", None)
        usage_dict = usage.to_dict() if usage else {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        quality = self._quality_score(run)
        rollup = "over_budget" if over_budget else run.rollup_status()

        self.runs.finish(
            run.id, status=rollup, run_status=run.rollup_status(),
            quality_score=quality,
            subtasks_total=len(run.subtasks), subtasks_passed=passed,
            tests=tests, summary=brief.tldr, sessions_spawned=pool.created_total,
            sessions_reaped=pool.reaped_total,
            kg_nodes=self.kg.num_nodes, kg_edges=self.kg.num_edges,
            memories=self._mem_writes, messages=len(self.bus.history),
            **{k: v for k, v in usage_dict.items() if k != "calls"},
        )

        emit(Event("brief", brief.tldr, {
            "tldr": brief.tldr, "key_points": brief.key_points, "status": brief.status,
        }))
        emit(Event("done", "Run complete.", {
            "task_id": run.id, "passed": passed, "total": len(run.subtasks),
            "docs_dir": str(out_dir), "tests": tests, "over_budget": over_budget,
            "run_status": rollup, "quality_score": quality,
            **usage_dict, **self._metrics(),
        }))
        return brief, out_dir

    async def aclose(self) -> None:
        await self.provider.aclose()
        self.memory.close()
        self.runs.close()
        if self._code_index is not None:
            self._code_index.close()
            self._code_index = None

    # ---- internals ----
    def _recall_prior(self, prompt: str) -> str:
        """Pull relevant lessons + recent human feedback from past runs for planning."""
        parts: list[str] = []
        try:
            hits = self.memory.recall("longterm", prompt, top_k=5, min_score=0.15, decay=True)
            parts.extend(f"- {h.content}" for h in hits)
        except Exception as exc:  # a store/embedding error must not abort planning
            logger.warning("prior recall failed: %s", exc)
        fb = self._feedback_text()
        if fb:
            parts.append("Recent human feedback on past runs (weigh this heavily):")
            parts.append(fb)
        return "\n".join(parts)

    def _workspace_sibling_context(self, query: str) -> tuple[str, str] | None:
        """One extra context part borrowing memories/KB hits from workspace siblings.

        Strictly conditional: unless ``settings.workspace_context`` is on AND the
        active project belongs to a workspace with at least one sibling, this
        returns None and the caller's parts list stays byte-identical to a
        workspace-less run. Sibling stores are opened through the memory module's
        never-create wrapper and used read-only, so a sibling without a memory.db
        contributes nothing and no files are ever created on its behalf.
        """
        if not self.settings.workspace_context:
            return None
        try:
            ws_slug = workspace_of(self.settings, self.settings.project)
            if ws_slug is None:
                return None
            ws = get_workspace(self.settings, ws_slug) or {}
            me = slugify(self.settings.project)
            siblings = [s for s in ws.get("project_slugs", []) if s != me]
            if not siblings:
                return None
        except Exception as exc:  # a broken workspaces file must not cost the run
            logger.debug("workspace lookup skipped: %s", exc)
            return None
        lines: list[str] = []
        for slug in siblings[:_SIBLING_PROJECTS_MAX]:
            store = _open_sibling_store(self.settings, slug)
            if store is None:  # sibling has no memory store yet — never create one
                continue
            try:
                for hit in store.recall("longterm", query, top_k=2, min_score=0.15,
                                        decay=True):
                    lines.append(f"[{slug}] {hit.content[:_SIBLING_ITEM_CHARS]}")
                for hit in KnowledgeBase(store.vectors).search(query, top_k=2,
                                                               min_score=0.2):
                    lines.append(f"[{slug}] {hit.text[:_SIBLING_ITEM_CHARS]}")
            except Exception as exc:  # one sibling's failure is not the run's problem
                logger.debug("sibling context from %r skipped: %s", slug, exc)
            finally:
                store.close()
        body: list[str] = []
        used = 0
        for line in lines:  # bounded: the whole borrowed section stays small
            item = f"- {line}"
            if used + len(item) + 1 > _SIBLING_SECTION_CHARS:
                break
            body.append(item)
            used += len(item) + 1
        if not body:
            return None
        name = str(ws.get("name") or ws_slug)
        return (f"Related knowledge from workspace '{name}'",
                untrusted("\n".join(body), source="workspace-sibling"))

    def _feedback_text(self, limit: int = 5) -> str:
        """Human feedback is the highest-signal learning input — feed it to the planner (M3)."""
        try:
            rows = self.runs.recent_feedback(limit=limit)
        except Exception:
            return ""
        lines = []
        for r in rows:
            verdictish = ("accepted" if r.get("accepted") else "rejected") if r.get("accepted") is not None else ""
            rating = f"rated {r['rating']}/5" if r.get("rating") is not None else ""
            note = (r.get("comment") or "").strip()
            desc = ", ".join(x for x in (verdictish, rating) if x)
            if desc or note:
                lines.append(f"- [{desc}] task '{(r.get('prompt') or '')[:60]}': {note}".rstrip(": "))
        return "\n".join(lines)

    def _routing_signals(self) -> tuple[dict[str, dict], dict[str, list[bool]]]:
        """Per-role outcome aggregates + this project's recent outcomes, for the
        planning catalog's record notes and per-project demotion. Best-effort."""
        try:
            stats = self.runs.agent_stats()
        except Exception:
            stats = {}
        try:
            recent = self.runs.agent_recent_project_outcomes(
                self.settings.project or "default")
        except Exception:
            recent = {}
        return stats, recent

    def _track_record_text(self) -> str:
        try:
            tr = self.runs.agent_track_record()
        except Exception:
            return ""
        good = [f"{a}: {d['pass_rate']:.0%} over {d['n']} subtasks"
                + ("" if d["n"] >= 5 else " (limited data)")
                for a, d in sorted(tr.items()) if d["n"]]
        return "\n".join(good)

    async def _reflect_and_learn(self, run: TaskRun, brief: BriefDoc, emit: EventFn) -> None:
        """Distill outcome-aware lessons and write them to long-term memory (deduped)."""
        rollup = run.rollup_status()
        try:
            with self._tracer.span("phase", "reflect"):
                lessons = await self.reflector.reflect(run)
            tag = run.prompt[:80]
            self.memory.remember_unique("longterm", self._scrub(f"[{tag}] {lessons.summary}"),
                                        metadata={"task": run.id, "kind": "summary", "outcome": rollup})
            for item in lessons.what_worked[:4]:
                self.memory.remember_unique("longterm", self._scrub(f"[DO] {item}"),
                                            metadata={"task": run.id, "kind": "do"})
            for item in lessons.what_to_avoid[:4]:
                self.memory.remember_unique("longterm", self._scrub(f"[AVOID] {item}"),
                                            metadata={"task": run.id, "kind": "avoid"})
            for item in lessons.routing_notes[:3]:
                self.memory.remember_unique("longterm", self._scrub(f"[ROUTING] {item}"),
                                            metadata={"task": run.id, "kind": "routing"})
            self._mem_writes += 1
            emit(Event("reflection", lessons.summary, {
                "summary": lessons.summary, "what_worked": lessons.what_worked,
                "what_to_avoid": lessons.what_to_avoid, "outcome": rollup,
            }))
        except Exception as exc:  # reflection is best-effort; degrade to a plain note
            logger.info("reflection skipped: %s", exc)
            try:
                self.memory.remember_unique("longterm", f"[{run.prompt[:80]}] {brief.tldr}",
                                            metadata={"task": run.id, "outcome": rollup})
                self._mem_writes += 1
            except Exception:
                pass

    def _index_artifacts(self, run: TaskRun, brief: BriefDoc, run_ws: Path) -> None:
        """Index this run's brief/report + produced source into the KB so kb_search goes live."""
        try:
            self.kb.reingest(f"brief:{run.id}",
                             self._scrub(brief.tldr + "\n" + "\n".join(brief.key_points)))
            for rel in self._workspace_files(run, run_ws)[:40]:
                p = run_ws / rel
                if p.suffix in (".py", ".js", ".ts", ".md", ".txt", ".rs", ".go") and p.is_file():
                    self.kb.reingest(f"code:{run.id}:{rel}", self._scrub(p.read_text(errors="replace")))
        except Exception as exc:
            logger.info("artifact indexing skipped: %s", exc)

    # ---- checkpoint / resume (R1) ----
    def _checkpoint(self, run: TaskRun, state: SubTaskState) -> None:
        try:
            self.runs.checkpoint_subtask(
                run.id, state.id, status=state.status.value, attempts=state.attempts,
                result=self._scrub(state.result or ""),
                verdict_json=state.verdict.model_dump_json() if state.verdict else None,
                error=state.error or "",
                merge_commit=self._merge_by_subtask.get(state.id, ""),
                changed_json=json.dumps(self._changed_by_subtask.get(state.id, [])))
        except Exception:
            logger.debug("checkpoint failed for %s/%s", run.id, state.id, exc_info=True)

    def _checkpoint_all(self, run: TaskRun) -> None:
        for st in run.subtasks.values():
            if st.status is not RunStatus.PENDING or st.attempts:
                self._checkpoint(run, st)

    def _restore_checkpoints(self, run: TaskRun) -> int:
        """Load persisted subtask states; completed ones are skipped by the scheduler."""
        try:
            stored = self.runs.get_subtask_states(run.id)
        except Exception:
            return 0
        restored = 0
        for sid, row in stored.items():
            st = run.subtasks.get(sid)
            if st is None:
                continue
            st.attempts = int(row.get("attempts") or 0)
            if row.get("status") in ("passed", "passed_with_caveats"):
                st.status = RunStatus(row["status"])
                st.result = row.get("result") or ""
                if row.get("verdict"):
                    try:
                        st.verdict = Verdict.model_validate_json(row["verdict"])
                    except Exception:
                        pass
                restored += 1
            # anything not completed re-runs from PENDING (attempts preserved)
        return restored

    def _record_agent_outcomes(self, run: TaskRun) -> None:
        for st in run.subtasks.values():
            if st.verdict is not None:
                try:
                    self.runs.record_agent_outcome(run.id, st.agent, st.verdict.passed, st.verdict.score)
                except Exception:
                    pass

    def _quality_score(self, run: TaskRun) -> float:
        """0-100: criteria pass-rate, weighted down by failed objective tests and retries."""
        total = len(run.subtasks) or 1
        passed = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
        soft = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED_WITH_CAVEATS)
        base = 100.0 * (passed + 0.5 * soft) / total
        if run.execution is not None and not run.execution.passed:
            base *= 0.7
        retries = sum(max(0, s.attempts - 1) for s in run.subtasks.values())
        base -= min(15.0, retries * 3.0)
        return round(max(0.0, min(100.0, base)), 1)

    def _make_replan(self, run: TaskRun, emit: EventFn):
        """Adaptive replanning hook: inject a bounded repair subtask for a failed one."""
        async def replan(r: TaskRun) -> int:
            if self._repairs >= _MAX_REPAIRS:
                return 0
            added = 0
            for st in list(r.subtasks.values()):
                if st.status is RunStatus.FAILED and not st.id.endswith("-fix"):
                    fix_id = f"{st.id}-fix"
                    if fix_id in r.subtasks:
                        continue
                    fix = SubTask(
                        id=fix_id, title=f"Repair: {st.spec.title}",
                        description=(f"A previous attempt at '{st.spec.title}' failed: "
                                     f"{st.error or (st.verdict.reasons[0] if st.verdict and st.verdict.reasons else 'review failed')}. "
                                     "Diagnose and fix it so the acceptance criteria are met."),
                        agent="debugger" if "debugger" in self.agents else st.agent,
                        rationale="adaptive replan: repair a failed subtask",
                        depends_on=[d for d in st.spec.depends_on if d in r.subtasks],
                        acceptance_criteria=st.spec.acceptance_criteria,
                    )
                    r.plan.subtasks.append(fix)
                    r.subtasks[fix_id] = SubTaskState(spec=fix)
                    self._repairs += 1
                    added += 1
                    # Structured so the UI can add a card/node for the mid-run subtask.
                    emit(Event("plan_update", f"Adaptive replan: added repair subtask {fix_id}.", {
                        "id": fix.id, "title": fix.title, "agent": fix.agent,
                        "depends_on": list(fix.depends_on), "repairs": st.id,
                    }))
                    if self._repairs >= _MAX_REPAIRS:
                        break
            return added
        return replan

    def _make_on_retry(self, emit: EventFn):
        """Retry visibility: surface a review-failed retry in the event stream/UI."""
        def on_retry(state: SubTaskState, attempt: int, max_retries: int,
                     verdict: Verdict) -> None:
            reason = verdict.reasons[0] if verdict.reasons else "review failed"
            emit(Event("subtask_retry",
                       f"Retrying {state.id} (attempt {attempt}/{max_retries}): {reason}",
                       {"id": state.id, "agent": state.agent, "attempt": attempt,
                        "max": max_retries}))
        return on_retry

    def _consolidate_longterm(self, run: TaskRun, brief: BriefDoc) -> None:  # kept for compatibility
        pass

    def _workspace_files(self, run: TaskRun, run_ws: Path | None = None) -> list[str]:
        """Relative paths of files this run actually wrote — ground truth for the reviewer."""
        ws = run_ws if run_ws is not None else self.settings.run_workspace(run.id)
        if not ws.is_dir():
            return []
        out = []
        for p in sorted(ws.rglob("*")):
            if p.is_file() and not _INTERNAL_DIRS.intersection(p.parts):
                out.append(str(p.relative_to(ws)))
        return out[:200]

    def _file_index(self, run_ws: Path) -> dict[str, int]:
        """path -> size, for computing per-subtask diffs cheaply."""
        idx: dict[str, int] = {}
        if not run_ws.is_dir():
            return idx
        for p in run_ws.rglob("*"):
            if p.is_file() and not _INTERNAL_DIRS.intersection(p.parts):
                try:
                    idx[str(p.relative_to(run_ws))] = p.stat().st_size
                except OSError:
                    pass
        return idx

    def _metrics(self) -> dict[str, Any]:
        usage = getattr(self.provider, "usage", None)
        u = usage.to_dict() if usage else {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        return {
            "kg_nodes": self.kg.num_nodes,
            "kg_edges": self.kg.num_edges,
            "messages": len(self.bus.history),
            "memory": self._mem_writes,
            **{k: v for k, v in u.items() if k != "calls"},
        }

    def _seed_kg(self, run: TaskRun) -> None:
        self.kg.add_node(run.id, "task", prompt=run.prompt)
        for st in run.plan.subtasks:
            self.kg.add_node(st.id, "subtask", title=st.title)
            self.kg.add_fact(run.id, "has_subtask", st.id, layer="run", run_id=run.id)
            self.kg.add_fact(st.id, "assigned_to", st.agent, layer="run", run_id=run.id)

    async def _emit_new_messages(self, emit: EventFn) -> None:
        async with self._msg_lock:
            new = self.bus.history[self._last_msg_idx:]
            self._last_msg_idx = len(self.bus.history)
        for m in new:
            emit(Event("message", f"{m.sender} → {m.recipient or 'all'}", {
                "sender": m.sender, "recipient": m.recipient, "content": m.content,
            }))

    def _build_toolbox(self, agent_name: str, task_scope: str, run_ws: Path, *,
                       spawn: Any = None, attention: Any = None) -> ToolBox:
        """ToolContext construction resilient to optional fields (spawn/allow_web) landing."""
        kwargs: dict[str, Any] = dict(
            memory=self.memory, kb=self.kb, kg=self.kg, bus=self.bus,
            agent_name=agent_name, task_scope=task_scope, base_dir=run_ws,
            workspace=run_ws, verify_timeout=self.settings.verify_timeout,
            redact=self.settings.redact_secrets, audit=self._audit,
            allow_run_command=self.settings.allow_run_command, sandbox=self.settings.sandbox,
            sandbox_cpu=self.settings.sandbox_cpu_seconds, sandbox_mem=self.settings.sandbox_mem_mb,
            spawn=spawn, allow_web=self.settings.allow_web,
            protected_paths=self.settings.protected_paths,
            attention=attention,
        )
        fields = {f.name for f in dataclasses.fields(ToolContext)}
        return ToolBox(ToolContext(**{k: v for k, v in kwargs.items() if k in fields}))

    def _make_execute(self, run: TaskRun, emit: EventFn, run_workspace: Path):
        git_lock = asyncio.Lock()  # serializes worktree add/merge on the shared repo

        def usage_snapshot() -> dict[str, Any]:
            u = getattr(self.provider, "usage", None)
            return u.snapshot() if u and hasattr(u, "snapshot") else {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}

        async def execute(state: SubTaskState, deps: dict[str, str], session: Session) -> str:
            budget = self.settings.budget_usd
            usage = getattr(self.provider, "usage", None)
            if budget and usage and usage.cost_usd > budget:
                emit(Event("budget",
                           f"Budget ${budget:.2f} exceeded (${usage.cost_usd:.2f} spent) — stopping new work.",
                           {"budget": budget, "cost": round(usage.cost_usd, 4)}))
                raise BudgetExceeded()
            emit(Event("subtask_start", f"{state.id} [{state.agent}] {state.spec.title}", {
                "id": state.id, "agent": state.agent, "title": state.spec.title,
            }))
            before_files = self._file_index(run_workspace)
            before_cost = usage_snapshot()
            agent = session.agent  # the BaseAgent for this session

            # Per-subtask worktree isolation: parallel subtasks each work on their own
            # checkout and merge back on completion — siblings can't clobber each other,
            # and each subtask lands as its own commit (what per-subtask review
            # acceptance is built on, so it's on even for single-subtask project plans).
            use_wt = (self.settings.worktree_per_subtask
                      and (run_workspace / ".git").exists())
            exec_ws = run_workspace
            if use_wt:
                try:
                    async with git_lock:
                        exec_ws = await asyncio.to_thread(vcs.worktree_add, run_workspace, state.id)
                except Exception as exc:
                    emit(status(f"{state.id}: worktree isolation unavailable ({exc}); "
                                "using the shared workspace."))
                    exec_ws, use_wt = run_workspace, False

            async def spawn(agent_name: str, task: str) -> str:
                """Depth-1 delegation (T4): run a specialist inline on a bounded subtask."""
                sub = self.agents.get(agent_name)
                if sub is None:
                    return f"unknown agent '{agent_name}'"

                async def _go(provider) -> str:
                    sub_box = self._build_toolbox(agent_name, run.id, exec_ws, spawn=None)
                    return await asyncio.wait_for(sub.run(
                        task_text=task[:4000], context="", toolbox=sub_box,
                        provider=provider, workdir=str(exec_ws), on_step=on_step),
                        timeout=self.settings.subtask_max_seconds or None)

                try:
                    return await _go(self.provider)
                except asyncio.TimeoutError:
                    return "[delegation stopped: wall-clock cap]"
                except RuntimeError:
                    # The delegate tool runs this coroutine on a worker-thread event loop;
                    # a provider client bound to the main loop fails with RuntimeError.
                    # Retry once with a dedicated provider instance.
                    fresh = get_provider(self.settings)
                    try:
                        return await _go(fresh)
                    except asyncio.TimeoutError:
                        return "[delegation stopped: wall-clock cap]"
                    finally:
                        await fresh.aclose()

            async def attention(kind: str, text: str, options: list[str]) -> str | None:
                """Attention screens (ask/permission): emit the event the UI renders and
                await the operator's steer-delivered answer. None = no operator/timeout."""
                if self.control is None:
                    return None
                rid = self.control.open_request(kind)
                if kind == "ask":
                    emit(Event("ask", f"[{state.agent}] {text[:140]}", {
                        "id": rid, "agent": state.agent, "question": text,
                        "options": list(options or [])}))
                else:
                    emit(Event("permission", f"[{state.agent}] {text[:140]}", {
                        "id": rid, "agent": state.agent, "request": text}))
                return await self.control.wait_answer(rid, self.settings.attention_timeout or None)

            toolbox = self._build_toolbox(state.agent, run.id, exec_ws, spawn=spawn,
                                          attention=attention)
            criteria = "\n".join(f"- {c}" for c in state.spec.acceptance_criteria) or "- (none)"
            task_text = (
                f"{state.spec.title}\n\n{state.spec.description}\n\nAcceptance criteria:\n{criteria}"
            )
            parts: list[tuple[str, str]] = [
                ("Overall task", run.prompt), ("Plan summary", run.plan.summary)]
            # Upstream result digests: dependents inherit upstream REASONING (bounded
            # per-item/section digests), not just merged files. Absent entirely for
            # dependency-free subtasks — their prompts stay byte-identical.
            up_part = upstream_results_part(run, state)
            if up_part is not None:
                parts.append(up_part)
            if state.verdict and not state.verdict.passed and state.verdict.suggestions:
                parts.append(("Your previous attempt failed review — fix these",
                              "\n".join(f"- {s}" for s in state.verdict.suggestions)))
            if self.control is not None:
                for note in self.control.drain_steer():
                    parts.append(("Steering note from the operator", note))
            # Subtask-scoped context pack (C2): a ranked workspace map + top knowledge hits,
            # so the agent doesn't start blind and burn turns rediscovering the codebase.
            try:
                repo_slice = await asyncio.to_thread(
                    build_repo_map, run_workspace, 2500,
                    f"{state.spec.title} {state.spec.description}")
                if repo_slice:
                    parts.append(("Workspace map (most relevant files first)", repo_slice))
            except Exception:
                pass
            # Semantic code retrieval (repo-workspace runs only; see _start_code_index):
            # the best-matching indexed source chunks, AFTER the repo map and wrapped as
            # untrusted — retrieved file content must never be treated as instructions.
            # self._code_index is None on greenfield/eval runs or with the setting off,
            # so this block adds nothing there (byte-identical prompts, cassette rule).
            if self._code_index is not None:
                try:
                    # role-weighted: the subtask's agent role biases retrieval toward
                    # its path affinities (score boost only, never a hard filter).
                    code_part = await asyncio.to_thread(
                        self._code_index.retrieval_context,
                        f"{state.spec.title} {state.spec.description}",
                        role=state.agent)
                    if code_part:
                        parts.append(("Relevant code (indexed)",
                                      untrusted(code_part, source="code-index")))
                except Exception:
                    pass
            try:
                hits = self.kb.search(f"{state.spec.title} {state.spec.description}",
                                      top_k=3, min_score=0.2)
                if hits:
                    parts.append(("Related knowledge", untrusted(
                        "\n".join(f"- {h.text[:300]}" for h in hits), source="knowledge-base")))
            except Exception:
                pass
            # Workspace-aware context: knowledge borrowed read-only from sibling
            # projects in the same workspace, attributed per project. Appended AFTER
            # the project's own recall/KB parts (lower priority under the budget);
            # absent entirely — byte-identical prompts — when the project is in no
            # workspace, has no siblings, or the setting is off.
            try:
                ws_part = self._workspace_sibling_context(
                    f"{state.spec.title} {state.spec.description}")
                if ws_part:
                    parts.append(ws_part)
            except Exception:
                pass
            context = assemble_context(
                parts, budget_tokens=budget_for(self.settings.agent_model).context_budget_tokens)

            def on_step(step: dict[str, Any], _id=state.id, _agent=state.agent) -> None:
                self._activity.setdefault(_id, []).append(step)
                emit(Event("agent_step", "", {"id": _id, "agent": _agent, **step}))

            cap = self.settings.subtask_max_seconds or None
            run_kwargs: dict[str, Any] = dict(
                task_text=task_text, context=context, toolbox=toolbox,
                provider=self.provider, workdir=str(exec_ws), on_step=on_step)
            if budget:
                # R3: hand the tool loop the remaining budget so it can stop mid-subtask.
                run_kwargs["max_cost_usd"] = max(0.0, budget - float(usage_snapshot()["cost_usd"]))
            try:
                with self._tracer.span("subtask", state.id, agent=state.agent):
                    try:
                        try:
                            coro = agent.run(**run_kwargs)
                        except TypeError:  # BaseAgent without max_cost_usd support
                            run_kwargs.pop("max_cost_usd", None)
                            coro = agent.run(**run_kwargs)
                        result = await asyncio.wait_for(coro, timeout=cap)
                    except asyncio.TimeoutError:
                        raise RuntimeError(
                            f"subtask exceeded the wall-clock cap ({cap:.0f}s); "
                            "raise ADA_SUBTASK_MAX_SECONDS if this task legitimately needs longer")
                if use_wt:
                    async with git_lock:
                        mres = await asyncio.to_thread(
                            vcs.worktree_merge, run_workspace, state.id,
                            message=f"ada: subtask {state.id} ({state.spec.title[:50]})")
                    self._merge_by_subtask[state.id] = mres.get("commit", "") or ""
                    if mres.get("conflict"):
                        conflicts = ", ".join(mres.get("files", [])) or "unknown files"
                        result += (f"\n\n[worktree merge conflict on: {conflicts} — these changes "
                                   "were NOT merged into the shared workspace]")
                        emit(status(f"{state.id}: merge conflict ({conflicts}); changes not merged."))
            finally:
                if use_wt:
                    # under the same lock: a concurrent remove/prune corrupts a sibling's merge
                    async with git_lock:
                        await asyncio.to_thread(vcs.worktree_remove, run_workspace, state.id)
            self.memory.remember(
                run.id, self._scrub(f"[{state.agent}] {state.spec.title} -> {result[:400]}"),
                metadata={"subtask": state.id},
            )
            self._mem_writes += 1
            self.kg.add_fact(state.id, "produced_result_by", state.agent,
                             layer="run", run_id=run.id)
            # per-subtask cost attribution + the file diff (also feeds scoped verification, V2)
            after_cost = usage_snapshot()
            self._cost_by_subtask[state.id] = self._cost_delta(before_cost, after_cost)
            self._changed_by_subtask[state.id] = self._emit_diff(state.id, emit, before_files,
                                                                run_workspace)
            await self._emit_new_messages(emit)
            return result

        return execute

    @staticmethod
    def _cost_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        return {
            "cost_usd": round(float(after.get("cost_usd", 0)) - float(before.get("cost_usd", 0)), 6),
            "input_tokens": int(after.get("input_tokens", 0)) - int(before.get("input_tokens", 0)),
            "output_tokens": int(after.get("output_tokens", 0)) - int(before.get("output_tokens", 0)),
        }

    def _emit_diff(self, sid: str, emit: EventFn, before: dict[str, int], run_ws: Path) -> list[str]:
        after = self._file_index(run_ws)
        added = sorted(set(after) - set(before))
        modified = sorted(p for p in (set(after) & set(before)) if after[p] != before[p])
        if added or modified:
            emit(Event("diff", f"{sid}: +{len(added)} files, ~{len(modified)} changed", {
                "id": sid, "added": added[:50], "modified": modified[:50],
            }))
        return added + modified

    def _make_verify(self, run: TaskRun, emit: EventFn, run_ws: Path):
        async def verify(state: SubTaskState, result: str):
            files = self._workspace_files(run, run_ws)
            changed = self._changed_by_subtask.get(state.id, [])
            with self._tracer.span("verify", state.id):
                verdict = await self.reviewer.verify(
                    title=state.spec.title,
                    description=state.spec.description,
                    acceptance_criteria=state.spec.acceptance_criteria,
                    result=result,
                    workspace_files=files,
                    changed_files=changed,
                    # T6: the reviewer reads what THIS subtask touched, not a whole-repo dump
                    file_contents=(self._collect_contents(run_ws, changed or files)
                                   if self.settings.objective_review else ""),
                )
            # ---- Tier 3: fold objective signals (tests/lint) into the verdict ----
            if self.settings.objective_review:
                # V2: run only the tests relevant to what THIS subtask changed — never the
                # whole workspace suite (siblings can't fail each other; N× cheaper).
                test_paths = self._select_test_paths(run_ws, changed)
                signals = await asyncio.to_thread(
                    gather_signals, run_ws, files,
                    run_tests=self.settings.verify_run_tests and bool(test_paths),
                    lint=self.settings.lint_check,
                    timeout=self.settings.verify_timeout, test_paths=test_paths,
                    # Static gate: re-count the run-baseline's detected checks in this
                    # subtask's post-change workspace; deltas fold into the same
                    # objective_note/demotion channel as the test gate. Detection empty
                    # or setting off → key absent → nothing appended anywhere.
                    static_checks=(self._baseline.get("static_checks")
                                   if self.settings.objective_static else None))
                verdict = apply_objective_gate(verdict, signals, baseline=self._baseline)
            self.kg.add_fact(state.id, "review_status", "passed" if verdict.passed else "failed",
                             layer="run", run_id=run.id)
            result_text = self._scrub(result or "")
            if len(result_text) > 12000:
                result_text = result_text[:12000] + "\n… (truncated)"
            emit(Event("subtask_review",
                       f"{state.id} {'passed' if verdict.passed else 'failed'} (score {verdict.score})",
                       {
                           "id": state.id, "passed": verdict.passed, "score": verdict.score,
                           "attempts": state.attempts, "reasons": verdict.reasons,
                           "objective_note": verdict.objective_note,
                           "result": result_text, "cost": self._cost_by_subtask.get(state.id),
                           **self._metrics(),
                       }))
            state.verdict = verdict
            self._checkpoint(run, state)  # R1: survive interruption mid-run
            return verdict

        return verify

    @staticmethod
    def _select_test_paths(run_ws: Path, changed: list[str]) -> list[str]:
        """Test files relevant to a subtask's changes (V2): the test files it touched, plus
        tests named after the modules it touched."""
        if not run_ws.is_dir() or not changed:
            return []

        def is_test(rel: str) -> bool:
            name = Path(rel).name
            return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")

        paths = {rel for rel in changed if is_test(rel) and (run_ws / rel).is_file()}
        stems = {Path(rel).stem for rel in changed if rel.endswith(".py") and not is_test(rel)}
        for stem in stems:
            for pat in (f"test_{stem}.py", f"{stem}_test.py"):
                for p in run_ws.rglob(pat):
                    if p.is_file() and ".git" not in p.parts:
                        paths.add(str(p.relative_to(run_ws)))
        return sorted(paths)

    @staticmethod
    def _collect_contents(run_ws: Path, files: list[str]) -> str:
        from .verification import collect_file_contents
        return collect_file_contents(run_ws, files)

    async def _summarize(self, run: TaskRun) -> BriefDoc:
        parts = [f"Task: {run.prompt}", f"Approach: {run.plan.summary}", "", "Subtask outcomes:"]
        for st in run.subtasks.values():
            parts.append(f"- {st.id} [{st.agent}] {st.spec.title}: {st.status.value}")
            if st.result:
                parts.append(f"  result: {st.result[:600]}")
        if run.execution is not None:
            ex = run.execution
            parts.append(f"\nTest execution: `{ex.command}` exit={ex.return_code} "
                         f"({'PASSED' if ex.passed else 'FAILED'}).")
        user = "\n".join(parts) + "\n\nSummarize this run."
        return await self.provider.structured(
            system=_SUMMARY_SYSTEM,
            user=user,
            schema=BriefDoc,
            model=self.settings.orchestrator_model,
            effort=self.settings.agent_effort,
            max_tokens=2000,
        )
