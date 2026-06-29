"""FastAPI app: start a task, stream live events over a WebSocket, browse task docs.

The engine emits ``Event`` objects; a per-task Broker buffers them (so a client that
connects mid-run still gets the backlog) and fans them out to connected WebSockets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import projects
from ..agents.registry import build_agents
from ..config import Settings
from ..engine import Engine
from ..knowledge.graph import NetworkXKnowledgeGraph
from ..llm.errors import LLMError
from ..llm.schemas import Plan
from ..orchestration.events import Event
from ..orchestration.run_control import RunControl
from ..orchestration.run_store import RunStore, derive_title
from ..orchestration.task import new_task_id

_STATIC = Path(__file__).parent / "static"


class Broker:
    """Buffers a task's events and fans them out to subscribers."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._subs: set[asyncio.Queue] = set()
        self.done = False

    def publish(self, event: Event) -> None:
        payload = event.to_dict()
        self.events.append(payload)
        if event.type in ("done", "error"):
            self.done = True
        for q in self._subs:
            q.put_nowait(payload)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for past in self.events:  # replay backlog to late joiners
            q.put_nowait(past)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


# UI effort tier -> run knobs. Higher tiers raise the per-role reasoning effort
# (output_config.effort on the API backend), the per-agent turn budget, and review retries;
# low/medium also drop to a cheaper model. "high" reproduces the env defaults exactly.
_EFFORT: dict[str, dict[str, Any]] = {
    "low":    {"turns": 12, "retries": 0, "model": "claude-sonnet-4-6",
               "orch": "low",    "agent": "low",    "rev": "low"},
    "medium": {"turns": 24, "retries": 0, "model": "claude-sonnet-4-6",
               "orch": "medium", "agent": "medium", "rev": "medium"},
    "high":   {"turns": 40, "retries": 1, "model": None,
               "orch": "high",   "agent": "medium", "rev": "high"},
    "xhigh":  {"turns": 60, "retries": 2, "model": None,
               "orch": "xhigh",  "agent": "high",   "rev": "xhigh"},
    "max":    {"turns": 80, "retries": 2, "model": None,
               "orch": "max",    "agent": "max",    "rev": "max"},
}


def _settings_for(base: Settings, effort: str | None, budget: float | None,
                  project: str | None = None, memory_scope: str | None = None) -> Settings:
    overrides: dict[str, Any] = {}
    cfg = _EFFORT.get(effort or "")
    if cfg:
        overrides["agent_max_turns"] = cfg["turns"]
        overrides["max_retries"] = cfg["retries"]
        overrides["orchestrator_effort"] = cfg["orch"]
        overrides["agent_effort"] = cfg["agent"]
        overrides["reviewer_effort"] = cfg["rev"]
        if cfg["model"]:
            overrides["sdk_model"] = cfg["model"]  # cheaper than the default Opus
    if budget and budget > 0:
        overrides["budget_usd"] = budget
    if project:
        overrides["project"] = projects.resolve(base, project)
    if memory_scope in ("project", "global"):
        overrides["memory_scope"] = memory_scope
    return dataclasses.replace(base, **overrides) if overrides else base


class PlanRequest(BaseModel):
    prompt: str
    project: str | None = None
    memory_scope: str | None = None
    effort: str | None = None
    budget: float | None = None
    continue_from: str | None = None   # re-engage: plan as a continuation of this task


class RefinePlanRequest(BaseModel):
    prompt: str
    plan: dict[str, Any]          # the current (possibly hand-edited) plan
    instruction: str              # natural-language refinement, e.g. "add a security review"
    project: str | None = None
    memory_scope: str | None = None
    effort: str | None = None
    budget: float | None = None


class RunRequest(BaseModel):
    prompt: str
    plan: dict[str, Any] | None = None  # an approved/edited plan (skips re-planning)
    task_id: str | None = None
    effort: str | None = None
    budget: float | None = None
    title: str | None = None  # optional; auto-derived from the prompt when blank
    project: str | None = None
    memory_scope: str | None = None  # "project" | "global"
    continue_from: str | None = None  # re-engage: continue this completed task's workspace + context


class ProjectRequest(BaseModel):
    name: str


class ReorderRequest(BaseModel):
    order: list[str]


class QueueConfigRequest(BaseModel):
    concurrency: int


class SteerRequest(BaseModel):
    note: str


class FeedbackRequest(BaseModel):
    rating: int | None = None       # 1-5
    accepted: bool | None = None    # was the delivered work accepted?
    comment: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    app = FastAPI(title="AI Dev Assistant")
    app.state.settings = settings
    app.state.brokers = {}
    app.state.tasks = {}  # task_id -> asyncio.Task (for cancellation)
    app.state.runs = RunStore(settings.data_dir / "runs.db")
    app.state.runs.interrupt_orphans()  # clean up runs orphaned by a restart
    # task queue / scheduler state
    app.state.concurrency = max(1, settings.max_concurrent_runs)
    app.state.paused = False
    app.state.running = set()  # task_ids currently executing
    app.state.controls = {}  # task_id -> RunControl (in-run pause/steer)

    @app.middleware("http")
    async def no_cache(request, call_next):  # static assets should never be cached in dev
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    def _publish_queue_positions() -> None:
        """Notify each still-queued task's socket of its current position."""
        for tid, pos in app.state.runs.queue_positions().items():
            b = app.state.brokers.get(tid)
            if b is not None:
                b.publish(Event("queued", f"Queued · position {pos}",
                                {"position": pos, "status": "queued"}))

    def _start(task_id: str, payload: dict[str, Any]) -> None:
        if task_id not in app.state.brokers:  # e.g. resumed from disk after a restart
            app.state.brokers[task_id] = Broker()
        app.state.running.add(task_id)
        app.state.tasks[task_id] = asyncio.create_task(_run_task(
            task_id, payload.get("prompt", ""), payload.get("plan"),
            payload.get("effort"), payload.get("budget"), payload.get("title"),
            payload.get("project"), payload.get("memory_scope"), payload.get("continue_from")))

    def _pump() -> None:
        """Start queued tasks while slots are free (auto-run); respects Pause."""
        while not app.state.paused and len(app.state.running) < app.state.concurrency:
            entry = app.state.runs.queue_next()
            if entry is None:
                break
            _start(entry["task_id"], entry["payload"])
        _publish_queue_positions()

    async def _run_task(task_id: str, prompt: str, plan_dict: dict[str, Any] | None,
                        effort: str | None, budget: float | None, title: str | None = None,
                        project: str | None = None, memory_scope: str | None = None,
                        continue_from: str | None = None) -> None:
        broker: Broker = app.state.brokers[task_id]
        # record up front so cancels-during-planning persist (title auto-derived if blank)
        app.state.runs.start(task_id, prompt, title=(title or None))
        if continue_from:
            app.state.runs.set_parent(task_id, continue_from)
        engine = Engine(_settings_for(settings, effort, budget, project, memory_scope))
        control = RunControl()
        engine.control = control  # enables pause/resume/steer endpoints to reach this run
        app.state.controls[task_id] = control
        try:
            plan = Plan.model_validate(plan_dict) if plan_dict else None
            await engine.run(prompt, plan=plan, task_id=task_id, title=(title or None),
                             continue_from=continue_from, on_event=broker.publish)
        except asyncio.CancelledError:
            broker.publish(Event("error", "Run cancelled by user.", {"message": "cancelled"}))
            app.state.runs.set_status(task_id, "cancelled")
        except LLMError as exc:
            broker.publish(Event("error", f"Run failed: {exc}", {"message": str(exc)}))
            app.state.runs.set_status(task_id, "failed")  # don't strand it 'running'
        except Exception as exc:  # don't leave the socket hanging on unexpected failures
            broker.publish(Event("error", f"Unexpected error: {exc}", {"message": str(exc)}))
            app.state.runs.set_status(task_id, "failed")
        finally:
            await engine.aclose()
            app.state.tasks.pop(task_id, None)
            app.state.running.discard(task_id)
            app.state.controls.pop(task_id, None)
            if not broker.done:
                broker.publish(Event("done", "Run ended.", {}))
            _pump()  # a slot just freed — start the next queued task

    @app.post("/api/plan")
    async def make_plan(req: PlanRequest) -> JSONResponse:
        engine = Engine(_settings_for(settings, req.effort, req.budget, req.project, req.memory_scope))
        try:
            plan = await engine.make_plan(req.prompt, continue_from=req.continue_from)
        except LLMError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        finally:
            await engine.aclose()
        return JSONResponse({
            "plan_id": new_task_id(),
            "plan": {"title": plan.title, "summary": plan.summary,
                     "subtasks": [st.model_dump() for st in plan.subtasks]},
        })

    @app.post("/api/plan/refine")
    async def refine_plan(req: RefinePlanRequest) -> JSONResponse:
        if not (req.instruction or "").strip():
            return JSONResponse({"error": "instruction is required"}, status_code=400)
        engine = Engine(_settings_for(settings, req.effort, req.budget, req.project, req.memory_scope))
        try:
            current = Plan.model_validate(req.plan)
            plan = await engine.refine_plan(req.prompt, current, req.instruction)
        except LLMError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        finally:
            await engine.aclose()
        warning = ""
        try:  # surface a structural problem in the proposed plan without blocking the editor
            from ..orchestration.task import TaskRun
            TaskRun.from_plan(req.prompt, plan).validate()
        except Exception as exc:  # noqa: BLE001
            warning = str(exc)
        return JSONResponse({
            "plan_id": new_task_id(),
            "plan": {"title": plan.title, "summary": plan.summary,
                     "subtasks": [st.model_dump() for st in plan.subtasks]},
            "warning": warning,
        })

    @app.post("/api/run")
    async def start_run(req: RunRequest) -> dict[str, Any]:
        task_id = req.task_id or new_task_id()
        app.state.brokers[task_id] = Broker()
        app.state.brokers[task_id].publish(Event("status", "Backend: " + settings.llm_backend,
                                                  {"backend": settings.llm_backend}))
        payload = {
            "prompt": req.prompt, "plan": req.plan, "effort": req.effort, "budget": req.budget,
            "title": req.title, "project": req.project, "memory_scope": req.memory_scope,
            "continue_from": req.continue_from,
        }
        plan_title = (req.plan or {}).get("title") if req.plan else None
        app.state.runs.enqueue(task_id, req.prompt, req.title or plan_title, payload)
        _pump()  # auto-run if a slot is free
        status = "running" if task_id in app.state.running else "queued"
        position = app.state.runs.queue_positions().get(task_id)
        return {"task_id": task_id, "status": status, "position": position}

    @app.post("/api/run/{task_id}/cancel")
    async def cancel_run(task_id: str) -> JSONResponse:
        t = app.state.tasks.get(task_id)
        if t is None or t.done():
            return JSONResponse({"ok": False, "error": "no running task"}, status_code=404)
        t.cancel()
        return JSONResponse({"ok": True})

    # ---- in-run control (Tier 5): pause / resume / steer ----
    @app.post("/api/run/{task_id}/pause")
    async def pause_run(task_id: str) -> JSONResponse:
        ctrl: RunControl | None = app.state.controls.get(task_id)
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "no running task"}, status_code=404)
        ctrl.pause()
        b = app.state.brokers.get(task_id)
        if b:
            b.publish(Event("control", "Run paused — will halt before the next batch.", {"paused": True}))
        return JSONResponse({"ok": True, "paused": True})

    @app.post("/api/run/{task_id}/resume")
    async def resume_run(task_id: str) -> JSONResponse:
        ctrl: RunControl | None = app.state.controls.get(task_id)
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "no running task"}, status_code=404)
        ctrl.resume()
        b = app.state.brokers.get(task_id)
        if b:
            b.publish(Event("control", "Run resumed.", {"paused": False}))
        return JSONResponse({"ok": True, "paused": False})

    @app.post("/api/run/{task_id}/steer")
    async def steer_run(task_id: str, req: SteerRequest) -> JSONResponse:
        ctrl: RunControl | None = app.state.controls.get(task_id)
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "no running task"}, status_code=404)
        ctrl.steer(req.note)
        b = app.state.brokers.get(task_id)
        if b:
            b.publish(Event("control", f"Steering note queued: {req.note[:80]}", {"note": req.note}))
        return JSONResponse({"ok": True})

    # ---- human feedback (Tier 4 learning input) ----
    @app.post("/api/run/{task_id}/feedback")
    async def submit_feedback(task_id: str, req: FeedbackRequest) -> JSONResponse:
        app.state.runs.set_feedback(task_id, rating=req.rating, accepted=req.accepted,
                                    comment=req.comment or "")
        return JSONResponse({"ok": True})

    @app.get("/api/run/{task_id}/feedback")
    async def get_run_feedback(task_id: str) -> JSONResponse:
        return JSONResponse(app.state.runs.get_feedback(task_id) or {})

    # ---- observability (Tier 5): durable event log + trace + quality + stats ----
    @app.get("/api/tasks/{task_id}/events")
    async def get_events(task_id: str) -> JSONResponse:
        return JSONResponse(_read_jsonl(settings.docs_dir / task_id / "events.jsonl"))

    @app.get("/api/tasks/{task_id}/trace")
    async def get_trace(task_id: str) -> JSONResponse:
        return JSONResponse(_read_jsonl(settings.docs_dir / task_id / "trace.jsonl"))

    @app.get("/api/quality")
    async def get_quality() -> JSONResponse:
        return JSONResponse({
            "trend": app.state.runs.quality_trend(limit=30),
            "agents": app.state.runs.agent_track_record(),
        })

    @app.get("/api/stats")
    async def get_stats() -> JSONResponse:
        rows = app.state.runs.list(limit=500)
        total_cost = sum(float(r.get("cost_usd") or 0) for r in rows)
        scored = [r for r in rows if r.get("quality_score") is not None]
        avg_q = round(sum(float(r["quality_score"]) for r in scored) / len(scored), 1) if scored else None
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r.get("status") or "?"] = by_status.get(r.get("status") or "?", 0) + 1
        return JSONResponse({
            "runs": len(rows), "total_cost_usd": round(total_cost, 4),
            "avg_quality": avg_q, "by_status": by_status,
            "agents": app.state.runs.agent_track_record(),
        })

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = settings.data_dir.exists()
        return JSONResponse({"status": "ready" if ready else "starting", "backend": settings.llm_backend},
                            status_code=200 if ready else 503)

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str) -> JSONResponse:
        # stop it first if it's still running
        t = app.state.tasks.get(task_id)
        if t is not None and not t.done():
            t.cancel()
        app.state.tasks.pop(task_id, None)
        app.state.brokers.pop(task_id, None)
        app.state.running.discard(task_id)
        app.state.runs.queue_remove(task_id)  # drop it if still queued
        app.state.runs.delete(task_id)
        _pump()  # a slot may have freed
        # remove its docs + workspace artifacts (guarded against path traversal)
        for root in (settings.docs_dir, settings.workspace_dir):
            target = (root / task_id).resolve()
            if target.is_relative_to(root.resolve()) and target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
        # drop the task's line from the docs INDEX
        index = settings.docs_dir / "INDEX.md"
        if index.exists():
            kept = [ln for ln in index.read_text().splitlines() if task_id not in ln]
            index.write_text("\n".join(kept) + "\n")
        return JSONResponse({"ok": True})

    # ---- task queue control ----
    @app.get("/api/queue")
    async def get_queue() -> JSONResponse:
        pos = app.state.runs.queue_positions()
        pending = [
            {"id": p["task_id"],
             "title": p.get("title") or derive_title(p.get("prompt") or ""),
             "position": pos.get(p["task_id"])}
            for p in app.state.runs.queue_pending()
        ]
        running = []
        for tid in app.state.running:
            r = app.state.runs.get(tid) or {}
            running.append({"id": tid, "title": r.get("title") or derive_title(r.get("prompt") or "")})
        return JSONResponse({
            "concurrency": app.state.concurrency, "paused": app.state.paused,
            "running": running, "pending": pending,
        })

    @app.post("/api/queue/{task_id}/promote")
    async def promote_task(task_id: str) -> JSONResponse:
        app.state.runs.queue_promote(task_id)
        _pump()
        return JSONResponse({"ok": True})

    @app.post("/api/queue/reorder")
    async def reorder_queue(req: ReorderRequest) -> JSONResponse:
        app.state.runs.queue_reorder(req.order)
        _publish_queue_positions()
        return JSONResponse({"ok": True})

    @app.post("/api/queue/pause")
    async def pause_queue() -> JSONResponse:
        app.state.paused = True
        return JSONResponse({"ok": True, "paused": True})

    @app.post("/api/queue/resume")
    async def resume_queue() -> JSONResponse:
        app.state.paused = False
        _pump()
        return JSONResponse({"ok": True, "paused": False})

    @app.post("/api/queue/config")
    async def config_queue(req: QueueConfigRequest) -> JSONResponse:
        app.state.concurrency = max(1, int(req.concurrency))
        _pump()
        return JSONResponse({"ok": True, "concurrency": app.state.concurrency})

    @app.on_event("startup")
    async def _resume_queue() -> None:
        # persisted queue survives restarts — start pumping once the loop is up
        _pump()

    @app.websocket("/ws/{task_id}")
    async def ws(websocket: WebSocket, task_id: str) -> None:
        await websocket.accept()
        broker: Broker | None = app.state.brokers.get(task_id)
        if broker is None:
            await websocket.send_json({"type": "error", "message": "unknown task", "data": {}})
            await websocket.close()
            return
        q = broker.subscribe()
        try:
            while True:
                payload = await q.get()
                await websocket.send_json(payload)
                if payload["type"] in ("done", "error"):
                    break
        except WebSocketDisconnect:
            pass
        finally:
            broker.unsubscribe(q)

    @app.get("/api/tasks")
    async def list_tasks() -> JSONResponse:
        rows = app.state.runs.list(limit=100)
        if rows:
            return JSONResponse([{
                "id": r["id"], "title": r.get("title") or derive_title(r.get("prompt") or ""),
                "prompt": r.get("prompt") or "",
                "tldr": r.get("summary") or "", "status": r.get("status"),
                "tests": r.get("tests"), "cost_usd": r.get("cost_usd"),
                "passed": r.get("subtasks_passed"), "total": r.get("subtasks_total"),
                "quality_score": r.get("quality_score"), "run_status": r.get("run_status"),
                "parent_id": r.get("parent_id"),
            } for r in rows])
        # Fallback: docs dirs from runs that predate the run store.
        docs = settings.docs_dir
        items = []
        if docs.exists():
            for d in sorted(docs.iterdir(), reverse=True):
                brief = d / "brief.md"
                if d.is_dir() and brief.is_file():
                    items.append({"id": d.name, "tldr": _first_tldr(brief)})
        return JSONResponse(items)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> JSONResponse:
        d = settings.docs_dir / task_id
        if not d.is_dir():
            return JSONResponse({"error": "not found"}, status_code=404)
        activity_raw = _read(d / "activity.json")
        try:
            activity = json.loads(activity_raw) if activity_raw else {}
        except json.JSONDecodeError:
            activity = {}
        r = app.state.runs.get(task_id) or {}
        meta = {
            "status": r.get("status"), "title": r.get("title"), "prompt": r.get("prompt"),
            "passed": r.get("subtasks_passed"), "total": r.get("subtasks_total"),
            "tests": r.get("tests"), "cost_usd": r.get("cost_usd"),
            "input_tokens": r.get("input_tokens"), "output_tokens": r.get("output_tokens"),
            "sessions_spawned": r.get("sessions_spawned"), "sessions_reaped": r.get("sessions_reaped"),
            "kg_nodes": r.get("kg_nodes"), "kg_edges": r.get("kg_edges"),
            "memories": r.get("memories"), "messages": r.get("messages"),
            "quality_score": r.get("quality_score"), "run_status": r.get("run_status"),
            "parent_id": r.get("parent_id"),
        }
        return JSONResponse({
            "plan": _read(d / "plan.md"),
            "report": _read(d / "report.md"),
            "brief": _read(d / "brief.md"),
            "activity": activity,
            "meta": meta,
            "feedback": app.state.runs.get_feedback(task_id) or {},
        })

    @app.get("/api/agents")
    async def list_agents() -> JSONResponse:
        agents = build_agents(settings)
        return JSONResponse([
            {
                "name": a.name,
                "description": a.profile.description,
                "when_to_use": a.profile.when_to_use,
                "tools": a.profile.tools,
            }
            for a in agents.values()
        ])

    @app.get("/api/projects")
    async def get_projects() -> JSONResponse:
        return JSONResponse(projects.list_projects(settings))

    @app.post("/api/projects")
    async def add_project(req: ProjectRequest) -> JSONResponse:
        if not (req.name or "").strip():
            return JSONResponse({"error": "name required"}, status_code=400)
        return JSONResponse(projects.create_project(settings, req.name))

    @app.get("/api/graph")
    async def get_graph(project: str | None = None) -> JSONResponse:
        # The knowledge graph is project-scoped only.
        s = dataclasses.replace(settings, project=projects.resolve(settings, project))
        kg = NetworkXKnowledgeGraph(s.graph_path)
        nodes = [{"id": n, "type": t} for n, t in kg.node_types().items()]
        edges = [
            {"source": tr.subject, "target": tr.object, "relation": tr.relation}
            for tr in kg.all_triples()
        ]
        return JSONResponse({"project": s.project, "nodes": nodes, "edges": edges})

    def _read_memory(path: Path, mem_scope: str) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, scope, key, content, metadata, created_at "
                "FROM memory ORDER BY id DESC LIMIT 200"
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            meta = json.loads(r["metadata"] or "{}")
            out.append({
                "id": r["id"], "scope": r["scope"], "content": r["content"],
                "author": meta.get("author", ""), "subtask": meta.get("subtask", ""),
                "created_at": r["created_at"], "mem_scope": mem_scope,
            })
        return out

    @app.get("/api/memory")
    async def get_memory(project: str | None = None) -> JSONResponse:
        # Show the project's memories plus the shared global memories.
        s = dataclasses.replace(settings, project=projects.resolve(settings, project))
        out = _read_memory(s.db_path, "project") + _read_memory(s.global_db_path, "global")
        out.sort(key=lambda m: m["created_at"], reverse=True)
        return JSONResponse(out[:300])

    @app.get("/api/workspace")
    async def list_workspace(task: str | None = None) -> JSONResponse:
        ws = settings.workspace_dir.resolve()
        root = (ws / task).resolve() if task else ws
        if not root.is_relative_to(ws) or not root.exists():
            return JSONResponse([])
        items = []
        for p in sorted(root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                items.append({"path": str(p.relative_to(root)), "size": p.stat().st_size})
        return JSONResponse(items)

    @app.get("/api/workspace/file")
    async def workspace_file(path: str, task: str | None = None) -> JSONResponse:
        ws = settings.workspace_dir.resolve()
        root = (ws / task).resolve() if task else ws
        if not root.is_relative_to(ws):
            return JSONResponse({"error": "not found"}, status_code=404)
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        data = target.read_text(errors="replace")
        return JSONResponse({"path": path, "content": data[:200_000], "truncated": len(data) > 200_000})

    @app.get("/api/workspace/download")
    async def workspace_download(path: str, task: str | None = None):
        ws = settings.workspace_dir.resolve()
        root = (ws / task).resolve() if task else ws
        target = (root / path).resolve()
        if not root.is_relative_to(ws) or not target.is_relative_to(root) or not target.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(target), filename=target.name, media_type="application/octet-stream")

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(_STATIC / "index.html"))

    return app


def _read(path: Path) -> str:
    return path.read_text() if path.is_file() else ""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _first_tldr(brief: Path) -> str:
    lines = brief.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("## TL;DR"):
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    return nxt.strip()
    return ""


app = create_app()
