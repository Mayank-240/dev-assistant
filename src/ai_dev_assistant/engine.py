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
import json
import logging
from pathlib import Path
from typing import Any, Callable

from . import vcs
from .agents.orchestrator import Orchestrator
from .agents.reflector import Reflector
from .agents.registry import build_agents
from .agents.reviewer import Reviewer
from .config import Settings
from .context import assemble as assemble_context
from .docs.writer import write_task_docs
from .knowledge.base import KnowledgeBase
from .knowledge.extract import enrich_kg_from_workspace
from .knowledge.graph import NetworkXKnowledgeGraph
from .knowledge.repo_map import build_repo_map, onboard
from .llm.factory import get_provider
from .llm.schemas import BriefDoc, Plan, SubTask
from .memory.store import MemoryStore, ScopedMemory
from .orchestration.events import Event, status
from .orchestration.message_bus import MessageBus
from .orchestration.run_control import RunControl
from .orchestration.run_store import RunStore
from .orchestration.scheduler import BudgetExceeded, Scheduler
from .orchestration.session_pool import Session, SessionPool
from .orchestration.task import RunStatus, SubTaskState, TaskRun, new_task_id
from .orchestration.trace import Tracer
from .security.redaction import AuditLog
from .tools.registry import ToolBox, ToolContext
from .verification import apply_objective_gate, gather_signals

logger = logging.getLogger("ada.engine")

_SUMMARY_SYSTEM = (
    "You summarize a completed multi-agent task run for a human who wants the story in ten "
    "seconds. Be concrete about what was actually produced and the outcome. The TL;DR is two "
    "or three sentences; key_points are the few things that matter."
)

EventFn = Callable[[Event], None]
_MAX_REPAIRS = 2  # adaptive-replan safety cap per run


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
        self._repairs = 0
        self._tracer: Tracer | None = None
        self._audit: AuditLog | None = None

    def ingest_doc(self, doc_id: str, text: str) -> int:
        return self.kb.ingest(doc_id, text)

    async def make_plan(self, prompt: str) -> Plan:
        """Just the orchestrator step — used by the plan-approval flow."""
        return await self.orchestrator.make_plan(
            prompt, self.agents, prior_knowledge=self._recall_prior(prompt),
            track_record=self._track_record_text(),
        )

    async def run(
        self,
        prompt: str,
        *,
        plan: Plan | None = None,
        task_id: str | None = None,
        title: str | None = None,
        on_event: EventFn | None = None,
    ) -> tuple[TaskRun, BriefDoc, Path]:
        base_emit: EventFn = on_event or (lambda _e: None)
        self._activity = {}
        self._cost_by_subtask = {}
        self._repairs = 0
        events_log: list[dict[str, Any]] = []

        def emit(e: Event) -> None:
            events_log.append(e.to_dict())
            base_emit(e)

        rid = task_id or new_task_id()  # fix the id now so workspace/trace paths are per-run
        run_ws = self.settings.run_workspace(rid)
        run_ws.mkdir(parents=True, exist_ok=True)  # agents (and repo materialize) write here
        self._tracer = Tracer(self.settings.docs_dir / rid / "trace.jsonl", enabled=self.settings.trace)
        self._audit = AuditLog(self.settings.docs_dir / rid / "audit.jsonl", enabled=self.settings.audit_log)

        # ---- Tier 2: bind to a real repository (or git-init a greenfield workspace) ----
        repo_context = ""
        if self.settings.repo_backed:
            emit(status("Materializing repository into the workspace…"))
            try:
                info = await asyncio.to_thread(
                    vcs.materialize, dest=run_ws, repo_url=self.settings.repo_url,
                    repo_path=self.settings.repo_path, repo_ref=self.settings.repo_ref)
                emit(status(f"Repository ready ({info['mode']} @ {info.get('head', '')[:8]})."))
                repo_context = build_repo_map(run_ws)
                onb = onboard(self.kb, self.kg, run_ws, rid or "run")
                if onb["files"]:
                    emit(status(f"Onboarded {onb['files']} repo file(s) into the KB + graph."))
            except Exception as exc:  # never let repo setup abort the whole run
                emit(status(f"Repository setup failed ({exc}); continuing greenfield."))

        if plan is None:
            prior = self._recall_prior(prompt)
            if prior:
                emit(status(f"Recalled {len(prior.splitlines())} relevant lesson(s) from past runs."))
            emit(status("Orchestrator: decomposing the task…"))
            with self._tracer.span("phase", "plan"):
                plan = await self.orchestrator.make_plan(
                    prompt, self.agents, prior_knowledge=prior, repo_context=repo_context,
                    track_record=self._track_record_text())
        else:
            plan = Orchestrator._sanitize(plan, self.agents)  # validate an edited/approved plan

        run = TaskRun.from_plan(prompt, plan, rid)
        try:
            run.validate()  # structural DAG check (cycles / dup ids / dangling deps)
        except Exception as exc:
            emit(Event("error", f"Invalid plan: {exc}", {"message": str(exc)}))
            self.runs.set_status(run.id, "failed")
            raise

        final_title = (title or "").strip() or (getattr(plan, "title", "") or "").strip() or None
        self.runs.start(run.id, prompt, title=final_title)
        self._seed_kg(run)
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

            # Objective verification: run the generated tests in the run's workspace.
            if self.settings.verify_run_tests:
                from .execution import run_workspace_tests
                emit(status("Running generated tests in the workspace…"))
                try:
                    run.execution = await run_workspace_tests(run_ws, self.settings.verify_timeout)
                except Exception as exc:  # never let test execution abort the run
                    run.execution = None
                    emit(status(f"Test execution skipped: {exc}"))
                if run.execution is not None:
                    self.kg.add_fact(run.id, "tests", "passed" if run.execution.passed else "failed")
                    emit(Event(
                        "execution",
                        f"Tests {'passed' if run.execution.passed else 'failed'}: {run.execution.command}",
                        {"ran": True, "passed": run.execution.passed, "command": run.execution.command,
                         "return_code": run.execution.return_code,
                         "duration": round(run.execution.duration, 1), "timed_out": run.execution.timed_out},
                    ))
                else:
                    emit(Event("execution", "No runnable tests detected in the workspace.", {"ran": False}))

            # Enrich the knowledge graph with real code entities from the generated files.
            n_files = enrich_kg_from_workspace(self.kg, run_ws, run.id)
            if n_files:
                emit(status(f"Indexed {n_files} generated file(s) into the knowledge graph."))

            # ---- Tier 2: deliver the work as a git branch/commit (optional) ----
            if self.settings.git_finalize:
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
            self._index_artifacts(run, brief, run_ws)
            self._record_agent_outcomes(run)
            self.kg.save()

            out_dir = write_task_docs(self.settings, run, brief, self.bus.history,
                                      run.execution, activity=self._activity)
            self._write_events_log(out_dir, events_log)

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
            finalized = True

            emit(Event("brief", brief.tldr, {
                "tldr": brief.tldr, "key_points": brief.key_points, "status": brief.status,
            }))
            emit(Event("done", "Run complete.", {
                "task_id": run.id, "passed": passed, "total": len(run.subtasks),
                "docs_dir": str(out_dir), "tests": tests, "over_budget": over_budget,
                "run_status": rollup, "quality_score": quality,
                **usage_dict, **self._metrics(),
            }))
            return run, brief, out_dir
        except asyncio.CancelledError:
            self.runs.set_status(run.id, "cancelled")
            raise
        finally:
            if not finalized:
                # Any non-terminal run after an unexpected failure is marked failed, not left
                # stranded 'running'.
                cur = self.runs.get(run.id) or {}
                if cur.get("status") in (None, "running"):
                    self.runs.set_status(run.id, "failed")

    async def aclose(self) -> None:
        await self.provider.aclose()
        self.memory.close()
        self.runs.close()

    # ---- internals ----
    def _recall_prior(self, prompt: str) -> str:
        """Pull relevant lessons from cross-run long-term memory for planning."""
        try:
            hits = self.memory.recall("longterm", prompt, top_k=5, min_score=0.15, decay=True)
        except Exception as exc:  # a store/embedding error must not abort planning
            logger.warning("prior recall failed: %s", exc)
            return ""
        return "\n".join(f"- {h.content}" for h in hits)

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
            self.memory.remember_unique("longterm", f"[{tag}] {lessons.summary}",
                                        metadata={"task": run.id, "kind": "summary", "outcome": rollup})
            for item in lessons.what_worked[:4]:
                self.memory.remember_unique("longterm", f"[DO] {item}",
                                            metadata={"task": run.id, "kind": "do"})
            for item in lessons.what_to_avoid[:4]:
                self.memory.remember_unique("longterm", f"[AVOID] {item}",
                                            metadata={"task": run.id, "kind": "avoid"})
            for item in lessons.routing_notes[:3]:
                self.memory.remember_unique("longterm", f"[ROUTING] {item}",
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
            self.kb.reingest(f"brief:{run.id}", brief.tldr + "\n" + "\n".join(brief.key_points))
            for rel in self._workspace_files(run, run_ws)[:40]:
                p = run_ws / rel
                if p.suffix in (".py", ".js", ".ts", ".md", ".txt", ".rs", ".go") and p.is_file():
                    self.kb.reingest(f"code:{run.id}:{rel}", p.read_text(errors="replace"))
        except Exception as exc:
            logger.info("artifact indexing skipped: %s", exc)

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

    def _write_events_log(self, out_dir: Path, events: list[dict[str, Any]]) -> None:
        try:
            with (out_dir / "events.jsonl").open("w") as fh:
                for i, e in enumerate(events):
                    fh.write(json.dumps({"seq": i, **e}, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

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
                    emit(status(f"Adaptive replan: added repair subtask {fix_id}."))
                    if self._repairs >= _MAX_REPAIRS:
                        break
            return added
        return replan

    def _consolidate_longterm(self, run: TaskRun, brief: BriefDoc) -> None:  # kept for compatibility
        pass

    def _workspace_files(self, run: TaskRun, run_ws: Path | None = None) -> list[str]:
        """Relative paths of files this run actually wrote — ground truth for the reviewer."""
        ws = run_ws if run_ws is not None else self.settings.run_workspace(run.id)
        if not ws.is_dir():
            return []
        out = []
        for p in sorted(ws.rglob("*")):
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
                out.append(str(p.relative_to(ws)))
        return out[:200]

    def _file_index(self, run_ws: Path) -> dict[str, int]:
        """path -> size, for computing per-subtask diffs cheaply."""
        idx: dict[str, int] = {}
        if not run_ws.is_dir():
            return idx
        for p in run_ws.rglob("*"):
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
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
            self.kg.add_fact(run.id, "has_subtask", st.id)
            self.kg.add_fact(st.id, "assigned_to", st.agent)

    async def _emit_new_messages(self, emit: EventFn) -> None:
        async with self._msg_lock:
            new = self.bus.history[self._last_msg_idx:]
            self._last_msg_idx = len(self.bus.history)
        for m in new:
            emit(Event("message", f"{m.sender} → {m.recipient or 'all'}", {
                "sender": m.sender, "recipient": m.recipient, "content": m.content,
            }))

    def _make_execute(self, run: TaskRun, emit: EventFn, run_workspace: Path):
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
            toolbox = ToolBox(
                ToolContext(
                    memory=self.memory, kb=self.kb, kg=self.kg, bus=self.bus,
                    agent_name=state.agent, task_scope=run.id, base_dir=run_workspace,
                    workspace=run_workspace, verify_timeout=self.settings.verify_timeout,
                    redact=self.settings.redact_secrets, audit=self._audit,
                    allow_run_command=self.settings.allow_run_command, sandbox=self.settings.sandbox,
                    sandbox_cpu=self.settings.sandbox_cpu_seconds, sandbox_mem=self.settings.sandbox_mem_mb,
                )
            )
            criteria = "\n".join(f"- {c}" for c in state.spec.acceptance_criteria) or "- (none)"
            task_text = (
                f"{state.spec.title}\n\n{state.spec.description}\n\nAcceptance criteria:\n{criteria}"
            )
            parts: list[tuple[str, str]] = [
                ("Overall task", run.prompt), ("Plan summary", run.plan.summary)]
            for dep_id, dep_result in deps.items():
                parts.append((f"Result of dependency {dep_id}", dep_result))
            if state.verdict and not state.verdict.passed and state.verdict.suggestions:
                parts.append(("Your previous attempt failed review — fix these",
                              "\n".join(f"- {s}" for s in state.verdict.suggestions)))
            if self.control is not None:
                for note in self.control.drain_steer():
                    parts.append(("Steering note from the operator", note))
            context = assemble_context(parts, budget_tokens=6000)

            def on_step(step: dict[str, Any], _id=state.id, _agent=state.agent) -> None:
                self._activity.setdefault(_id, []).append(step)
                emit(Event("agent_step", "", {"id": _id, "agent": _agent, **step}))

            with self._tracer.span("subtask", state.id, agent=state.agent):
                result = await agent.run(
                    task_text=task_text, context=context, toolbox=toolbox,
                    provider=self.provider, workdir=str(run_workspace), on_step=on_step,
                )
            self.memory.remember(
                run.id, f"[{state.agent}] {state.spec.title} -> {result[:400]}",
                metadata={"subtask": state.id},
            )
            self._mem_writes += 1
            self.kg.add_fact(state.id, "produced_result_by", state.agent)
            # per-subtask cost attribution + a cheap file diff
            after_cost = usage_snapshot()
            self._cost_by_subtask[state.id] = self._cost_delta(before_cost, after_cost)
            self._emit_diff(state.id, emit, before_files, run_workspace)
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

    def _emit_diff(self, sid: str, emit: EventFn, before: dict[str, int], run_ws: Path) -> None:
        after = self._file_index(run_ws)
        added = sorted(set(after) - set(before))
        modified = sorted(p for p in (set(after) & set(before)) if after[p] != before[p])
        if not (added or modified):
            return
        emit(Event("diff", f"{sid}: +{len(added)} files, ~{len(modified)} changed", {
            "id": sid, "added": added[:50], "modified": modified[:50],
        }))

    def _make_verify(self, run: TaskRun, emit: EventFn, run_ws: Path):
        async def verify(state: SubTaskState, result: str):
            files = self._workspace_files(run, run_ws)
            with self._tracer.span("verify", state.id):
                verdict = await self.reviewer.verify(
                    title=state.spec.title,
                    description=state.spec.description,
                    acceptance_criteria=state.spec.acceptance_criteria,
                    result=result,
                    workspace_files=files,
                    file_contents=(self._collect_contents(run_ws, files)
                                   if self.settings.objective_review else ""),
                )
            # ---- Tier 3: fold objective signals (tests/lint) into the verdict ----
            if self.settings.objective_review:
                signals = await asyncio.to_thread(
                    gather_signals, run_ws, files,
                    run_tests=self._has_tests(run_ws, files), lint=self.settings.lint_check,
                    timeout=self.settings.verify_timeout)
                verdict = apply_objective_gate(verdict, signals)
            self.kg.add_fact(state.id, "review_status", "passed" if verdict.passed else "failed")
            result_text = result or ""
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
            return verdict

        return verify

    @staticmethod
    def _has_tests(run_ws: Path, files: list[str]) -> bool:
        return run_ws.is_dir() and (any(f.startswith("test_") or f.endswith("_test.py") for f in files)
                                    or any(run_ws.rglob("test_*.py")))

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
