"""The engine wires every subsystem together and runs a task end to end.

Flow: orchestrator makes a plan -> scheduler runs subtasks in parallel through the
session pool -> each result is verified by the reviewer -> the run is summarized and
documented (full report + brief), and the knowledge graph is persisted.

Progress is reported as structured ``Event`` objects so both the CLI and the web UI can
consume the same stream.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from .agents.orchestrator import Orchestrator
from .agents.registry import build_agents
from .agents.reviewer import Reviewer
from .config import Settings
from .docs.writer import write_task_docs
from .knowledge.base import KnowledgeBase
from .knowledge.extract import enrich_kg_from_workspace
from .knowledge.graph import NetworkXKnowledgeGraph
from .llm.factory import get_provider
from .llm.schemas import BriefDoc, Plan
from .memory.store import MemoryStore, ScopedMemory
from .orchestration.events import Event, status
from .orchestration.message_bus import MessageBus
from .orchestration.run_store import RunStore
from .orchestration.scheduler import BudgetExceeded, Scheduler
from .orchestration.session_pool import Session, SessionPool
from .orchestration.task import RunStatus, SubTaskState, TaskRun
from .tools.registry import ToolBox, ToolContext

logger = logging.getLogger("ada.engine")

_SUMMARY_SYSTEM = (
    "You summarize a completed multi-agent task run for a human who wants the story in ten "
    "seconds. Be concrete about what was actually produced and the outcome. The TL;DR is two "
    "or three sentences; key_points are the few things that matter."
)

EventFn = Callable[[Event], None]


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
        self.runs = RunStore(settings.data_dir / "runs.db")
        self.last_pool_stats: dict[str, int] = {}
        self._mem_writes = 0
        self._last_msg_idx = 0
        self._msg_lock = asyncio.Lock()
        self._activity: dict[str, list[dict[str, Any]]] = {}  # subtask id -> agent steps

    def ingest_doc(self, doc_id: str, text: str) -> int:
        return self.kb.ingest(doc_id, text)

    async def make_plan(self, prompt: str) -> Plan:
        """Just the orchestrator step — used by the plan-approval flow."""
        return await self.orchestrator.make_plan(
            prompt, self.agents, prior_knowledge=self._recall_prior(prompt)
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
        emit: EventFn = on_event or (lambda _e: None)
        self._activity = {}

        if plan is None:
            prior = self._recall_prior(prompt)
            if prior:
                emit(status(f"Recalled {len(prior.splitlines())} relevant lesson(s) from past runs."))
            emit(status("Orchestrator: decomposing the task…"))
            plan = await self.orchestrator.make_plan(prompt, self.agents, prior_knowledge=prior)
        else:
            plan = Orchestrator._sanitize(plan, self.agents)  # validate an edited/approved plan

        run = TaskRun.from_plan(prompt, plan, task_id)
        # Prefer a user-supplied title; otherwise the orchestrator's LLM-written one;
        # RunStore falls back to deriving from the prompt if both are blank.
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
            execute=self._make_execute(run, emit),
            verify=self._make_verify(run, emit),
            max_retries=self.settings.max_retries,
        )
        over_budget = False
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
                run_ws = self.settings.workspace_dir / run.id
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
            n_files = enrich_kg_from_workspace(self.kg, self.settings.workspace_dir / run.id, run.id)
            if n_files:
                emit(status(f"Indexed {n_files} generated file(s) into the knowledge graph."))

            emit(status("Documenting the run (report + brief)…"))
            try:
                brief = await self._summarize(run)
            except Exception as exc:  # always produce docs, even if the summary call fails
                passed_n = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
                brief = BriefDoc(
                    tldr=f"Completed {passed_n}/{len(run.subtasks)} subtasks. (Auto-summary unavailable: {exc})",
                    key_points=[f"{s.id} [{s.agent}] {s.spec.title}: {s.status.value}" for s in run.subtasks.values()],
                    status="completed with errors",
                )
            self._consolidate_longterm(run, brief)
            self.kg.save()
            out_dir = write_task_docs(self.settings, run, brief, self.bus.history,
                                      run.execution, activity=self._activity)

            passed = sum(1 for s in run.subtasks.values() if s.status is RunStatus.PASSED)
            tests = "n/a" if run.execution is None else ("passed" if run.execution.passed else "failed")
            usage = getattr(self.provider, "usage", None)
            usage_dict = usage.to_dict() if usage else {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

            self.runs.finish(
                run.id, status="over_budget" if over_budget else "completed",
                subtasks_total=len(run.subtasks), subtasks_passed=passed,
                tests=tests, summary=brief.tldr, sessions_spawned=pool.created_total,
                sessions_reaped=pool.reaped_total,
                kg_nodes=self.kg.num_nodes, kg_edges=self.kg.num_edges,
                memories=self._mem_writes, messages=len(self.bus.history),
                **usage_dict,
            )

            emit(Event("brief", brief.tldr, {
                "tldr": brief.tldr, "key_points": brief.key_points, "status": brief.status,
            }))
            emit(Event("done", "Run complete.", {
                "task_id": run.id, "passed": passed, "total": len(run.subtasks),
                "docs_dir": str(out_dir), "tests": tests, "over_budget": over_budget,
                **usage_dict, **self._metrics(),
            }))
            return run, brief, out_dir
        except asyncio.CancelledError:
            self.runs.set_status(run.id, "cancelled")
            raise

    async def aclose(self) -> None:
        await self.provider.aclose()
        self.memory.close()
        self.runs.close()

    # ---- internals ----
    def _recall_prior(self, prompt: str) -> str:
        """Pull relevant lessons from cross-run long-term memory for planning."""
        hits = self.memory.recall("longterm", prompt, top_k=5)
        return "\n".join(f"- {h.content}" for h in hits)

    def _consolidate_longterm(self, run: TaskRun, brief: BriefDoc) -> None:
        """Promote this run's takeaways into long-term memory for future runs."""
        tag = run.prompt[:80]
        try:
            self.memory.remember("longterm", f"[{tag}] {brief.tldr}", metadata={"task": run.id})
            for kp in brief.key_points[:5]:
                self.memory.remember("longterm", f"[{tag}] {kp}", metadata={"task": run.id})
        except Exception:
            pass

    def _workspace_files(self, run: TaskRun) -> list[str]:
        """Relative paths of files this run actually wrote — ground truth for the reviewer."""
        ws = self.settings.workspace_dir / run.id
        if not ws.is_dir():
            return []
        out = []
        for p in sorted(ws.rglob("*")):
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
                out.append(str(p.relative_to(ws)))
        return out[:200]

    def _metrics(self) -> dict[str, Any]:
        usage = getattr(self.provider, "usage", None)
        u = usage.to_dict() if usage else {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        return {
            "kg_nodes": self.kg.num_nodes,
            "kg_edges": self.kg.num_edges,
            "messages": len(self.bus.history),
            "memory": self._mem_writes,
            **u,
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

    def _make_execute(self, run: TaskRun, emit: EventFn):
        run_workspace = self.settings.workspace_dir / run.id

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
            agent = session.agent  # the BaseAgent for this session
            toolbox = ToolBox(
                ToolContext(
                    memory=self.memory, kb=self.kb, kg=self.kg, bus=self.bus,
                    agent_name=state.agent, task_scope=run.id, base_dir=Path.cwd(),
                    workspace=run_workspace, verify_timeout=self.settings.verify_timeout,
                )
            )
            criteria = "\n".join(f"- {c}" for c in state.spec.acceptance_criteria) or "- (none)"
            task_text = (
                f"{state.spec.title}\n\n{state.spec.description}\n\nAcceptance criteria:\n{criteria}"
            )
            ctx_parts = [f"Overall task: {run.prompt}", f"Plan summary: {run.plan.summary}"]
            for dep_id, dep_result in deps.items():
                ctx_parts.append(f"Result of dependency {dep_id}:\n{dep_result}")
            if state.verdict and not state.verdict.passed and state.verdict.suggestions:
                ctx_parts.append(
                    "Your previous attempt failed review. Fix these:\n"
                    + "\n".join(f"- {s}" for s in state.verdict.suggestions)
                )
            context = "\n\n".join(ctx_parts)

            def on_step(step: dict[str, Any], _id=state.id, _agent=state.agent) -> None:
                self._activity.setdefault(_id, []).append(step)
                emit(Event("agent_step", "", {"id": _id, "agent": _agent, **step}))

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
            await self._emit_new_messages(emit)
            return result

        return execute

    def _make_verify(self, run: TaskRun, emit: EventFn):
        async def verify(state: SubTaskState, result: str):
            files = self._workspace_files(run)
            verdict = await self.reviewer.verify(
                title=state.spec.title,
                description=state.spec.description,
                acceptance_criteria=state.spec.acceptance_criteria,
                result=result,
                workspace_files=files,
            )
            self.kg.add_fact(state.id, "review_status", "passed" if verdict.passed else "failed")
            result_text = result or ""
            if len(result_text) > 12000:
                result_text = result_text[:12000] + "\n… (truncated)"
            emit(Event("subtask_review",
                       f"{state.id} {'passed' if verdict.passed else 'failed'} (score {verdict.score})",
                       {
                           "id": state.id, "passed": verdict.passed, "score": verdict.score,
                           "attempts": state.attempts, "reasons": verdict.reasons,
                           "result": result_text,
                           **self._metrics(),
                       }))
            return verdict

        return verify

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
