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
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import analytics, config, github, notify, playbooks, projects, search, vcs
from .. import gc as workspace_gc
from .. import workspaces as workspaces_mod
from ..agents import custom as custom_agents
from ..agents.registry import build_agents, builtin_agent_names
from ..config import Settings
from ..evals import history as eval_history
from ..engine import Engine
from ..knowledge import combine
from ..knowledge.base import KnowledgeBase
from ..knowledge.graph import NetworkXKnowledgeGraph
from ..llm.errors import LLMError
from ..llm.schemas import Plan
from ..memory.store import (
    MemoryStore,
    count_project_memories,
    delete_project_memory,
    list_project_memories,
    update_project_memory,
)
from ..orchestration.events import Event
from ..orchestration.run_control import RunControl
from ..orchestration.run_store import RunStore, derive_title
from ..orchestration.schedules import ScheduleStore, next_run_at
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

# Valid Settings field names — used to sanity-filter playbook settings_overrides.
_SETTINGS_FIELDS = {f.name for f in dataclasses.fields(Settings)}

# Background loop cadences (seconds).
_SCHEDULES_TICK_S = 60.0
_GITHUB_TICK_S = 120.0

_KB_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # per-file cap for /kb/upload


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
                  git_finalize: bool | None = None, model: str | None = None,
                  settings_overrides: dict[str, Any] | None = None) -> Settings:
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
    # Playbook settings_overrides land after policy, but still under any explicit
    # request choices (effort/model/budget/...) — those are re-applied last.
    extra = {k: v for k, v in (settings_overrides or {}).items() if k in _SETTINGS_FIELDS}
    if extra:
        resolved = dataclasses.replace(resolved, **extra)
        if overrides:
            resolved = dataclasses.replace(resolved, **overrides)
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
    # Optional slug -> upstream slugs for fan-out runs: dependents start after their
    # upstreams and get the upstream results appended to their prompt.
    project_deps: dict[str, list[str]] | None = None
    memory_scope: str | None = None  # "project" | "global"
    continue_from: str | None = None  # re-engage: continue this completed task's workspace + context
    # Clean break: repo_path/repo_url/repo_ref are gone — the run's *project* owns the repo.
    git_finalize: bool | None = None  # commit the workspace to a new branch at the end
    # Additive attribution: set when a workspace run expanded into this request
    # (POST /api/workspaces/{ws}/run) — carried in the queue payload, never read
    # by the run machinery itself.
    workspace: str | None = None


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


class PlaybookRunRequest(BaseModel):
    params: dict[str, Any] = {}
    project: str | None = None
    effort: str | None = None
    model: str | None = None
    budget: float | None = None
    title: str | None = None        # optional; defaults to the playbook's rendered title


class ScheduleCreateRequest(BaseModel):
    project: str | None = None
    prompt: str
    title: str | None = None
    # Recurrence is exactly one of every_hours (interval) or cron (5-field
    # expression) — the ScheduleStore validates and 400s on bad/ambiguous input.
    every_hours: float | None = None
    cron: str | None = None
    budget_usd: float = 0.0


class SchedulePatchRequest(BaseModel):
    project: str | None = None
    prompt: str | None = None
    title: str | None = None
    every_hours: float | None = None
    cron: str | None = None  # explicit null reverts a cron row to its interval
    budget_usd: float | None = None
    enabled: bool | None = None


class MemoryUpdateRequest(BaseModel):
    content: str


class GcRequest(BaseModel):
    keep_days: int | None = None   # default: the gc_keep_days setting
    ids: list[str] | None = None   # restrict removal to these task ids


class ABRequest(BaseModel):
    knob: str
    values: list[str]


class LoginRequest(BaseModel):
    token: str = ""


class WorkspaceCreateRequest(BaseModel):
    name: str
    description: str = ""
    projects: list[str] | None = None   # initial members (must exist; moves them here)


class WorkspacePatchRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class WorkspaceMemberRequest(BaseModel):
    project: str


class WorkspaceDepsRequest(BaseModel):
    deps: dict[str, list[str]] | None = None  # {slug: [upstream slugs]}; None/{} clears


class WorkspaceRunRequest(BaseModel):
    prompt: str
    title: str | None = None
    effort: str | None = None
    model: str | None = None
    budget: float | None = None
    subset: list[str] | None = None  # run only these members (validated against the ws)


class AgentSaveRequest(BaseModel):
    spec: dict[str, Any]


def create_app(settings: Settings | None = None, host: str | None = None,
               api_token: str | None = None) -> FastAPI:
    # `settings` is the env/default base (paths, backend identity). The settings
    # console's overlay is applied ON TOP into app.state.base_settings — the live
    # base every NEW run/plan starts from; PATCH/DELETE /api/settings rebind it,
    # so edits apply to new runs immediately, without a restart.
    settings = settings or Settings.load(overlay=False)
    app = FastAPI(title="AI Dev Assistant")
    app.state.settings = settings
    app.state.env_settings = settings
    app.state.base_settings = config.apply_overrides(settings)
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
        # session cookie set by POST /api/login (how the browser UI authenticates)
        cookie = request.cookies.get("ada_token", "")
        if cookie and secrets.compare_digest(cookie, app.state.api_token):
            return True
        supplied = request.query_params.get("token", "")
        return bool(supplied) and secrets.compare_digest(supplied, app.state.api_token)

    # Always-open auth endpoints: the UI must be able to ask whether login is needed
    # and perform it while still unauthorized.
    _AUTH_OPEN = {"/api/auth/status", "/api/login", "/api/logout"}

    @app.middleware("http")
    async def require_token(request, call_next):
        # /healthz, /readyz, / and /static stay open; every /api/* route needs the token.
        if (request.url.path.startswith("/api/") and request.url.path not in _AUTH_OPEN
                and not _authorized(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> JSONResponse:
        return JSONResponse({"auth_required": bool(app.state.api_token),
                             "authorized": _authorized(request)})

    @app.post("/api/login")
    async def login(req: LoginRequest) -> Response:
        supplied = (req.token or "").strip()
        if not (app.state.api_token and supplied
                and secrets.compare_digest(supplied, app.state.api_token)):
            await asyncio.sleep(0.3)  # flat delay on every failure — slows brute force
            return JSONResponse({"error": "invalid token"}, status_code=403)
        # HttpOnly session cookie: flows on every fetch/WebSocket automatically and is
        # unreadable from JS. ADA_COOKIE_SECURE=1 marks it HTTPS-only (set behind TLS).
        resp = Response(status_code=204)
        resp.set_cookie("ada_token", app.state.api_token, httponly=True,
                        samesite="strict", path="/",
                        secure=os.getenv("ADA_COOKIE_SECURE", "").strip() == "1")
        return resp

    @app.post("/api/logout")
    async def logout() -> Response:
        resp = Response(status_code=204)
        resp.delete_cookie("ada_token", path="/")
        return resp

    # Explicit CORS policy: same-origin by default (no cross-origin allowed);
    # widen with a comma-separated ADA_CORS_ORIGINS.
    cors_origins = [o.strip() for o in os.getenv("ADA_CORS_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins,
                       allow_methods=["*"], allow_headers=["*"])
    app.state.tasks = {}  # task_id -> asyncio.Task (for cancellation)
    app.state.runs = RunStore(settings.data_dir / "runs.db")
    app.state.runs.interrupt_orphans()  # clean up runs orphaned by a restart
    # Recurring runs: same runs.db file, its own table + connection (schedules contract).
    app.state.schedules = ScheduleStore(settings.data_dir / "runs.db")
    app.state.github_transport = None  # tests inject a fake github Transport here
    app.state.bg_tasks = []  # background loop asyncio.Tasks (started at startup)
    # task queue / scheduler state
    app.state.concurrency = max(1, app.state.base_settings.max_concurrent_runs)
    app.state.paused = False
    app.state.running = set()  # task_ids currently executing
    app.state.controls = {}  # task_id -> RunControl (in-run pause/steer)

    @app.middleware("http")
    async def no_cache(request, call_next):  # static assets should never be cached in dev
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path in ("/", "/app"):
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
                bool(payload.get("stagger")), model=payload.get("model"),
                deps=payload.get("project_deps")))
            return
        app.state.tasks[task_id] = asyncio.create_task(_run_task(
            task_id, payload.get("prompt", ""), payload.get("plan"),
            payload.get("effort"), payload.get("budget"), payload.get("title"),
            payload.get("project"), payload.get("memory_scope"), payload.get("continue_from"),
            payload.get("git_finalize"), bool(payload.get("resume")),
            model=payload.get("model"),
            settings_overrides=payload.get("settings_overrides")))

    # ---- Out-of-tab notifications: NotifyConfig from the LIVE settings at event
    # time; dispatch rides the engine on_event bridge and never blocks the loop.
    def _notify_config(live: Settings) -> notify.NotifyConfig:
        events = tuple(e.strip().lower() for e in (live.notify_events or "").split(",")
                       if e.strip())
        desktop = bool(live.notify_desktop) and sys.platform == "darwin"
        try:
            smtp_port = int(live.notify_smtp_port or 587)
        except (TypeError, ValueError):
            smtp_port = 587
        kwargs: dict[str, Any] = dict(
            webhook_url=(live.notify_webhook or "").strip(),
            slack_webhook_url=(live.notify_slack_webhook or "").strip(),
            desktop=desktop,
            email_to=(live.notify_email_to or "").strip(),
            smtp_host=(live.notify_smtp_host or "").strip(),
            smtp_port=smtp_port,
            smtp_user=(live.notify_smtp_user or "").strip(),
            smtp_starttls=bool(live.notify_smtp_starttls),
        )
        if events:
            kwargs["events"] = events
        return notify.NotifyConfig(**kwargs)

    def _notify_channels_configured(cfg: notify.NotifyConfig) -> bool:
        return bool(cfg.webhook_url or cfg.slack_webhook_url or cfg.desktop
                    or (cfg.email_to and cfg.smtp_host))

    def _dispatch_notify(event: Event, task_id: str, project_slug: str) -> None:
        """Fan a run event out to the configured notify channels. Fire-and-forget:
        the webhook/osascript work runs in a thread, and nothing here ever raises."""
        try:
            cfg = _notify_config(app.state.base_settings)
            if not _notify_channels_configured(cfg):
                return
            if not notify.should_notify(cfg, event.type):
                return

            def _send() -> None:
                notify.notify_event(
                    cfg, event_type=event.type, task_id=task_id, project=project_slug,
                    message=event.message or "",
                    data=event.data if isinstance(event.data, dict) else {})

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _send()  # no running loop (tests/teardown) — notify_event never raises
                return
            loop.create_task(asyncio.to_thread(_send))
        except Exception:  # noqa: BLE001 — a notifier must never take down a run
            pass

    app.state.notify_dispatch = _dispatch_notify

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
                        model: str | None = None,
                        settings_overrides: dict[str, Any] | None = None) -> None:
        broker: Broker = app.state.brokers[task_id]
        slug = projects.resolve(settings, project)

        def _publish(ev: Event) -> None:
            broker.publish(ev)
            _dispatch_notify(ev, task_id, slug)

        # record up front so cancels-during-planning persist (title auto-derived if blank);
        # the run row carries its project so activity/history can be filtered per project.
        try:
            app.state.runs.start(task_id, prompt, title=(title or None), project=slug)
        except TypeError:  # run store without the project column (landing separately)
            app.state.runs.start(task_id, prompt, title=(title or None))
        if continue_from:
            app.state.runs.set_parent(task_id, continue_from)
        engine = Engine(_settings_for(app.state.base_settings, effort, budget, project,
                                      memory_scope, git_finalize, model=model,
                                      settings_overrides=settings_overrides))
        control = RunControl()
        engine.control = control  # enables pause/resume/steer endpoints to reach this run
        app.state.controls[task_id] = control
        # W5/R1: resume rides through **kwargs so this compiles against Engine.run whether
        # or not the checkpoint-resume kwarg has landed; the TypeError guard below fails soft.
        extra: dict[str, Any] = {"resume": True} if resume else {}
        try:
            plan = Plan.model_validate(plan_dict) if plan_dict else None
            await engine.run(prompt, plan=plan, task_id=task_id, title=(title or None),
                             continue_from=continue_from, on_event=_publish, **extra)
        except TypeError as exc:
            msg = "resume not available yet" if resume else str(exc)
            _publish(Event("error", f"Run failed: {msg}", {"message": msg}))
            app.state.runs.set_status(task_id, "failed")
        except asyncio.CancelledError:
            _publish(Event("error", "Run cancelled by user.", {"message": "cancelled"}))
            app.state.runs.set_status(task_id, "cancelled")
        except LLMError as exc:
            _publish(Event("error", f"Run failed: {exc}", {"message": str(exc)}))
            app.state.runs.set_status(task_id, "failed")  # don't strand it 'running'
        except Exception as exc:  # don't leave the socket hanging on unexpected failures
            _publish(Event("error", f"Unexpected error: {exc}", {"message": str(exc)}))
            app.state.runs.set_status(task_id, "failed")
        finally:
            await engine.aclose()
            app.state.tasks.pop(task_id, None)
            app.state.running.discard(task_id)
            app.state.controls.pop(task_id, None)
            if not broker.done:
                _publish(Event("done", "Run ended.", {}))
            _evict_broker_later(task_id, broker)
            _pump()  # a slot just freed — start the next queued task

    async def _run_fanout(task_id: str, prompt: str, slugs: list[str],
                          effort: str | None, budget: float | None,
                          title: str | None = None, stagger: bool = False,
                          model: str | None = None,
                          deps: dict[str, list[str]] | None = None) -> None:
        """F3: cross-project fan-out parent. run_cross_project owns the run rows
        (parent project='multi', children with parent_id) and emits plan/child_start/
        child_done/brief/done on the parent stream — we just wire it into the Broker."""
        broker: Broker = app.state.brokers[task_id]

        def _publish(ev: Event) -> None:
            broker.publish(ev)
            _dispatch_notify(ev, task_id, _MULTI_PROJECT)

        fn = _fanout_runner()
        try:
            if fn is None:  # queued before the fan-out core landed (e.g. across a restart)
                msg = "cross-project fan-out is not available yet"
                _publish(Event("error", msg, {"message": msg}))
                app.state.runs.set_status(task_id, "failed")
                return
            # deps is passed only when set so fakes/older cores without the kwarg
            # keep working for plain fan-out runs.
            extra = {"deps": deps} if deps else {}
            await fn(_settings_for(app.state.base_settings, effort, budget, model=model),
                     prompt, slugs,
                     title=(title or None), stagger=bool(stagger), task_id=task_id,
                     on_event=_publish, **extra)
        except asyncio.CancelledError:
            _publish(Event("error", "Run cancelled by user.", {"message": "cancelled"}))
            app.state.runs.set_status(task_id, "cancelled")
        except LLMError as exc:
            _publish(Event("error", f"Run failed: {exc}", {"message": str(exc)}))
            app.state.runs.set_status(task_id, "failed")
        except Exception as exc:  # don't leave the socket hanging on unexpected failures
            _publish(Event("error", f"Unexpected error: {exc}", {"message": str(exc)}))
            app.state.runs.set_status(task_id, "failed")
        finally:
            app.state.tasks.pop(task_id, None)
            app.state.running.discard(task_id)
            if not broker.done:
                _publish(Event("done", "Run ended.", {}))
            _evict_broker_later(task_id, broker)
            _pump()  # a slot just freed — start the next queued task

    @app.post("/api/plan")
    async def make_plan(req: PlanRequest) -> JSONResponse:
        engine = Engine(_settings_for(app.state.base_settings, req.effort, req.budget,
                                      req.project, req.memory_scope, model=req.model))
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
        engine = Engine(_settings_for(app.state.base_settings, req.effort, req.budget,
                                      req.project, req.memory_scope, model=req.model))
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
        deps: dict[str, list[str]] | None = None
        if slugs and req.project_deps:
            try:  # unknown slugs / cycles are a client error — reject before enqueue
                from ..orchestration.fanout import validate_project_deps, _dependency_waves
                deps = validate_project_deps(slugs, req.project_deps)
                _dependency_waves(slugs, deps)  # cycle check
                deps = deps or None
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except ImportError:  # core landing separately: pass through unvalidated
                deps = req.project_deps
        task_id = req.task_id or new_task_id()
        app.state.brokers[task_id] = Broker()
        app.state.brokers[task_id].publish(Event("status", "Backend: " + settings.llm_backend,
                                                  {"backend": settings.llm_backend}))
        if slugs:
            payload: dict[str, Any] = {
                "prompt": req.prompt, "projects": slugs, "stagger": bool(req.stagger),
                "effort": req.effort, "model": req.model, "budget": req.budget, "title": req.title,
            }
            if deps:
                payload["project_deps"] = deps
        else:
            payload = {
                "prompt": req.prompt, "plan": req.plan, "effort": req.effort,
                "model": req.model, "budget": req.budget, "title": req.title, "project": project,
                "memory_scope": req.memory_scope, "continue_from": req.continue_from,
                "git_finalize": req.git_finalize,
            }
        if req.workspace:  # additive workspace attribution (never read by _start)
            payload["workspace"] = req.workspace
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

    # Full per-subtask agent transcript: every agent_step event (thinking / text / tool /
    # tool_result / result) for one subtask, in emission order, straight from the durable
    # event log. Unknown task or subtask -> empty transcript (not an error).
    @app.get("/api/runs/{task_id}/transcript/{subtask_id}")
    async def get_transcript(task_id: str, subtask_id: str) -> JSONResponse:
        steps = [ev["data"] for ev in _read_jsonl(settings.docs_dir / task_id / "events.jsonl")
                 if ev.get("type") == "agent_step"
                 and (ev.get("data") or {}).get("id") == subtask_id]
        agent = next((s.get("agent", "") for s in steps if s.get("agent")), "")
        return JSONResponse({"agent": agent, "count": len(steps), "steps": steps})

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
        # Whitelisted fields only — never API keys, tokens, or paths. Reads the
        # LIVE base (env + console overlay) so the meter tracks console edits.
        live = app.state.base_settings
        return JSONResponse({
            "budget_usd": live.budget_usd,
            "llm_backend": live.llm_backend,
            "max_concurrent_runs": live.max_concurrent_runs,
            "sdk_model": live.sdk_model,
        })

    # ---- Global settings console: schema-driven GET/PATCH/DELETE over the JSON
    # overlay in <data_dir>/settings.json. PATCH/DELETE rebind the live base so
    # NEW runs pick the change up immediately; in-flight runs keep their settings.
    def _settings_source(key: str, overlay: dict[str, Any]) -> str:
        if key in overlay:
            return "override"
        return "env" if (os.getenv(config.env_var_for(key)) or "") != "" else "default"

    @app.get("/api/settings")
    async def get_global_settings() -> JSONResponse:
        overlay = config.load_overrides(settings.data_dir)
        live = app.state.base_settings
        groups: list[dict[str, Any]] = []
        by_name: dict[str, dict[str, Any]] = {}
        for entry in config.SETTINGS_SCHEMA:
            group = by_name.get(entry["group"])
            if group is None:
                group = {"name": entry["group"], "fields": []}
                by_name[entry["group"]] = group
                groups.append(group)
            group["fields"].append({
                "key": entry["key"], "label": entry["label"], "help": entry["help"],
                "type": entry["type"], "choices": list(entry.get("choices") or []),
                "value": getattr(live, entry["key"]),
                "source": _settings_source(entry["key"], overlay),
                "restart_required": bool(entry.get("restart_required")),
            })
        return JSONResponse({
            "groups": groups,
            "info": {
                "llm_backend": settings.llm_backend,
                "data_dir": str(settings.data_dir),
                "projects": len(projects.list_projects(settings)),
            },
        })

    @app.patch("/api/settings")
    async def patch_global_settings(payload: dict[str, Any]) -> JSONResponse:
        if not payload:
            return JSONResponse({"error": "no settings provided"}, status_code=400)
        coerced: dict[str, Any] = {}
        for key, value in payload.items():
            if not config.is_editable(key):
                return JSONResponse({"error": f"unknown or non-editable setting: {key}"},
                                    status_code=400)
            try:  # None deletes the override (save_overrides contract)
                coerced[key] = None if value is None else config.coerce_setting(key, value)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        config.save_overrides(settings.data_dir, coerced)
        app.state.base_settings = config.apply_overrides(app.state.env_settings)
        return JSONResponse({"ok": True, "settings": {
            k: getattr(app.state.base_settings, k) for k in coerced}})

    @app.delete("/api/settings/{key}")
    async def delete_global_setting(key: str) -> JSONResponse:
        if not config.is_editable(key):
            return JSONResponse({"error": f"unknown or non-editable setting: {key}"},
                                status_code=400)
        config.save_overrides(settings.data_dir, {key: None})
        app.state.base_settings = config.apply_overrides(app.state.env_settings)
        return JSONResponse({
            "ok": True, "value": getattr(app.state.base_settings, key),
            "source": _settings_source(key, config.load_overrides(settings.data_dir)),
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

    # ---- Home aggregation: one read-only JSON for the console's home screen.
    # Each section is computed independently and never raises — a failing section
    # yields its empty default plus a line in "errors".
    @app.get("/api/home")
    async def get_home() -> JSONResponse:
        out: dict[str, Any] = {
            "attention": [], "running": [], "queued": [], "recent": [],
            "spend": {}, "benchmarks": {}, "workspaces": [], "counts": {},
            "errors": [],
        }

        def _err(section: str, exc: Exception) -> None:
            out["errors"].append(f"{section}: {exc}")

        # Open ask/permission requests across RUNNING tasks: RunControl tracks the
        # pending ids; the question/agent/options detail lives on the broker's
        # ask/permission events (engine attention hook), matched by request id.
        try:
            for tid, ctrl in list(app.state.controls.items()):
                pending = {rid for rid, fut in dict(getattr(ctrl, "_pending", {})).items()
                           if not fut.done()}
                if not pending:
                    continue
                row = app.state.runs.get(tid) or {}
                broker = app.state.brokers.get(tid)
                for ev in list(broker.events) if broker is not None else []:
                    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                    rid = data.get("id")
                    if ev.get("type") not in ("ask", "permission") or rid not in pending:
                        continue
                    pending.discard(rid)
                    text = (data.get("question") or data.get("request")
                            or ev.get("message") or "")
                    item = {"task_id": tid, "project": _project_of_run(row),
                            "kind": ev["type"], "id": rid,
                            "agent": str(data.get("agent") or ""),
                            "options": list(data.get("options") or [])}
                    item["question" if ev["type"] == "ask" else "request"] = text
                    out["attention"].append(item)
        except Exception as exc:  # noqa: BLE001
            _err("attention", exc)

        try:
            for tid in sorted(app.state.running):
                r = app.state.runs.get(tid) or {}
                item = {"task_id": tid, "title": _title_of(r),
                        "project": _project_of_run(r)}
                if r.get("subtasks_total"):  # progress only when cheaply available
                    item["progress"] = {"passed": r.get("subtasks_passed"),
                                        "total": r.get("subtasks_total")}
                out["running"].append(item)
        except Exception as exc:  # noqa: BLE001
            _err("running", exc)

        try:
            positions = app.state.runs.queue_positions()
            for p in app.state.runs.queue_pending():
                payload = p.get("payload") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = {}
                if len(payload.get("projects") or []) >= 2:
                    p_slug = _MULTI_PROJECT
                else:
                    p_slug = projects.resolve(settings, payload.get("project"))
                out["queued"].append({"task_id": p["task_id"], "title": _title_of(p),
                                      "project": p_slug,
                                      "position": positions.get(p["task_id"])})
        except Exception as exc:  # noqa: BLE001
            _err("queued", exc)

        try:
            recent = []
            for r in app.state.runs.list(limit=100):
                if (r.get("status") or "") not in _TERMINAL_STATUSES:
                    continue
                recent.append({"task_id": r.get("id"), "title": _title_of(r),
                               "project": _project_of_run(r), "status": r.get("status"),
                               "quality": r.get("quality_score"),
                               "cost_usd": r.get("cost_usd"),
                               "ended_at": r.get("ended_at")})
                if len(recent) == 10:
                    break
            out["recent"] = recent
        except Exception as exc:  # noqa: BLE001
            _err("recent", exc)

        try:
            out["spend"] = await asyncio.to_thread(
                analytics.spend_overview, app.state.base_settings, days=30)
        except Exception as exc:  # noqa: BLE001
            _err("spend", exc)

        try:  # graceful when empty: {"latest": None, "delta": None, "series": []}
            out["benchmarks"] = await asyncio.to_thread(eval_history.trend_report, settings)
        except Exception as exc:  # noqa: BLE001
            _err("benchmarks", exc)

        ws_entries: list[dict[str, Any]] = []
        try:
            ws_entries = workspaces_mod.list_workspaces(settings)
            out["workspaces"] = [{"slug": w["slug"], "name": w["name"],
                                  "projects": len(w.get("project_slugs") or [])}
                                 for w in ws_entries]
        except Exception as exc:  # noqa: BLE001
            _err("workspaces", exc)

        try:
            out["counts"] = {
                "projects": len(projects.list_projects(settings)),
                "workspaces": len(ws_entries),
                "custom_agents": len(custom_agents.list_custom_agents(settings)),
            }
        except Exception as exc:  # noqa: BLE001
            _err("counts", exc)
        return JSONResponse(out)

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
        # the token as a query param, or the ada_token session cookie (set by
        # /api/login; rides along on the handshake). Reject before it completes.
        if app.state.api_token:
            supplied = websocket.query_params.get("token", "")
            cookie = websocket.cookies.get("ada_token", "")
            if not ((supplied and secrets.compare_digest(supplied, app.state.api_token))
                    or (cookie and secrets.compare_digest(cookie, app.state.api_token))):
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

    # ---- Agents: the built-in roster + user-defined custom agents (agents/custom.py).
    # New runs pick customs up automatically — Engine.__init__ calls
    # build_agents(settings) per run, which composes customs with the built-ins.
    @app.get("/api/agents")
    async def list_agents() -> JSONResponse:
        roster = build_agents(app.state.base_settings)  # tool roster follows console edits
        custom_specs = custom_agents.list_custom_agents(settings)
        custom_names = {s.name for s in custom_specs}
        return JSONResponse({
            "builtin": [{
                "name": a.profile.name,
                "description": a.profile.description,
                "when_to_use": a.profile.when_to_use,
                "tools": a.profile.tools,
                "effort": a.profile.effort,
            } for a in roster.values() if a.profile.name not in custom_names],
            # Raw stored specs (incl. system_prompt/model) so the UI can edit them.
            "custom": [s.to_dict() for s in custom_specs],
            "tools": sorted(custom_agents.toolbox_tool_names()),
        })

    @app.post("/api/agents")
    async def save_agent(req: AgentSaveRequest) -> JSONResponse:
        try:
            saved = custom_agents.save_custom_agent(settings, req.spec)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "agent": saved.to_dict()})

    @app.delete("/api/agents/{name}")
    async def delete_agent(name: str) -> JSONResponse:
        slug = (name or "").strip().lower()
        if slug in builtin_agent_names():
            return JSONResponse({"error": f"'{slug}' is a built-in agent and cannot be deleted"},
                                status_code=400)
        if not custom_agents.delete_custom_agent(settings, slug):
            return JSONResponse({"error": f"unknown custom agent: {slug}"}, status_code=404)
        return JSONResponse({"ok": True})

    @app.get("/api/projects")
    async def get_projects() -> JSONResponse:
        # Additive "workspace" per item (slug | null) so the sidebar can group.
        try:
            ws_of = {m: w["slug"] for w in workspaces_mod.list_workspaces(settings)
                     for m in w.get("project_slugs", [])}
        except Exception:  # noqa: BLE001 — grouping is best-effort, never blocks the list
            ws_of = {}
        return JSONResponse([{**p, "workspace": ws_of.get(p.get("slug"))}
                             for p in projects.list_projects(settings)])

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

    # ---- Workspaces: named groups of inter-related projects (workspaces.py).
    # Entries are returned as stored/normalized: {slug, name, description,
    # created_at, project_slugs, default_deps}. A workspace run expands via
    # workspace_run_spec and re-enters POST /api/run's own handler, so it is
    # byte-identical to a manual projects+project_deps run (plus the additive
    # "workspace" payload key for attribution).

    def _ws_error(exc: ValueError) -> JSONResponse:
        msg = str(exc)
        code = 404 if msg.startswith("unknown workspace") else 400
        return JSONResponse({"error": msg}, status_code=code)

    @app.get("/api/workspaces")
    async def list_workspaces_endpoint() -> JSONResponse:
        return JSONResponse(workspaces_mod.list_workspaces(settings))

    @app.post("/api/workspaces")
    async def create_workspace_endpoint(req: WorkspaceCreateRequest) -> JSONResponse:
        if not (req.name or "").strip():
            return JSONResponse({"error": "name required"}, status_code=400)
        try:
            entry = workspaces_mod.create_workspace(
                settings, req.name, description=req.description or "",
                projects=req.projects)
        except ValueError as exc:
            return _ws_error(exc)
        return JSONResponse(entry)

    @app.patch("/api/workspaces/{ws}")
    async def patch_workspace_endpoint(ws: str, req: WorkspacePatchRequest) -> JSONResponse:
        try:
            entry = workspaces_mod.update_workspace(
                settings, ws, name=req.name, description=req.description)
        except ValueError as exc:
            return _ws_error(exc)
        return JSONResponse(entry)

    @app.delete("/api/workspaces/{ws}")
    async def delete_workspace_endpoint(ws: str) -> JSONResponse:
        workspaces_mod.delete_workspace(settings, ws)  # no-op on unknown; projects survive
        return JSONResponse({"ok": True})

    @app.post("/api/workspaces/{ws}/projects")
    async def assign_workspace_project(ws: str, req: WorkspaceMemberRequest) -> JSONResponse:
        try:
            entry = workspaces_mod.assign_project(settings, ws, req.project)
        except ValueError as exc:
            return _ws_error(exc)
        return JSONResponse(entry)

    @app.delete("/api/workspaces/{ws}/projects/{project_slug}")
    async def unassign_workspace_project(ws: str, project_slug: str) -> JSONResponse:
        try:
            entry = workspaces_mod.unassign_project(settings, ws, project_slug)
        except ValueError as exc:
            return _ws_error(exc)
        return JSONResponse(entry)

    @app.put("/api/workspaces/{ws}/deps")
    async def set_workspace_deps_endpoint(ws: str, req: WorkspaceDepsRequest) -> JSONResponse:
        try:
            entry = workspaces_mod.set_workspace_deps(settings, ws, req.deps)
        except ValueError as exc:
            return _ws_error(exc)  # unknown slug / self-dep / cycle -> 400 with the message
        return JSONResponse(entry)

    @app.post("/api/workspaces/{ws}/run")
    async def run_workspace(ws: str, req: WorkspaceRunRequest):
        try:
            spec = workspaces_mod.workspace_run_spec(settings, ws, subset=req.subset)
        except ValueError as exc:
            return _ws_error(exc)
        if not spec["projects"]:
            return JSONResponse({"error": f"workspace '{ws}' has no member projects"},
                                status_code=400)
        # Re-enter the exact multi-project run path (validation, payload, enqueue,
        # pump) — one member collapses to a plain single-project run, same as /api/run.
        return await start_run(RunRequest(
            prompt=req.prompt, title=req.title, effort=req.effort, model=req.model,
            budget=req.budget, projects=spec["projects"],
            project_deps=(spec["deps"] or None), workspace=ws))

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

    @app.post("/api/tasks/{task_id}/deliver")
    async def deliver_task(task_id: str) -> JSONResponse:
        """Accept all & deliver: merge the whole task branch into the review target.

        One-click completion of the review — per-subtask decisions stay available
        beforehand; this records every undecided subtask as accepted. Idempotent
        (an already-delivered branch merges as up-to-date)."""
        row = app.state.runs.get(task_id)
        if row is None:
            return JSONResponse({"error": "unknown task"}, status_code=404)
        status = row.get("status") or ""
        if status not in _TERMINAL_STATUSES:
            return JSONResponse(
                {"error": f"run is still {status or 'active'} — deliver after it finishes"},
                status_code=409)
        branch = str(row.get("task_branch") or "").strip()
        if not branch:
            return JSONResponse({"error": "task has no delivery branch"}, status_code=409)
        if not hasattr(projects, "deliver_branch"):
            return JSONResponse({"error": "deliver is not available yet"}, status_code=501)
        slug = _project_of_run(row)
        title = str(row.get("title") or task_id)
        res = await asyncio.to_thread(
            projects.deliver_branch, settings, slug, branch,
            message=f"ada: deliver task {task_id} ({title[:60]})")
        if res.get("conflict"):
            return JSONResponse({**res, "error": "merge conflict"}, status_code=409)
        if not res.get("merged"):
            return JSONResponse({**res, "error": res.get("error") or "merge failed"},
                                status_code=409)
        accepted = []
        try:
            states = app.state.runs.get_subtask_states(task_id) or {}
            reviews = app.state.runs.get_subtask_reviews(task_id) or {}
            for sid in states:
                if sid not in reviews:
                    app.state.runs.set_subtask_review(task_id, sid, "accepted",
                                                      "accepted via deliver-all")
                    accepted.append(sid)
        except Exception:  # noqa: BLE001 - decision bookkeeping is best-effort
            pass
        return JSONResponse({**res, "decision": "accepted", "accepted_subtasks": accepted})

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
        # projects.accept_subtask records the cherry-pick sha (accepted_commit) on the
        # review row — which is what rollback later reverts. Older trees fall back to
        # accept_commit / vcs.cherry_pick_merge, and with none landed the action is 501.
        use_accept_subtask = hasattr(projects, "accept_subtask")
        use_accept = hasattr(projects, "accept_commit")
        if not ((use_accept_subtask or use_accept or hasattr(vcs, "cherry_pick_merge"))
                and hasattr(app.state.runs, "set_subtask_review")):
            return JSONResponse(
                {"error": "per-subtask acceptance is not available yet"}, status_code=501)
        slug = _project_of_run(row)
        try:
            if use_accept_subtask:
                res = await asyncio.to_thread(
                    projects.accept_subtask, settings, slug, task_id, subtask_id)
            elif use_accept:
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
        if not use_accept_subtask:  # the wrapper already recorded decision + sha
            app.state.runs.set_subtask_review(task_id, subtask_id, "accepted", "")
        return JSONResponse({**res, "decision": "accepted"})

    @app.post("/api/runs/{task_id}/subtasks/{subtask_id}/rollback")
    async def rollback_subtask(task_id: str, subtask_id: str) -> JSONResponse:
        """Revert a previously accepted subtask's commit on the review target.

        projects.rollback_accept owns the git work and the bookkeeping (decision
        'rolled_back' + rollback_commit); its dict comes back verbatim — 200 on
        ok, 409 when there is nothing to roll back or the revert conflicts."""
        row = app.state.runs.get(task_id)
        if row is None:
            return JSONResponse({"error": "unknown task"}, status_code=404)
        if not hasattr(projects, "rollback_accept"):
            return JSONResponse({"error": "rollback is not available yet"}, status_code=501)
        slug = _project_of_run(row)
        res = await asyncio.to_thread(
            projects.rollback_accept, settings, slug, task_id, subtask_id)
        if not res.get("ok"):
            return JSONResponse(res, status_code=409)
        b = app.state.brokers.get(task_id)
        if b is not None:  # a live viewer sees the review state change immediately
            b.publish(Event("control", f"Subtask {subtask_id} rolled back.",
                            {"subtask": subtask_id, "decision": "rolled_back",
                             "rollback_commit": res.get("rollback_commit")}))
        return JSONResponse(res)

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

    # ---- Knowledge graph v2: layered/weighted views over the SAME per-project KG
    # file /api/graph reads (settings.graph_path for the resolved project slug),
    # opened read-only — these endpoints never call save().
    _KG_LAYERS = {"domain", "run"}

    def _open_kg(slug: str) -> NetworkXKnowledgeGraph:
        s = dataclasses.replace(settings, project=projects.resolve(settings, slug))
        return NetworkXKnowledgeGraph(s.graph_path)

    def _kg_layer(layer: str | None) -> str | None:
        layer = (layer or "").strip().lower()
        return layer if layer in _KG_LAYERS else None  # anything else = all layers

    @app.get("/api/projects/{slug}/graph2")
    async def get_graph2(slug: str, layer: str | None = None, min_weight: int = 1,
                         limit: int = 250) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)

        def _view() -> dict[str, Any]:
            kg = _open_kg(slug)
            view = kg.export_view(layer=_kg_layer(layer),
                                  min_weight=max(1, int(min_weight)),
                                  limit_nodes=max(1, min(int(limit), 2000)))
            return {"project": slug, **view, "stats": kg.stats()}

        return JSONResponse(await asyncio.to_thread(_view))

    @app.get("/api/projects/{slug}/graph2/search")
    async def graph2_search(slug: str, q: str = "", limit: int = 20) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        query = (q or "").strip()
        if not query:
            return JSONResponse({"project": slug, "query": "", "nodes": []})
        nodes = await asyncio.to_thread(
            lambda: _open_kg(slug).search_nodes(query, limit=max(1, min(int(limit), 200))))
        return JSONResponse({"project": slug, "query": query, "nodes": nodes})

    # :path so canonical node ids containing "/" (file paths) resolve.
    @app.get("/api/projects/{slug}/graph2/node/{node_id:path}")
    async def graph2_node(slug: str, node_id: str, depth: int = 1,
                          layer: str | None = None) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        hood = await asyncio.to_thread(
            lambda: _open_kg(slug).neighborhood(
                node_id, depth=max(0, min(int(depth), 5)), layer=_kg_layer(layer)))
        return JSONResponse({"project": slug, "node": node_id, **hood})

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

    # ---- Memory curation: list / edit / forget a project's memories ----
    @app.get("/api/projects/{slug}/memories")
    async def list_memories_endpoint(slug: str, scope: str | None = None,
                                     limit: int = 200, offset: int = 0) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        rows = await asyncio.to_thread(
            list_project_memories, settings, slug, scope=scope, limit=limit, offset=offset)
        total = await asyncio.to_thread(count_project_memories, settings, slug, scope=scope)
        return JSONResponse({"project": slug, "total": total, "memories": rows})

    @app.patch("/api/projects/{slug}/memories/{mem_id}")
    async def update_memory_endpoint(slug: str, mem_id: int,
                                     req: MemoryUpdateRequest) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        content = (req.content or "").strip()
        if not content:
            return JSONResponse({"error": "content must not be empty"}, status_code=400)
        ok = await asyncio.to_thread(update_project_memory, settings, slug, mem_id, content)
        if not ok:
            return JSONResponse({"error": "unknown memory"}, status_code=404)
        return JSONResponse({"ok": True, "id": mem_id, "content": content})

    @app.delete("/api/projects/{slug}/memories/{mem_id}")
    async def delete_memory_endpoint(slug: str, mem_id: int) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        ok = await asyncio.to_thread(delete_project_memory, settings, slug, mem_id)
        if not ok:
            return JSONResponse({"error": "unknown memory"}, status_code=404)
        return JSONResponse({"ok": True, "id": mem_id})

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

    # =====================================================================
    # Feature wave: playbooks, schedules, search, analytics, KB upload,
    # GitHub poller, A/B replay. All under /api/* (auth middleware applies).
    # =====================================================================

    # ---- Playbooks: parameterized task templates -> the normal run pipeline ----
    @app.get("/api/playbooks")
    async def list_playbooks() -> JSONResponse:
        return JSONResponse(playbooks.catalog())

    @app.post("/api/playbooks/{pid}/run")
    async def run_playbook(pid: str, req: PlaybookRunRequest):
        try:
            rendered = playbooks.render(pid, req.params or {})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        task_id = new_task_id()
        title = (req.title or "").strip() or rendered["title"]
        app.state.brokers[task_id] = Broker()
        app.state.brokers[task_id].publish(Event("status", "Backend: " + settings.llm_backend,
                                                 {"backend": settings.llm_backend}))
        # Same shape/path as /api/run's single-project payload, plus the playbook's
        # settings_overrides, which _start/_settings_for thread onto the run.
        payload: dict[str, Any] = {
            "prompt": rendered["prompt"], "plan": None, "effort": req.effort,
            "model": req.model, "budget": req.budget, "title": title,
            "project": req.project, "memory_scope": None, "continue_from": None,
            "git_finalize": None, "settings_overrides": rendered["settings_overrides"],
        }
        app.state.runs.enqueue(task_id, rendered["prompt"], title, payload)
        _pump()  # auto-run if a slot is free
        status = "running" if task_id in app.state.running else "queued"
        return {"task_id": task_id, "status": status, "title": title,
                "position": app.state.runs.queue_positions().get(task_id)}

    # ---- Schedules: recurring per-project runs ----
    def _schedule_row(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "next_run_at": next_run_at(row)}

    @app.get("/api/schedules")
    async def list_schedules(project: str | None = None) -> JSONResponse:
        return JSONResponse([_schedule_row(r) for r in app.state.schedules.list(project)])

    @app.post("/api/schedules")
    async def create_schedule(req: ScheduleCreateRequest) -> JSONResponse:
        try:
            row = app.state.schedules.create(
                project=projects.resolve(settings, req.project), prompt=req.prompt,
                title=(req.title or None), every_hours=req.every_hours,
                cron=req.cron, budget_usd=float(req.budget_usd or 0.0))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_schedule_row(row))

    @app.patch("/api/schedules/{sid}")
    async def patch_schedule(sid: str, req: SchedulePatchRequest) -> JSONResponse:
        fields = req.model_dump(exclude_unset=True)
        try:
            row = app.state.schedules.update(sid, **fields)
        except KeyError:
            return JSONResponse({"error": f"unknown schedule: {sid}"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_schedule_row(row))

    @app.delete("/api/schedules/{sid}")
    async def delete_schedule(sid: str) -> JSONResponse:
        if app.state.schedules.get(sid) is None:
            return JSONResponse({"error": f"unknown schedule: {sid}"}, status_code=404)
        app.state.schedules.delete(sid)
        return JSONResponse({"ok": True})

    async def _schedules_tick(now: float | None = None) -> int:
        """One scheduler pass: enqueue a run per due schedule, then mark_started
        (which advances the interval, so due() stays idempotent). Returns how many
        runs were enqueued. Failures are logged and skipped, never raised."""
        started = 0
        try:
            due = await asyncio.to_thread(app.state.schedules.due, now)
        except Exception as exc:  # noqa: BLE001
            print(f"[ai-dev-assistant] schedules: due() failed: {exc}", flush=True)
            return 0
        for row in due:
            sid = row.get("id")
            try:
                task_id = new_task_id()
                title = row.get("title") or derive_title(row.get("prompt") or "")
                payload = {
                    "prompt": row.get("prompt") or "", "plan": None, "effort": None,
                    "model": None, "budget": (row.get("budget_usd") or None),
                    "title": title, "project": row.get("project"), "memory_scope": None,
                    "continue_from": None, "git_finalize": None,
                }
                app.state.brokers[task_id] = Broker()
                app.state.runs.enqueue(task_id, payload["prompt"], title, payload)
                app.state.schedules.mark_started(sid, task_id, now)
                started += 1
            except Exception as exc:  # noqa: BLE001 — one bad schedule must not stop the rest
                print(f"[ai-dev-assistant] schedules: {sid} failed to start: {exc}", flush=True)
        if started:
            _pump()
        return started

    app.state.schedules_tick = _schedules_tick

    # ---- Global search ----
    @app.get("/api/search")
    async def global_search_endpoint(
            q: str = "", kinds: str | None = None,
            projects_csv: str | None = Query(None, alias="projects"),
            limit: int = 30) -> JSONResponse:
        query = (q or "").strip()
        if not query:
            return JSONResponse([])
        kind_tuple = (tuple(k.strip() for k in (kinds or "").split(",") if k.strip())
                      or ("task", "memory", "kb", "file"))
        proj_filter = None
        if projects_csv is not None and projects_csv.strip():
            proj_filter = [s.strip() for s in projects_csv.split(",") if s.strip()]
        hits = await asyncio.to_thread(
            search.global_search, app.state.base_settings, query,
            kinds=kind_tuple, projects_filter=proj_filter,
            limit=max(1, min(int(limit), 100)))
        return JSONResponse(hits)

    # ---- Spend analytics (read-only over runs.db + events.jsonl) ----
    @app.get("/api/analytics/overview")
    async def analytics_overview(days: int = 30) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(
            analytics.spend_overview, app.state.base_settings, days=max(1, int(days))))

    @app.get("/api/analytics/outcomes")
    async def analytics_outcomes(days: int = 90) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(
            analytics.cost_per_outcome, app.state.base_settings, days=max(1, int(days))))

    @app.get("/api/analytics/project/{slug}")
    async def analytics_project(slug: str, days: int = 90) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(
            analytics.project_spend, app.state.base_settings, slug, days=max(1, int(days))))

    @app.get("/api/analytics/run/{task_id}")
    async def analytics_run(task_id: str) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(
            analytics.run_cost_breakdown, app.state.base_settings, task_id))

    # ---- KB upload: one text file -> the project's knowledge base ----
    @app.post("/api/projects/{slug}/kb/upload")
    async def kb_upload(slug: str, file: UploadFile = File(...)) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        raw = await file.read()
        if len(raw) > _KB_UPLOAD_MAX_BYTES:
            return JSONResponse({"error": "file too large (2 MB cap)"}, status_code=413)
        text = raw.decode("utf-8", errors="replace")
        name = Path(file.filename or "upload.txt").name or "upload.txt"

        def _ingest() -> int:
            # Build the per-project KB exactly like the engine does:
            # MemoryStore(settings-for-slug).vectors -> KnowledgeBase.
            s = dataclasses.replace(app.state.base_settings,
                                    project=projects.resolve(settings, slug))
            store = MemoryStore(s)
            try:
                # reingest: re-uploading a file replaces its chunks, never duplicates.
                return KnowledgeBase(store.vectors).reingest(f"upload:{name}", text)
            finally:
                store.close()

        chunks = await asyncio.to_thread(_ingest)
        return JSONResponse({"chunks": chunks})

    # ---- Workspace GC: dry-run report + explicit cleanup (nothing is automatic;
    # persist-for-review stays the default — gc_keep_days is only the retention
    # these two explicit actions use) ----
    @app.get("/api/projects/{slug}/gc")
    async def gc_report(slug: str, keep_days: int | None = None) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        keep = max(0, int(keep_days if keep_days is not None
                          else app.state.base_settings.gc_keep_days))
        report = await asyncio.to_thread(
            workspace_gc.cleanup_report, app.state.base_settings, slug, keep)
        return JSONResponse({**report, "keep_days": keep})

    @app.post("/api/projects/{slug}/gc")
    async def gc_cleanup(slug: str, req: GcRequest) -> JSONResponse:
        if not _project_known(slug):
            return JSONResponse({"error": "unknown project"}, status_code=404)
        keep = max(0, int(req.keep_days if req.keep_days is not None
                          else app.state.base_settings.gc_keep_days))
        res = await asyncio.to_thread(
            workspace_gc.cleanup, app.state.base_settings, slug, keep, req.ids)
        return JSONResponse({**res, "keep_days": keep})

    # ---- GitHub poller: labeled issues -> runs; finished runs -> PRs ----
    _gh_state_path = settings.data_dir / "github_seen.json"

    def _gh_load_state() -> dict[str, Any]:
        try:
            data = json.loads(_gh_state_path.read_text())
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            return {"seen": [], "tracked": {}, "pr_seen": {}}
        seen = data.get("seen")
        tracked = data.get("tracked")
        # pr_seen ("owner/repo#number" -> last-seen comment created_at) is additive:
        # files written before PR follow-ups landed simply have none.
        pr_seen = data.get("pr_seen")
        return {"seen": [str(m) for m in seen] if isinstance(seen, list) else [],
                "tracked": dict(tracked) if isinstance(tracked, dict) else {},
                "pr_seen": ({str(k): str(v) for k, v in pr_seen.items()}
                            if isinstance(pr_seen, dict) else {})}

    def _gh_save_state(state: dict[str, Any]) -> None:
        try:
            _gh_state_path.parent.mkdir(parents=True, exist_ok=True)
            _gh_state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            print(f"[ai-dev-assistant] github: could not persist state: {exc}", flush=True)

    def _github_config() -> github.GitHubConfig:
        """Repos + label from the LIVE settings; the token is ENV-ONLY by design
        (never in the schema, never returned by any endpoint)."""
        live = app.state.base_settings
        token = (os.getenv("ADA_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
        return github.GitHubConfig(
            token=token, label=(live.github_label or "").strip() or "ada",
            repo_map=github._parse_repo_map(live.github_repos or ""))

    @app.get("/api/github/status")
    async def github_status() -> JSONResponse:
        cfg = _github_config()
        state = _gh_load_state()
        return JSONResponse({
            "enabled": cfg.enabled, "repos": dict(cfg.repo_map), "label": cfg.label,
            "tracked": len(state["tracked"]), "seen": len(state["seen"]),
        })

    async def _github_finish(client: github.GitHubClient, task_id: str,
                             ref: dict[str, Any], row: dict[str, Any]) -> None:
        """A tracked run reached a terminal status: push its branch, open a PR with
        the run's evidence, and comment the link back on the issue. Every failure
        is logged and skipped — the poll loop must never crash."""
        repo = str(ref.get("repo") or "")
        number = ref.get("number")
        try:
            branch = str(row.get("task_branch") or "").strip()
            if not (repo and branch):
                return  # nothing deliverable (e.g. run failed before branching)
            slug = _project_of_run(row)
            root = _project_root(slug)
            if root:
                res = await asyncio.to_thread(github.push_branch, Path(root), branch)
                if not res.get("pushed"):
                    print(f"[ai-dev-assistant] github: push of {branch} failed for "
                          f"{task_id}: {res.get('error')}", flush=True)
            base = str(row.get("review_target") or "").strip() or _review_target_for(slug)
            body = github.pr_body({
                "tldr": row.get("summary") or "", "branch": branch,
                "tests": row.get("tests") or "",
                "quality_score": row.get("quality_score"),
                "cost_usd": row.get("cost_usd"),
            })
            title = str(row.get("title") or "") or derive_title(row.get("prompt") or "")
            pr = await asyncio.to_thread(client.open_pr, repo, head=branch, base=base,
                                         title=title, body=body)
            if pr is None:
                print(f"[ai-dev-assistant] github: open_pr failed for {task_id}", flush=True)
            elif number is not None:
                await asyncio.to_thread(
                    client.comment, repo, number,
                    f"ADA finished task {task_id}: {pr.get('url') or 'PR opened'}")
        except Exception as exc:  # noqa: BLE001
            print(f"[ai-dev-assistant] github: completion of {task_id} failed: {exc}",
                  flush=True)

    async def _github_tick() -> None:
        """One poll cycle: new labeled issues become runs (deduped via the persisted
        seen-set); tracked runs that finished get a pushed branch + PR + comment."""
        cfg = _github_config()
        if not cfg.enabled:
            return
        client = github.GitHubClient(cfg, transport=app.state.github_transport)
        state = _gh_load_state()
        seen = set(state["seen"])
        tracked: dict[str, Any] = state["tracked"]
        pr_seen: dict[str, str] = state["pr_seen"]
        dirty = False
        started = 0
        for repo, slug in cfg.repo_map.items():
            issues = await asyncio.to_thread(client.list_labeled_issues, repo)
            for issue in issues:
                marker = github.seen_marker(issue)
                if not marker or marker in seen:
                    continue
                prompt = github.issue_to_prompt(issue)
                if not prompt:
                    seen.add(marker)  # junk issue — never retry it
                    dirty = True
                    continue
                task_id = new_task_id()
                title = str(issue.get("title") or "").strip() or derive_title(prompt)
                payload = {
                    "prompt": prompt, "plan": None, "effort": None, "model": None,
                    "budget": None, "title": title, "project": slug,
                    "memory_scope": None, "continue_from": None, "git_finalize": None,
                }
                try:
                    app.state.brokers[task_id] = Broker()
                    app.state.runs.enqueue(task_id, prompt, title, payload)
                except Exception as exc:  # noqa: BLE001
                    print(f"[ai-dev-assistant] github: enqueue for {marker} failed: {exc}",
                          flush=True)
                    continue
                seen.add(marker)
                tracked[task_id] = {"repo": repo, "number": issue.get("number")}
                dirty = True
                started += 1
                await asyncio.to_thread(client.comment, repo, issue.get("number"),
                                        f"ADA started task {task_id}")
        # Follow-up pass (opt-in via github_pr_followups): new reviewer comments on
        # the assistant's own open PRs (ada/* head branches) become follow-up runs.
        # A branch named ada/<task-id> of a known run RE-ENGAGES that task — the
        # payload's continue_from is the same lineage path POST /api/run uses
        # (set_parent + workspace/context continuation); otherwise the follow-up is
        # a fresh run on the repo's mapped project. Dedupe: pr_seen keeps the last
        # comment timestamp per PR, and list_pr_comments(since=...) is strict.
        if app.state.base_settings.github_pr_followups:
            for repo, slug in cfg.repo_map.items():
                prs = await asyncio.to_thread(client.list_open_prs, repo)
                for pr in prs:
                    if not github.is_own_pr(pr):
                        continue
                    number = pr.get("number")
                    if number is None:
                        continue
                    key = f"{repo}#{number}"
                    comments = await asyncio.to_thread(
                        client.list_pr_comments, repo, number, pr_seen.get(key))
                    if not comments:
                        continue
                    latest = max(str(c.get("created_at") or "") for c in comments)
                    prompt = github.comments_to_followup(pr, comments)
                    if not prompt:  # junk batch — advance the cursor, never retry it
                        if latest:
                            pr_seen[key] = latest
                            dirty = True
                        continue
                    branch = str(pr.get("head") or "")
                    parent = branch.removeprefix(github.OWN_BRANCH_PREFIX)
                    parent_row = app.state.runs.get(parent) if parent else None
                    continue_from = parent if parent_row is not None else None
                    proj = _project_of_run(parent_row) if parent_row is not None else slug
                    title = f"Follow-up: {str(pr.get('title') or '').strip() or branch}"
                    task_id = new_task_id()
                    payload = {
                        "prompt": prompt, "plan": None, "effort": None, "model": None,
                        "budget": None, "title": title, "project": proj,
                        "memory_scope": None, "continue_from": continue_from,
                        "git_finalize": None,
                    }
                    try:
                        app.state.brokers[task_id] = Broker()
                        app.state.runs.enqueue(task_id, prompt, title, payload)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[ai-dev-assistant] github: follow-up enqueue for {key} "
                              f"failed: {exc}", flush=True)
                        continue
                    if latest:
                        pr_seen[key] = latest
                    dirty = True
                    started += 1
        # Completion pass: finished tracked runs -> branch push + PR, then untrack.
        for task_id in list(tracked):
            ref = tracked.get(task_id) or {}
            row = app.state.runs.get(task_id)
            if row is None:  # run row deleted — nothing left to deliver
                tracked.pop(task_id, None)
                dirty = True
                continue
            if (row.get("status") or "") not in _TERMINAL_STATUSES:
                continue
            await _github_finish(client, task_id, ref, row)
            tracked.pop(task_id, None)
            dirty = True
        if dirty:
            _gh_save_state({"seen": sorted(seen), "tracked": tracked, "pr_seen": pr_seen})
        if started:
            _pump()

    app.state.github_tick = _github_tick

    # ---- A/B knob comparison (offline replay smoke) ----
    @app.post("/api/ab")
    async def run_ab_endpoint(req: ABRequest) -> JSONResponse:
        from ..evals.ab import run_ab
        try:
            report = await asyncio.to_thread(
                run_ab, app.state.base_settings, req.knob, list(req.values), replay=True)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(report.to_dict())

    # ---- Background loop lifecycle: started at startup, cancelled at shutdown ----
    async def _periodic(interval: float, tick) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop itself never dies
                print(f"[ai-dev-assistant] background tick failed: {exc}", flush=True)

    @app.on_event("startup")
    async def _start_background_loops() -> None:
        app.state.bg_tasks = [
            asyncio.create_task(_periodic(_SCHEDULES_TICK_S, _schedules_tick)),
            asyncio.create_task(_periodic(_GITHUB_TICK_S, _github_tick)),
        ]

    @app.on_event("shutdown")
    async def _stop_background_loops() -> None:
        for t in app.state.bg_tasks:
            t.cancel()
        app.state.bg_tasks = []

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    async def landing() -> FileResponse:
        # Public product page; the console shell lives at /app. Data stays behind
        # the /api/* auth middleware either way, so both shells are safe to serve.
        target = _STATIC / "landing.html"
        if not target.is_file():  # older static bundles: fall back to the console
            target = _STATIC / "index.html"
        return FileResponse(str(target))

    @app.get("/app")
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
