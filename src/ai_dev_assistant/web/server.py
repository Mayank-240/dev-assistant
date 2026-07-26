"""FastAPI app: start a task, stream live events over a WebSocket, browse task docs.

The engine emits ``Event`` objects; a per-task Broker buffers them (so a client that
connects mid-run still gets the backlog) and fans them out to connected WebSockets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import ipaddress
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import projects, vcs
from ..agents.registry import build_agents
from ..config import Settings
from ..engine import Engine
from ..knowledge import combine
from ..knowledge.graph import NetworkXKnowledgeGraph
from ..llm.errors import LLMError
from ..llm.schemas import Plan
from ..orchestration.events import Event
from ..orchestration.run_control import RunControl
from ..orchestration.run_store import RunStore, derive_title
from ..orchestration.task import new_task_id

_STATIC = Path(__file__).parent / "static"

# Run statuses that mean "this run is over" (mirrors the run store's terminal set) and
# the subset a user may retry/resume from (W5).
_TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled", "over_budget", "interrupted"}
_RESUMABLE_STATUSES = {"interrupted", "failed", "over_budget", "cancelled", "partial"}


def _engine_supports_resume() -> bool:
    """True once Engine.run has grown a ``resume`` kwarg (R1 lands separately).
    Guarded with try/except TypeError so this module works against either signature."""
    try:
        inspect.signature(Engine.run).bind_partial(None, "prompt", resume=True)
        return True
    except TypeError:
        return False


def _fanout_runner():
    """orchestration.fanout.run_cross_project once it lands (F3, concurrent change);
    None until then so the fan-out paths answer 501 instead of crashing."""
    try:
        from ..orchestration.fanout import run_cross_project
    except ImportError:
        return None
    return run_cross_project


# Pseudo-project slug carried by cross-project parent run rows (fanout contract).
_MULTI_PROJECT = "multi"


def _is_loopback(host: str) -> bool:
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


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

# UI model choice -> concrete model ids. Chosen independently of effort (the two are
# separate controls in the composer); an explicit choice overrides the effort preset's
# cheaper-model default.
_MODELS: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def _settings_for(base: Settings, effort: str | None, budget: float | None,
                  project: str | None = None, memory_scope: str | None = None,
                  git_finalize: bool | None = None, model: str | None = None) -> Settings:
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
    mid = _MODELS.get((model or "").lower())
    if mid:  # explicit model choice wins over the effort preset's model default
        overrides["sdk_model"] = mid
        overrides["agent_model"] = mid
        overrides["orchestrator_model"] = mid
    if budget and budget > 0:
        overrides["budget_usd"] = budget
    if project:
        overrides["project"] = projects.resolve(base, project)
    if memory_scope in ("project", "global"):
        overrides["memory_scope"] = memory_scope
    # Per-run repo binding is gone (clean break) — a run targets a *project*, and the
    # project owns the repo. git_finalize remains a per-run knob.
    if git_finalize is not None:
        overrides["git_finalize"] = bool(git_finalize)
    resolved = dataclasses.replace(base, **overrides) if overrides else base
    # Apply the project's policy (F7): forces per-subtask worktrees ON for project
    # tasks (per-subtask review depends on their commits) and applies policy
    # budget/effort/git_mode/protected_paths under the request's explicit overrides.
    try:
        resolved = projects.effective_settings(resolved)
        if overrides:  # the request's explicit choices beat policy defaults
            resolved = dataclasses.replace(resolved, **overrides)
    except Exception:
        pass
    return resolved


class PlanRequest(BaseModel):
    prompt: str
    project: str | None = None
    memory_scope: str | None = None
    effort: str | None = None
    model: str | None = None   # "opus" | "sonnet" | "haiku" (independent of effort)
    budget: float | None = None
    continue_from: str | None = None   # re-engage: plan as a continuation of this task


class RefinePlanRequest(BaseModel):
    prompt: str
    plan: dict[str, Any]          # the current (possibly hand-edited) plan
    instruction: str              # natural-language refinement, e.g. "add a security review"
    project: str | None = None
    memory_scope: str | None = None
    effort: str | None = None
    model: str | None = None
    budget: float | None = None


class RunRequest(BaseModel):
    prompt: str
    plan: dict[str, Any] | None = None  # an approved/edited plan (skips re-planning)
    task_id: str | None = None
    effort: str | None = None
    model: str | None = None   # "opus" | "sonnet" | "haiku" (independent of effort)
    budget: float | None = None
    title: str | None = None  # optional; auto-derived from the prompt when blank
    project: str | None = None
    # F3: cross-project fan-out — 2+ slugs launch one child task per project with a
    # rolled-up parent; a single entry behaves exactly like `project`.
    projects: list[str] | None = None
    stagger: bool = False  # run the first child alone so its lessons inform the rest
    memory_scope: str | None = None  # "project" | "global"
    continue_from: str | None = None  # re-engage: continue this completed task's workspace + context
    # Clean break: repo_path/repo_url/repo_ref are gone — the run's *project* owns the repo.
    git_finalize: bool | None = None  # commit the workspace to a new branch at the end


class ProjectRequest(BaseModel):
    name: str


class ProjectImportRequest(BaseModel):
    source: str            # local path or git URL
    name: str | None = None
    ref: str | None = None  # branch / tag / sha (git-URL imports)


class ProjectPatchRequest(BaseModel):
    archived: bool | None = None
    policy: dict[str, Any] | None = None


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


class SubtaskRejectRequest(BaseModel):
    comment: str | None = None      # why the subtask's work was rejected (feeds learning)


def create_app(settings: Settings | None = None, host: str | None = None,
               api_token: str | None = None) -> FastAPI:
    settings = settings or Settings.load()
    app = FastAPI(title="AI Dev Assistant")
    app.state.settings = settings
    app.state.brokers = {}

    # ---- S8: bearer-token auth. Token from ADA_API_TOKEN; when binding a non-loopback
    # host with no token configured, auto-generate one so a 0.0.0.0 bind (e.g. Docker)
    # is never open by default. Loopback with no token keeps auth off, as before.
    bind_host = (host or os.getenv("ADA_BIND_HOST") or "127.0.0.1").strip()
    token = api_token if api_token is not None else os.getenv("ADA_API_TOKEN", "").strip()
    if not token and not _is_loopback(bind_host):
        token = secrets.token_urlsafe(32)
        print(f"[ai-dev-assistant] Binding {bind_host} with no ADA_API_TOKEN set — "
              "auto-generated an API token (send it as 'Authorization: Bearer <token>', "
              "or '?token=<token>' on WebSockets/downloads):", flush=True)
        print(f"[ai-dev-assistant] API token: {token}", flush=True)
    app.state.api_token = token
    broker_grace = _env_float("ADA_BROKER_GRACE_SECONDS", 60.0)

    def _authorized(request) -> bool:
        if not app.state.api_token:
            return True
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), app.state.api_token):
            return True
        supplied = request.query_params.get("token", "")
        return bool(supplied) and secrets.compare_digest(supplied, app.state.api_token)

    @app.middleware("http")
    async def require_token(request, call_next):
        # /healthz, /readyz, / and /static stay open; every /api/* route needs the token.
        if request.url.path.startswith("/api/") and not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # Explicit CORS policy: same-origin by default (no cross-origin allowed);
    # widen with a comma-separated ADA_CORS_ORIGINS.
    cors_origins = [o.strip() for o in os.getenv("ADA_CORS_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins,
                       allow_methods=["*"], allow_headers=["*"])
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
        slugs = payload.get("projects") or []
        if len(slugs) >= 2:  # F3: cross-project fan-out parent
            app.state.tasks[task_id] = asyncio.create_task(_run_fanout(
                task_id, payload.get("prompt", ""), list(slugs),
                payload.get("effort"), payload.get("budget"), payload.get("title"),
                bool(payload.get("stagger")), model=payload.get("model")))
            return
        app.state.tasks[task_id] = asyncio.create_task(_run_task(
            task_id, payload.get("prompt", ""), payload.get("plan"),
            payload.get("effort"), payload.get("budget"), payload.get("title"),
            payload.get("project"), payload.get("memory_scope"), payload.get("continue_from"),
            payload.get("git_finalize"), bool(payload.get("resume")),
            model=payload.get("model")))

    def _evict_broker_later(task_id: str, broker: Broker) -> None:
        """W4: drop a finished task's broker after a grace period so RAM doesn't hold
        every event forever — late joiners then replay history from events.jsonl."""
        def _evict() -> None:
            if app.state.brokers.get(task_id) is broker:
                app.state.brokers.pop(task_id, None)
        try:
            asyncio.get_running_loop().call_later(broker_grace, _evict)
        except RuntimeError:  # no running loop (shouldn't happen in-server)
            pass

    def _pump() -> None:
        """Start queued tasks while slots are free (auto-run); respects Pause."""
        while not app.state.paused and len(app.state.running) < app.state.concurrency:
            entry = app.state.runs.queue_next()
            if entry is None:
                break
            if entry["task_id"] in app.state.running:
                # R5: over-subscription guard — never start a second copy of a running task
                # (e.g. a stale queue entry surviving a restart while the run is live again).
                print(f"[ai-dev-assistant] queue: skipping {entry['task_id']} — already running",
                      flush=True)
                continue
            _start(entry["task_id"], entry["payload"])
        _publish_queue_positions()

    def _sanitize_queue() -> None:
        """R5 startup sanity: a restart rebuilds state from disk — drop queue entries whose
        run row already reached a terminal status, so finished work is never re-run."""
        for p in app.state.runs.queue_pending():
            status = (app.state.runs.get(p["task_id"]) or {}).get("status")
            if status in _TERMINAL_STATUSES:
                print(f"[ai-dev-assistant] queue: dropping {p['task_id']} — "
                      f"run already {status}", flush=True)
                app.state.runs.queue_remove(p["task_id"])

    async def _run_task(task_id: str, prompt: str, plan_dict: dict[str, Any] | None,
                        effort: str | None, budget: float | None, title: str | None = None,
                        project: str | None = None, memory_scope: str | None = None,
                        continue_from: str | None = None,
                        git_finalize: bool | None = None, resume: bool = False,
                        model: str | None = None) -> None:
        broker: Broker = app.state.brokers[task_id]
        # record up front so cancels-during-planning persist (title auto-derived if blank);
        # the run row carries its project so activity/history can be filtered per project.
        try:
            app.state.runs.start(task_id, prompt, title=(title or None),
                                 project=projects.resolve(settings, project))
        except TypeError:  # run store without the project column (landing separately)
            app.state.runs.start(task_id, prompt, title=(title or None))
        if continue_from:
            app.state.runs.set_parent(task_id, continue_from)
        engine = Engine(_settings_for(settings, effort, budget, project, memory_scope,
                                      git_finalize, model=model))
        control = RunControl()
        engine.control = control  # enables pause/resume/steer endpoints to reach this run
        app.state.controls[task_id] = control
        # W5/R1: resume rides through **kwargs so this compiles against Engine.run whether
        # or not the checkpoint-resume kwarg has landed; the TypeError guard below fails soft.
        extra: dict[str, Any] = {"resume": True} if resume else {}
        try:
            plan = Plan.model_validate(plan_dict) if plan_dict else None
            await engine.run(prompt, plan=plan, task_id=task_id, title=(title or None),
                             continue_from=continue_from, on_event=broker.publish, **extra)
        except TypeError as exc:
            msg = "resume not available yet" if resume else str(exc)
            broker.publish(Event("error", f"Run failed: {msg}", {"message": msg}))
            app.state.runs.set_status(task_id, "failed")
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
            _evict_broker_later(task_id, broker)
            _pump()  # a slot just freed — start the next queued task

    async def _run_fanout(task_id: str, prompt: str, slugs: list[str],
                          effort: str | None, budget: float | None,
                          title: str | None = None, stagger: bool = False,
                          model: str | None = None) -> None:
        """F3: cross-project fan-out parent. run_cross_project owns the run rows
        (parent project='multi', children with parent_id) and emits plan/child_start/
        child_done/brief/done on the parent stream — we just wire it into the Broker."""
        broker: Broker = app.state.brokers[task_id]
        fn = _fanout_runner()
        try:
            if fn is None:  # queued before the fan-out core landed (e.g. across a restart)
                msg = "cross-project fan-out is not available yet"
                broker.publish(Event("error", msg, {"message": msg}))
                app.state.runs.set_status(task_id, "failed")
                return
            await fn(_settings_for(settings, effort, budget, model=model), prompt, slugs,
                     title=(title or None), stagger=bool(stagger), task_id=task_id,
                     on_event=broker.publish)
        except asyncio.CancelledError:
            broker.publish(Event("error", "Run cancelled by user.", {"message": "cancelled"}))
            app.state.runs.set_status(task_id, "cancelled")
        except LLMError as exc:
            broker.publish(Event("error", f"Run failed: {exc}", {"message": str(exc)}))
            app.state.runs.set_status(task_id, "failed")
        except Exception as exc:  # don't leave the socket hanging on unexpected failures
            broker.publish(Event("error", f"Unexpected error: {exc}", {"message": str(exc)}))
            app.state.runs.set_status(task_id, "failed")
        finally:
            app.state.tasks.pop(task_id, None)
            app.state.running.discard(task_id)
            if not broker.done:
                broker.publish(Event("done", "Run ended.", {}))
            _evict_broker_later(task_id, broker)
            _pump()  # a slot just freed — start the next queued task

    @app.post("/api/plan")
    async def make_plan(req: PlanRequest) -> JSONResponse:
        engine = Engine(_settings_for(settings, req.effort, req.budget, req.project,
                                      req.memory_scope, model=req.model))
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
        engine = Engine(_settings_for(settings, req.effort, req.budget, req.project,
                                      req.memory_scope, model=req.model))
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
    async def start_run(req: RunRequest):
        # F3: `projects` with 2+ entries fans the task out; 1 entry behaves as `project`.
        slugs: list[str] = []
        for s in (req.projects or []):
            s = (s or "").strip()
            if s and s not in slugs:  # dedupe, keep order
                slugs.append(s)
        project = req.project
        if slugs:
            unknown = [s for s in slugs if not _project_known(s)]
            if unknown:
                return JSONResponse({"error": "unknown project(s): " + ", ".join(unknown)},
                                    status_code=400)
            if len(slugs) == 1:
                project, slugs = slugs[0], []
        if slugs and _fanout_runner() is None:
            return JSONResponse({"error": "cross-project fan-out is not available yet"},
                                status_code=501)
        task_id = req.task_id or new_task_id()
        app.state.brokers[task_id] = Broker()
        app.state.brokers[task_id].publish(Event("status", "Backend: " + settings.llm_backend,
                                                  {"backend": settings.llm_backend}))
        if slugs:
            payload: dict[str, Any] = {
                "prompt": req.prompt, "projects": slugs, "stagger": bool(req.stagger),
                "effort": req.effort, "model": req.model, "budget": req.budget, "title": req.title,
            }
        else:
            payload = {
                "prompt": req.prompt, "plan": req.plan, "effort": req.effort,
                "model": req.model, "budget": req.budget, "title": req.title, "project": project,
                "memory_scope": req.memory_scope, "continue_from": req.continue_from,
                "git_finalize": req.git_finalize,
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
        if t is not None and not t.done():
            t.cancel()
            return JSONResponse({"ok": True})
        # Additive: the task may still be waiting in the queue — cancelling there
        # removes the entry (so the pump can never start it) and marks the run
        # cancelled, mirroring what a cancel-while-running ends up recording.
        if any(p["task_id"] == task_id for p in app.state.runs.queue_pending()):
            app.state.runs.queue_remove(task_id)
            app.state.runs.set_status(task_id, "cancelled")
            b = app.state.brokers.get(task_id)
            if b is not None:
                b.publish(Event("error", "Run cancelled by user.", {"message": "cancelled"}))
            _publish_queue_positions()
            return JSONResponse({"ok": True, "was": "queued"})
        return JSONResponse({"ok": False, "error": "no running task"}, status_code=404)

    # ---- W5: retry/resume a stopped run ----
    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> JSONResponse:
        row = app.state.runs.get(task_id)
        if row is None:
            return JSONResponse({"error": "unknown task"}, status_code=404)
        status = row.get("status") or ""
        if task_id in app.state.running or status in ("running", "queued"):
            return JSONResponse({"error": f"task is already {status or 'active'}"},
                                status_code=409)
        if status not in _RESUMABLE_STATUSES:
            return JSONResponse(
                {"error": f"cannot resume a run with status '{status}'"}, status_code=409)
        if (row.get("project") or "") == _MULTI_PROJECT:
            return JSONResponse(
                {"error": "resuming a cross-project task is not supported yet"},
                status_code=501)
        if not _engine_supports_resume():
            return JSONResponse({"error": "resume not available yet"}, status_code=501)
        if task_id not in app.state.brokers:
            app.state.brokers[task_id] = Broker()
        app.state.brokers[task_id].publish(Event("status", "Backend: " + settings.llm_backend,
                                                 {"backend": settings.llm_backend}))
        payload = {
            "prompt": row.get("prompt") or "", "plan": None, "effort": None, "budget": None,
            "title": row.get("title"), "project": row.get("project"), "memory_scope": None,
            "continue_from": None, "git_finalize": None, "resume": True,
        }
        app.state.runs.enqueue(task_id, payload["prompt"], row.get("title"), payload)
        _pump()
        now = "running" if task_id in app.state.running else "queued"
        return JSONResponse({"task_id": task_id, "status": now,
                             "position": app.state.runs.queue_positions().get(task_id)})

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

    # ---- W5: diff viewer — what the run actually changed in its workspace ----
    @app.get("/api/tasks/{task_id}/diff")
    async def get_diff(task_id: str) -> JSONResponse:
        empty = {"is_git": False, "status": "", "diff": "", "truncated": False}
        ws_root = settings.workspace_dir.resolve()
        ws = (ws_root / task_id).resolve()
        if not ws.is_relative_to(ws_root) or not (ws / ".git").exists():
            return JSONResponse(empty)

        def _git(*args: str) -> tuple[int, str]:
            try:
                r = subprocess.run(["git", "-C", str(ws), *args],
                                   capture_output=True, text=True, timeout=30)
                return r.returncode, (r.stdout if r.returncode == 0 else (r.stdout or r.stderr))
            except (OSError, subprocess.SubprocessError) as exc:
                return 1, f"(git failed: {exc})"

        _, status_out = await asyncio.to_thread(_git, "status", "--porcelain")
        head_rc, _out = await asyncio.to_thread(_git, "rev-parse", "--verify", "HEAD")
        if head_rc == 0:
            _, diff_out = await asyncio.to_thread(_git, "diff", "HEAD")
        else:
            diff_out = ""  # no commits yet — status still shows what exists
        truncated = len(diff_out) > 200_000
        return JSONResponse({"is_git": True, "status": status_out[:20_000],
                             "diff": diff_out[:200_000], "truncated": truncated})

    # ---- W5: run comparison — two runs' stored metrics, side by side ----
    def _compare_fields(r: dict[str, Any]) -> dict[str, Any]:
        duration = None
        if r.get("created_at") and r.get("ended_at"):
            duration = round(float(r["ended_at"]) - float(r["created_at"]), 1)
        return {
            "id": r.get("id"), "title": r.get("title"), "status": r.get("status"),
            "run_status": r.get("run_status"), "quality_score": r.get("quality_score"),
            "subtasks_passed": r.get("subtasks_passed"), "subtasks_total": r.get("subtasks_total"),
            "tests": r.get("tests"), "cost_usd": r.get("cost_usd"),
            "input_tokens": r.get("input_tokens"), "output_tokens": r.get("output_tokens"),
            "duration_s": duration,
            "sessions_spawned": r.get("sessions_spawned"),
            "sessions_reaped": r.get("sessions_reaped"),
        }

    @app.get("/api/runs/compare")
    async def compare_runs(a: str, b: str) -> JSONResponse:
        ra, rb = app.state.runs.get(a), app.state.runs.get(b)
        missing = [rid for rid, row in ((a, ra), (b, rb)) if row is None]
        if missing:
            return JSONResponse({"error": "unknown run(s): " + ", ".join(missing)},
                                status_code=404)
        return JSONResponse({"a": _compare_fields(ra), "b": _compare_fields(rb)})

    # ---- W5: safe, non-secret config for the UI (feeds the cost-vs-budget meter) ----
    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        # Whitelisted fields only — never API keys, tokens, or paths.
        return JSONResponse({
            "budget_usd": settings.budget_usd,
            "llm_backend": settings.llm_backend,
            "max_concurrent_runs": settings.max_concurrent_runs,
            "sdk_model": settings.sdk_model,
        })

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
        # persisted queue survives restarts — sanity-check it, then start pumping
        _sanitize_queue()
        _pump()

    @app.websocket("/ws/{task_id}")
    async def ws(websocket: WebSocket, task_id: str) -> None:
        # S8: WebSockets can't send an Authorization header from the browser — accept
        # the token as a query param instead. Reject before the handshake completes.
        if app.state.api_token:
            supplied = websocket.query_params.get("token", "")
            if not (supplied and secrets.compare_digest(supplied, app.state.api_token)):
                await websocket.close(code=4401)
                return
        await websocket.accept()
        broker: Broker | None = app.state.brokers.get(task_id)
        if broker is None:
            # W4: the broker may have been evicted after the run finished — replay the
            # durable event log instead of holding history in RAM forever.
            backlog = _read_jsonl(settings.docs_dir / task_id / "events.jsonl")
            if backlog:
                try:
                    for past in backlog:
                        await websocket.send_json(past)
                except WebSocketDisconnect:
                    pass
                await websocket.close()
                return
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
                "project": r.get("project"),  # lets the UI scope recent tasks per project
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
            "project": r.get("project"),  # "multi" marks a cross-project parent (F3)
        }
        return JSONResponse({
            "plan": _read(d / "plan.md"),
            "report": _read(d / "report.md"),
            "brief": _read(d / "brief.md"),
            "activity": activity,
            "meta": meta,
            "feedback": app.state.runs.get_feedback(task_id) or {},
        })

    # ---- F3/F6: children of a cross-project parent (feeds the UI's children grid) ----
    @app.get("/api/tasks/{task_id}/children")
    async def get_children(task_id: str) -> JSONResponse:
        if not hasattr(app.state.runs, "children_of"):  # store variant landing separately
            return JSONResponse({"error": "cross-project children are not available yet"},
                                status_code=501)
        rows = app.state.runs.children_of(task_id) or []
        return JSONResponse({"children": [{
            "id": r.get("id"),
            "slug": r.get("project"), "project": r.get("project"),
            "title": r.get("title") or derive_title(r.get("prompt") or ""),
            "status": r.get("status"), "run_status": r.get("run_status"),
            "quality_score": r.get("quality_score"), "cost_usd": r.get("cost_usd"),
            "task_branch": r.get("task_branch"), "review_target": r.get("review_target"),
        } for r in rows]})

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

    # ---- F1/F6: project lifecycle (import / status / activity / patch / delete) ----
    # projects.py is growing import_project/project_status/set_policy/archive_project/
    # delete_project in a parallel change; every call is guarded with AttributeError -> 501
    # so this module works against either version.

    def _project_known(slug: str) -> bool:
        return any(p.get("slug") == slug for p in projects.list_projects(settings))

    def _title_of(row: dict[str, Any]) -> str:
        return row.get("title") or derive_title(row.get("prompt") or "")

    def _project_of_run(row: dict[str, Any] | None) -> str:
        return (row or {}).get("project") or "default"

    def _running_ids_for(slug: str) -> list[str]:
        return [tid for tid in sorted(app.state.running)
                if _project_of_run(app.state.runs.get(tid)) == slug]

    @app.post("/api/projects/import")
    async def import_project(req: ProjectImportRequest) -> JSONResponse:
        source = (req.source or "").strip()
        if not source:
            return JSONResponse({"error": "source (path or git URL) is required"}, status_code=400)
        try:
            entry = await asyncio.to_thread(
                projects.import_project, settings, source,
                name=(req.name or "").strip() or None, ref=(req.ref or "").strip())
        except AttributeError:
            return JSONResponse({"error": "project import is not available yet"}, status_code=501)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(entry)

    @app.get("/api/projects/{slug}/status")
    async def get_project_status(slug: str) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        try:
            st = await asyncio.to_thread(projects.project_status, settings, slug)
        except AttributeError:
            return JSONResponse({"error": "project status is not available yet"}, status_code=501)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse(st)

    @app.get("/api/projects/{slug}/activity")
    async def get_project_activity(slug: str) -> JSONResponse:
        """Live view of one project: running tasks + queued tasks + last 5 run rows.
        The pseudo-slug 'multi' aggregates cross-project fan-out parents (F3)."""
        if slug != _MULTI_PROJECT and not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        running = []
        for tid in _running_ids_for(slug):
            r = app.state.runs.get(tid) or {}
            running.append({"id": tid, "title": _title_of(r)})
        positions = app.state.runs.queue_positions()
        queued = []
        for p in app.state.runs.queue_pending():
            payload = p.get("payload") or {}
            if isinstance(payload, str):  # queue_pending stores payload as a JSON string
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            if len(payload.get("projects") or []) >= 2:  # queued fan-out parent
                p_slug = _MULTI_PROJECT
            else:
                p_slug = projects.resolve(settings, payload.get("project"))
            if p_slug != slug:
                continue
            queued.append({"id": p["task_id"], "title": _title_of(p),
                           "position": positions.get(p["task_id"])})
        try:
            recent = app.state.runs.list(limit=5, project=slug)
        except TypeError:  # run store without per-project filtering (landing separately)
            recent = [r for r in app.state.runs.list(limit=100)
                      if _project_of_run(r) == slug][:5]
        recent = [{
            "id": r.get("id"), "title": _title_of(r), "status": r.get("status"),
            "run_status": r.get("run_status"), "quality_score": r.get("quality_score"),
            "cost_usd": r.get("cost_usd"), "tests": r.get("tests"),
            "created_at": r.get("created_at"), "ended_at": r.get("ended_at"),
        } for r in recent]
        return JSONResponse({"slug": slug, "running": running, "queued": queued,
                             "recent": recent})

    @app.patch("/api/projects/{slug}")
    async def patch_project(slug: str, req: ProjectPatchRequest) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        try:
            if req.archived is not None:
                try:
                    projects.archive_project(settings, slug, archived=bool(req.archived))
                except TypeError:  # older archive-only signature
                    projects.archive_project(settings, slug)
            if req.policy is not None:
                projects.set_policy(settings, slug, req.policy)
        except AttributeError:
            return JSONResponse({"error": "project archive/policy is not available yet"},
                                status_code=501)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            entry = projects.get_project(settings, slug)
        except AttributeError:
            entry = next((p for p in projects.list_projects(settings)
                          if p.get("slug") == slug), None)
        return JSONResponse(entry or {"ok": True})

    @app.delete("/api/projects/{slug}")
    async def remove_project(slug: str) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        if slug == projects.DEFAULT_PROJECT:
            return JSONResponse({"error": "the default project cannot be deleted"},
                                status_code=409)
        active = _running_ids_for(slug)
        if active:
            return JSONResponse(
                {"error": "project has running task(s): " + ", ".join(active)},
                status_code=409)
        try:
            await asyncio.to_thread(projects.delete_project, settings, slug)
        except AttributeError:
            return JSONResponse({"error": "project delete is not available yet"}, status_code=501)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    # ---- F4: project home — recent runs (feeds the quality-trend sparkline) ----
    @app.get("/api/projects/{slug}/runs")
    async def get_project_runs(slug: str, limit: int = 30) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        limit = max(1, min(int(limit), 100))
        try:
            rows = app.state.runs.list(limit=limit, project=slug)
        except TypeError:  # run store without per-project filtering (landing separately)
            rows = [r for r in app.state.runs.list(limit=500)
                    if _project_of_run(r) == slug][:limit]
        return JSONResponse([{
            "id": r.get("id"), "title": _title_of(r), "status": r.get("status"),
            "run_status": r.get("run_status"), "quality_score": r.get("quality_score"),
            "cost_usd": r.get("cost_usd"), "tests": r.get("tests"),
            "created_at": r.get("created_at"), "ended_at": r.get("ended_at"),
        } for r in rows])

    # ---- F4 decision #5: Reviews & Permissions panel + per-subtask accept/reject ----
    # Several inputs land concurrently (run-store review columns/methods, the engine's
    # policy event, vcs.cherry_pick_merge). Every usage is guarded — missing pieces
    # degrade to empty fields on GETs and to 501 on the accept/reject actions.

    def _json_or(raw: Any, default: Any) -> Any:
        if not raw:
            return default
        if isinstance(raw, (dict, list)):
            return raw
        try:
            out = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default
        return out if isinstance(out, type(default)) else default

    def _project_root(slug: str) -> str:
        try:
            entry = projects.get_project(settings, slug) or {}
        except AttributeError:
            entry = next((p for p in projects.list_projects(settings)
                          if p.get("slug") == slug), None) or {}
        return str(entry.get("root") or "").strip()

    def _review_target_for(slug: str) -> str:
        try:
            return projects.review_target(settings, slug)
        except AttributeError:  # projects.review_target landing separately
            return "main"

    def _plan_subtasks(task_id: str, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Subtask id/title/agent from the plan event, else the run row's plan_json."""
        for ev in _read_jsonl(settings.docs_dir / task_id / "events.jsonl"):
            if ev.get("type") == "plan":
                subs = (ev.get("data") or {}).get("subtasks") or []
                if subs:
                    return [s for s in subs if isinstance(s, dict)]
        plan = _json_or(row.get("plan_json"), {})
        return [s for s in (plan.get("subtasks") or []) if isinstance(s, dict)]

    def _policy_event(task_id: str) -> dict[str, Any] | None:
        """The run's resolved-policy event from events.jsonl (last one wins)."""
        for ev in reversed(_read_jsonl(settings.docs_dir / task_id / "events.jsonl")):
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            if ev.get("type") != "policy" and "policy" not in data:
                continue
            src = data if ("policy" in data or "tools_by_agent" in data) else ev
            return {
                "policy": src.get("policy") if isinstance(src.get("policy"), dict) else {},
                "tools_by_agent": (src.get("tools_by_agent")
                                   if isinstance(src.get("tools_by_agent"), dict) else {}),
                "review_target": str(src.get("review_target") or ""),
                "task_branch": str(src.get("task_branch") or ""),
            }
        return None

    def _git_show(root: str, sha: str) -> str:
        """Unified diff of one commit in the project repo, bounded to 100 KB.
        -m --first-parent makes merge commits show a real patch."""
        try:
            r = subprocess.run(
                ["git", "-C", root, "show", "--no-color", "-m", "--first-parent", sha],
                capture_output=True, text=True, timeout=30)
            return r.stdout[:100_000] if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    @app.get("/api/tasks/{task_id}/review")
    async def get_review(task_id: str) -> JSONResponse:
        row = app.state.runs.get(task_id)
        if row is None and not (settings.docs_dir / task_id).is_dir():
            return JSONResponse({"error": "unknown task"}, status_code=404)
        row = row or {}
        slug = _project_of_run(row)
        task_branch = str(row.get("task_branch") or "") or f"ada/{task_id}"
        target = str(row.get("review_target") or "") or _review_target_for(slug)
        try:
            states = app.state.runs.get_subtask_states(task_id) or {}
        except Exception:  # noqa: BLE001 — store variant landing separately
            states = {}
        reviews: dict[str, Any] = {}
        if hasattr(app.state.runs, "get_subtask_reviews"):
            try:
                reviews = app.state.runs.get_subtask_reviews(task_id) or {}
            except Exception:  # noqa: BLE001
                reviews = {}
        root = _project_root(slug)
        subs = _plan_subtasks(task_id, row)
        meta = {s.get("id"): s for s in subs}
        ids = [s.get("id") for s in subs if s.get("id")]
        ids += [sid for sid in states if sid not in ids]
        out = []
        for sid in ids:
            st = states.get(sid) or {}
            merge_commit = str(st.get("merge_commit") or "")
            diff = ""
            if merge_commit and root:
                diff = await asyncio.to_thread(_git_show, root, merge_commit)
            rv = reviews.get(sid) if isinstance(reviews.get(sid), dict) else {}
            out.append({
                "id": sid,
                "title": str((meta.get(sid) or {}).get("title") or ""),
                "agent": str((meta.get(sid) or {}).get("agent") or ""),
                "status": st.get("status"),
                "attempts": st.get("attempts"),
                "verdict": _json_or(st.get("verdict"), {}),
                "changed": _json_or(st.get("changed"), []),
                "merge_commit": merge_commit,
                "decision": (rv or {}).get("decision") or None,
                "diff": diff,
            })
        return JSONResponse({"task_branch": task_branch, "review_target": target,
                             "subtasks": out})

    @app.get("/api/tasks/{task_id}/permissions")
    async def get_permissions(task_id: str) -> JSONResponse:
        # Tolerant by design: a missing policy event or audit log yields empty
        # fields (the run may predate them), never an error.
        row = app.state.runs.get(task_id) or {}
        pe = _policy_event(task_id) or {}
        denied = [{
            "ts": line.get("ts"), "agent": str(line.get("agent") or ""),
            "tool": str(line.get("tool") or ""), "outcome": str(line.get("outcome") or ""),
        } for line in _read_jsonl(settings.docs_dir / task_id / "audit.jsonl")
            if str(line.get("outcome") or "").startswith("DENIED:")]
        return JSONResponse({
            "policy": pe.get("policy") or {},
            "tools_by_agent": pe.get("tools_by_agent") or {},
            "review_target": pe.get("review_target") or str(row.get("review_target") or ""),
            "task_branch": pe.get("task_branch") or str(row.get("task_branch") or ""),
            "denied": denied,
        })

    @app.post("/api/tasks/{task_id}/subtasks/{subtask_id}/accept")
    async def accept_subtask(task_id: str, subtask_id: str) -> JSONResponse:
        row = app.state.runs.get(task_id)
        if row is None:
            return JSONResponse({"error": "unknown task"}, status_code=404)
        status = row.get("status") or ""
        if status not in _TERMINAL_STATUSES:
            return JSONResponse(
                {"error": f"run is still {status or 'active'} — review after it finishes"},
                status_code=409)
        try:
            states = app.state.runs.get_subtask_states(task_id) or {}
        except Exception:  # noqa: BLE001
            states = {}
        st = states.get(subtask_id)
        if st is None:
            return JSONResponse({"error": "unknown subtask"}, status_code=404)
        merge_commit = str(st.get("merge_commit") or "").strip()
        if not merge_commit:
            return JSONResponse(
                {"error": "subtask has no merge commit to accept"}, status_code=409)
        # projects.accept_commit routes owned checkouts to an in-checkout cherry-pick
        # and in-place projects to the temp-worktree path; older trees fall back to
        # vcs.cherry_pick_merge, and with neither landed the action answers 501.
        use_accept = hasattr(projects, "accept_commit")
        if not ((use_accept or hasattr(vcs, "cherry_pick_merge"))
                and hasattr(app.state.runs, "set_subtask_review")):
            return JSONResponse(
                {"error": "per-subtask acceptance is not available yet"}, status_code=501)
        slug = _project_of_run(row)
        try:
            if use_accept:
                res = await asyncio.to_thread(
                    projects.accept_commit, settings, slug, merge_commit)
            else:
                root = _project_root(slug)
                if not root:
                    return JSONResponse(
                        {"error": "project has no repository checkout"}, status_code=409)
                target = str(row.get("review_target") or "") or _review_target_for(slug)
                res = await asyncio.to_thread(
                    vcs.cherry_pick_merge, Path(root), merge_commit, target)
        except ValueError as exc:  # e.g. unknown project / no checkout
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:  # noqa: BLE001 — surface git failures, don't 500
            return JSONResponse({"error": f"merge failed: {exc}"}, status_code=502)
        res = res if isinstance(res, dict) else {}
        if res.get("conflict"):
            return JSONResponse({**res, "error": "merge conflict — resolve on the task "
                                 "branch or reject this subtask"}, status_code=409)
        if not res.get("merged"):
            return JSONResponse({**res, "error": res.get("error") or "merge failed"},
                                status_code=409)
        app.state.runs.set_subtask_review(task_id, subtask_id, "accepted", "")
        return JSONResponse({**res, "decision": "accepted"})

    @app.post("/api/tasks/{task_id}/subtasks/{subtask_id}/reject")
    async def reject_subtask(task_id: str, subtask_id: str,
                             req: SubtaskRejectRequest) -> JSONResponse:
        row = app.state.runs.get(task_id)
        if row is None:
            return JSONResponse({"error": "unknown task"}, status_code=404)
        if not hasattr(app.state.runs, "set_subtask_review"):
            return JSONResponse(
                {"error": "per-subtask review is not available yet"}, status_code=501)
        comment = (req.comment or "").strip()
        app.state.runs.set_subtask_review(task_id, subtask_id, "rejected", comment)
        # Learning input: a rejection is negative feedback on the run (M3 consumes it).
        app.state.runs.set_feedback(
            task_id, accepted=False,
            comment=comment or f"subtask {subtask_id} rejected in review")
        return JSONResponse({"ok": True, "decision": "rejected", "comment": comment})

    def _combine_slugs(csv: str) -> list[str]:
        """Comma-separated slugs -> deduped list validated against the registry.
        Unknown slugs are dropped (never a 400) — combining is best-effort."""
        known = {p.get("slug") for p in projects.list_projects(settings)}
        out: list[str] = []
        for s in (csv or "").split(","):
            s = s.strip()
            if s and s in known and s not in out:
                out.append(s)
        return out

    @app.get("/api/graph")
    async def get_graph(project: str | None = None,
                        combine_csv: str | None = Query(None, alias="projects")) -> JSONResponse:
        # ?projects=a,b — combined, read-only multi-project view (same shape,
        # plus "projects" and per-item "sources"). The wire name stays `projects`;
        # the alias avoids shadowing the projects module in this scope.
        if combine_csv is not None and combine_csv.strip():
            return JSONResponse(
                combine.combined_triples(settings, _combine_slugs(combine_csv)))
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
    async def get_memory(project: str | None = None,
                         combine_csv: str | None = Query(None, alias="projects"),
                         q: str | None = None, top_k: int = 10,
                         kind: str | None = None) -> JSONResponse:
        # ?projects=a,b — combined, read-only multi-project view, every item
        # tagged with its "project" slug. With &q=... it runs the same hybrid
        # recall memory search uses (score-sorted, cross-project deduped);
        # without q it merges the projects' recent memories (today's item shape).
        if combine_csv is not None and combine_csv.strip():
            slugs = _combine_slugs(combine_csv)
            if q is not None and q.strip():
                return JSONResponse(combine.combined_memory_search(
                    settings, slugs, q.strip(),
                    top_k=max(1, min(int(top_k), 100)), kind=kind))
            return JSONResponse(combine.combined_memory_recent(settings, slugs, kind=kind))
        # Show the project's memories plus the shared global memories.
        s = dataclasses.replace(settings, project=projects.resolve(settings, project))
        out = _read_memory(s.db_path, "project") + _read_memory(s.global_db_path, "global")
        out.sort(key=lambda m: m["created_at"], reverse=True)
        return JSONResponse(out[:300])

    _WS_HIDDEN = {".git", ".ada_worktrees", ".ada_deps", "__pycache__", ".pytest_cache",
                  "node_modules", ".venv"}

    def _workspace_root(task: str | None) -> Path | None:
        """Resolve a task's workspace dir across layouts: legacy workspace/<task-id>,
        or the project layout workspace/<slug>/worktrees/<task-id>."""
        ws = settings.workspace_dir.resolve()
        if not task:
            return ws
        legacy = (ws / task).resolve()
        if legacy.is_relative_to(ws) and legacy.is_dir():
            return legacy
        row = app.state.runs.get(task) or {}
        slug = row.get("project")
        if slug:
            wt = (ws / slug / "worktrees" / task).resolve()
            if wt.is_relative_to(ws) and wt.is_dir():
                return wt
        return None

    @app.get("/api/workspace")
    async def list_workspace(task: str | None = None) -> JSONResponse:
        root = _workspace_root(task)
        if root is None or not root.exists():
            return JSONResponse([])
        items = []
        for p in sorted(root.rglob("*")):
            if (p.is_file() and p.suffix != ".pyc"
                    and not _WS_HIDDEN.intersection(p.parts)):
                items.append({"path": str(p.relative_to(root)), "size": p.stat().st_size})
        return JSONResponse(items)

    @app.get("/api/workspace/file")
    async def workspace_file(path: str, task: str | None = None) -> JSONResponse:
        root = _workspace_root(task)
        if root is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        target = (root / path).resolve()
        if (not target.is_relative_to(root) or not target.is_file()
                or _WS_HIDDEN.intersection(target.relative_to(root).parts)):
            return JSONResponse({"error": "not found"}, status_code=404)
        data = target.read_text(errors="replace")
        return JSONResponse({"path": path, "content": data[:200_000], "truncated": len(data) > 200_000})

    @app.get("/api/workspace/download")
    async def workspace_download(path: str, task: str | None = None):
        root = _workspace_root(task)
        if root is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        target = (root / path).resolve()
        if (not target.is_relative_to(root) or not target.is_file()
                or _WS_HIDDEN.intersection(target.relative_to(root).parts)):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(target), filename=target.name, media_type="application/octet-stream")

    # ---- Run the project (UI "Run" tab): start/stop the checkout's app ----
    # One process per project, launched in its checkout with a scrubbed env in its own
    # process group; stdout/stderr stream into a ring buffer the UI polls. A convenience
    # runner for trying delivered work before merging — same isolation stance as agent
    # runs (the container is the real boundary).
    _APP_PORT = 8123
    _APP_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "VIRTUAL_ENV")
    _app_procs: dict[str, dict[str, Any]] = {}

    def _detect_app(root: Path) -> dict[str, Any] | None:
        """Best-effort detection of a runnable app in the checkout."""
        port = _APP_PORT
        if (root / "manage.py").is_file():
            return {"detected": "Django app (manage.py)",
                    "cmd": ["python", "manage.py", "runserver", f"127.0.0.1:{port}"],
                    "url": f"http://127.0.0.1:{port}"}
        pkg = root / "package.json"
        if pkg.is_file():
            try:
                scripts = json.loads(pkg.read_text()).get("scripts") or {}
            except (json.JSONDecodeError, OSError):
                scripts = {}
            for script in ("dev", "start"):
                if script in scripts:
                    return {"detected": f"npm project (scripts.{script})",
                            "cmd": ["npm", "run", script], "url": ""}
        for cand in ("main.py", "app.py", "server.py"):
            f = root / cand
            if not f.is_file():
                continue
            try:
                text = f.read_text(errors="replace").lower()
            except OSError:
                continue
            mod = cand[:-3]
            if "fastapi" in text:
                return {"detected": f"FastAPI app ({cand})",
                        "cmd": ["uvicorn", f"{mod}:app", "--port", str(port)],
                        "url": f"http://127.0.0.1:{port}"}
            if "flask" in text:
                return {"detected": f"Flask app ({cand})",
                        "cmd": ["flask", "--app", mod, "run", "--port", str(port)],
                        "url": f"http://127.0.0.1:{port}"}
        return None

    def _app_running(entry: dict[str, Any] | None) -> bool:
        return bool(entry and entry["proc"].poll() is None)

    def _app_state(slug: str) -> dict[str, Any]:
        root = projects.project_checkout(settings, slug)
        entry = _app_procs.get(slug)
        running = _app_running(entry)
        if root is None or not root.is_dir():
            return {"runnable": False, "running": False,
                    "reason": "This project has no repository checkout to run."}
        det = _detect_app(root)
        if det is None and not running:
            return {"runnable": False, "running": False,
                    "reason": "Nothing runnable detected in the checkout — no FastAPI/Flask "
                              "entrypoint, manage.py, or npm start script."}
        cmd = entry["cmd"] if running else det["cmd"]
        url = entry["url"] if running else (det or {}).get("url", "")
        return {"runnable": True, "running": running,
                "detected": (det or {}).get("detected") or "App",
                "cmd": " ".join(cmd), "cwd": str(root), "url": url,
                "pid": entry["proc"].pid if running else None}

    def _pump_app_logs(proc: subprocess.Popen, buf: deque) -> None:
        try:
            for line in proc.stdout:  # text mode; ends when the process exits
                buf.append(line.rstrip("\n"))
        except ValueError:
            pass  # stream closed on stop

    @app.get("/api/projects/{slug}/app")
    async def get_app_state(slug: str) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        return JSONResponse(_app_state(slug))

    @app.post("/api/projects/{slug}/app/start")
    async def start_project_app(slug: str) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        st = _app_state(slug)
        if st.get("running"):
            return JSONResponse(st)
        if not st.get("runnable"):
            return JSONResponse({"error": st.get("reason") or "nothing runnable detected"},
                                status_code=400)
        root = projects.project_checkout(settings, slug)
        det = _detect_app(root)  # runnable + not running -> det is not None
        env = {k: os.environ[k] for k in _APP_ENV_KEEP if k in os.environ}
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(det["cmd"], cwd=str(root), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, start_new_session=True)
        except OSError as exc:
            return JSONResponse({"error": f"could not start: {exc}"}, status_code=500)
        buf: deque[str] = deque(maxlen=500)
        buf.append("$ " + " ".join(det["cmd"]))
        threading.Thread(target=_pump_app_logs, args=(proc, buf), daemon=True).start()
        _app_procs[slug] = {"proc": proc, "logs": buf, "cmd": det["cmd"],
                            "url": det.get("url", ""), "started": time.time()}
        return JSONResponse(_app_state(slug))

    @app.post("/api/projects/{slug}/app/stop")
    async def stop_project_app(slug: str) -> JSONResponse:
        entry = _app_procs.get(slug)
        if entry and entry["proc"].poll() is None:
            try:  # kill the whole process group (uvicorn reloaders, npm children, …)
                os.killpg(os.getpgid(entry["proc"].pid), signal.SIGTERM)
            except (AttributeError, ProcessLookupError, PermissionError, OSError):
                entry["proc"].terminate()
        return JSONResponse({"ok": True, "running": False})

    @app.get("/api/projects/{slug}/app/logs")
    async def get_app_logs(slug: str) -> JSONResponse:
        entry = _app_procs.get(slug)
        return JSONResponse({"lines": list(entry["logs"]) if entry else [],
                             "running": _app_running(entry)})

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


# W6: no module-level `app = create_app()` — importing this module must not run
# Settings.load() or open SQLite. Serve via the factory instead, e.g.:
#   uvicorn ai_dev_assistant.web.server:create_app --factory
