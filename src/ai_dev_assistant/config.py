"""Runtime configuration, loaded from the environment (.env supported).

Everything tunable lives here so the rest of the code never reads os.environ
directly. Construct once via ``Settings.load()`` and pass it down.

Precedence: ``Settings.load()`` first resolves every field from the environment
(falling back to the dataclass defaults), then applies the JSON overlay written
by the global settings console (``<data_dir>/settings.json``). For any key the
console has overridden, the console value WINS over the environment; deleting
the override reverts the key to its env/default value. Only keys whitelisted in
``SETTINGS_SCHEMA`` are ever read from the overlay — secrets, paths and backend
identity can only come from the environment.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val not in (None, "") else default


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val in (None, ""):
        return default
    return val.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    # --- LLM backend ---
    # "claude_sdk" = Claude Agent SDK (uses your Claude Code login; no API key needed)
    # "anthropic"  = raw Anthropic API (needs ANTHROPIC_API_KEY)
    llm_backend: str = "claude_sdk"
    sdk_model: str = ""  # optional model override for the SDK backend ("" = Claude Code default)

    # --- Anthropic ---
    anthropic_api_key: str = ""
    orchestrator_model: str = "claude-opus-4-8"
    agent_model: str = "claude-opus-4-8"
    orchestrator_effort: str = "high"
    agent_effort: str = "medium"
    reviewer_effort: str = "high"

    # --- LLM resilience (Tier 1) ---
    request_timeout: float = 120.0  # per-LLM-call HTTP timeout (seconds)
    llm_max_retries: int = 3        # bounded backoff retries on transient 429/5xx/timeout

    # --- Session pool ---
    max_concurrent_runs: int = 1    # how many queued tasks execute at once (1 = serial)
    max_concurrent_sessions: int = 4
    session_idle_ttl: float = 30.0
    reaper_interval: float = 5.0
    max_retries: int = 1

    # --- Reliability (Tier 1) ---
    degrade_on_partial: bool = True  # a subtask that produced real output but failed review
                                     # becomes PASSED_WITH_CAVEATS so dependents still run

    # --- Verification (Tier 3) ---
    verify_run_tests: bool = True   # run generated tests in the workspace after a task
    verify_timeout: float = 120.0
    objective_review: bool = True   # gate verdicts on file contents + per-subtask tests
    lint_check: bool = True         # include ruff/pyflakes signal in the objective gate

    # --- Planning (Tier 3) ---
    adaptive_replan: bool = True    # amend the DAG between batches (bounded self-heal of
                                    # failed subtasks); on by default
    max_plan_subtasks: int = 24     # reject a plan larger than this (hallucination guard)

    # --- Cost guardrail ---
    budget_usd: float = 0.0         # 0 = no cap; otherwise stop scheduling new work past this
    agent_max_turns: int = 24       # per-agent tool-loop cap (lower = cheaper)
    subtask_max_seconds: float = 1800.0  # wall-clock cap per subtask attempt (0 = no cap)
    attention_timeout: float = 600.0    # how long an agent waits for an operator answer
                                        # (counts toward the subtask wall-clock cap)

    # --- Real-repo binding (programmatic only; per-run env binding was removed in
    # favor of projects — see docs/PLAN.md. The eval harness still constructs
    # Settings(repo_path=...) directly for fixture repos.) ---
    repo_url: str = ""
    repo_path: str = ""
    repo_ref: str = ""
    git_finalize: bool = False      # at the end, commit the workspace on a new branch
    git_branch_prefix: str = "ada/"
    worktree_per_subtask: bool = False  # isolate parallel subtasks in git worktrees, merge on pass
    git_mode: str = "branch"        # project outcome: "branch" (review) | "merge" (auto on pass)
    protected_paths: tuple[str, ...] = ()  # globs agents' write tools must refuse (policy-driven)

    # --- Sandbox / execution safety (Tier 2/5) ---
    # Isolation tier for the shell commands agents run (execution.py consumes
    # these via configure_sandbox at run start; the ADA_SANDBOX* env vars remain
    # the fallback for bare callers).
    sandbox: str = "subprocess"     # "subprocess" | "bwrap" | "container" | "none"
    sandbox_image: str = ""         # container-tier image ("" = per-command default)
    sandbox_allow_network: bool = False  # bwrap/container commands may reach the network
    sandbox_cpu_seconds: int = 60   # CPU-time rlimit for sandboxed commands
    sandbox_mem_mb: int = 1024      # address-space rlimit (MB) for sandboxed commands
    allow_run_command: bool = True  # expose the run_command/install_packages agent tools
    allow_web: bool = False         # allow built-in WebSearch/WebFetch in the SDK agent loop
    allowed_builtins: str = "Read,Write,Edit,Glob,Grep,LS"  # SDK built-in tool allowlist

    # --- Defense in depth (Tier 5) ---
    redact_secrets: bool = True     # scrub secret-shaped strings from tool output/docs/memory
    audit_log: bool = True          # append every tool dispatch to an audit log

    # --- Observability (Tier 5) ---
    trace: bool = True              # record an LLM/tool span log per run
    record_dir: str = ""           # if set, record provider calls as replay cassettes here
    replay_dir: str = ""           # if set, server provider calls from cassettes here (offline)

    # --- Notifications (web server builds NotifyConfig from these at event time;
    # the SMTP password is ENV-ONLY: ADA_SMTP_PASSWORD, read by notify.py at send
    # time — it is deliberately not a Settings field) ---
    notify_webhook: str = ""        # JSON POST target for ask/permission/done/error pings
    notify_slack_webhook: str = ""  # Slack incoming-webhook URL ({"text": ...} payloads)
    notify_desktop: bool = False    # macOS banner via osascript (darwin only)
    notify_events: str = ""         # csv of event types; blank = ask,permission,done,error
    notify_email_to: str = ""       # recipient address; blank disables the email channel
    notify_smtp_host: str = ""      # SMTP server; blank disables the email channel
    notify_smtp_port: int = 587
    notify_smtp_user: str = ""      # SMTP login (also the From address when set)
    notify_smtp_starttls: bool = True

    # --- GitHub poller (token is ENV-ONLY: ADA_GITHUB_TOKEN / GITHUB_TOKEN) ---
    github_repos: str = ""          # "owner/repo=project-slug,..." mapping the poller watches
    github_label: str = "ada"       # only open issues with this label become tasks

    # --- Storage / embeddings ---
    embeddings_backend: str = "fastembed"  # "fastembed" | "hash"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    data_dir: Path = Path(".ada_data")
    docs_dir: Path = Path("docs")
    # --- Scoping: memory can be global or per-project; the knowledge graph is per-project only.
    project: str = "default"          # active project slug
    memory_scope: str = "project"     # where new memories are written: "project" | "global"
    # Sandbox for agent file-writes (the Claude SDK's built-in tools run here, not in the
    # project source tree).
    workspace_dir: Path = Path("workspace")

    @classmethod
    def load(cls, *, overlay: bool = True) -> "Settings":
        """Resolve settings from the environment, then apply the settings-console
        overlay (``<data_dir>/settings.json``) — console values win for keys it
        has overridden. ``overlay=False`` returns the pure env/default view."""
        s = cls(
            llm_backend=_get("ADA_LLM_BACKEND", "claude_sdk"),
            sdk_model=_get("ADA_SDK_MODEL", ""),
            anthropic_api_key=_get("ANTHROPIC_API_KEY", ""),
            orchestrator_model=_get("ADA_ORCHESTRATOR_MODEL", "claude-opus-4-8"),
            agent_model=_get("ADA_AGENT_MODEL", "claude-opus-4-8"),
            orchestrator_effort=_get("ADA_ORCHESTRATOR_EFFORT", "high"),
            agent_effort=_get("ADA_AGENT_EFFORT", "medium"),
            reviewer_effort=_get("ADA_REVIEWER_EFFORT", "high"),
            request_timeout=_float("ADA_REQUEST_TIMEOUT", 120.0),
            llm_max_retries=_int("ADA_LLM_MAX_RETRIES", 3),
            max_concurrent_runs=_int("ADA_MAX_CONCURRENT_RUNS", 1),
            max_concurrent_sessions=_int("ADA_MAX_CONCURRENT_SESSIONS", 4),
            session_idle_ttl=_float("ADA_SESSION_IDLE_TTL", 30.0),
            reaper_interval=_float("ADA_REAPER_INTERVAL", 5.0),
            max_retries=_int("ADA_MAX_RETRIES", 1),
            degrade_on_partial=_bool("ADA_DEGRADE_ON_PARTIAL", True),
            verify_run_tests=_get("ADA_VERIFY_RUN_TESTS", "true").lower() not in ("0", "false", "no"),
            verify_timeout=_float("ADA_VERIFY_TIMEOUT", 120.0),
            objective_review=_bool("ADA_OBJECTIVE_REVIEW", True),
            lint_check=_bool("ADA_LINT_CHECK", True),
            adaptive_replan=_bool("ADA_ADAPTIVE_REPLAN", True),
            max_plan_subtasks=_int("ADA_MAX_PLAN_SUBTASKS", 24),
            budget_usd=_float("ADA_BUDGET_USD", 0.0),
            agent_max_turns=_int("ADA_AGENT_MAX_TURNS", 24),
            subtask_max_seconds=_float("ADA_SUBTASK_MAX_SECONDS", 1800.0),
            attention_timeout=_float("ADA_ATTENTION_TIMEOUT", 600.0),
            git_finalize=_bool("ADA_GIT_FINALIZE", False),
            git_branch_prefix=_get("ADA_GIT_BRANCH_PREFIX", "ada/"),
            worktree_per_subtask=_bool("ADA_WORKTREE_PER_SUBTASK", False),
            git_mode=_get("ADA_GIT_MODE", "branch"),
            protected_paths=tuple(p.strip() for p in _get("ADA_PROTECTED_PATHS", "").split(",")
                                  if p.strip()),
            sandbox=_get("ADA_SANDBOX", "subprocess"),
            sandbox_image=_get("ADA_SANDBOX_IMAGE", ""),
            # New name first; legacy ADA_SANDBOX_NET=1 (exact execution.py
            # semantics: only "1" means allow) stays honored as the fallback.
            sandbox_allow_network=_bool("ADA_SANDBOX_ALLOW_NETWORK",
                                        os.getenv("ADA_SANDBOX_NET") == "1"),
            sandbox_cpu_seconds=_int("ADA_SANDBOX_CPU_SECONDS", 60),
            sandbox_mem_mb=_int("ADA_SANDBOX_MEM_MB", 1024),
            allow_run_command=_bool("ADA_ALLOW_RUN_COMMAND", True),
            allow_web=_bool("ADA_ALLOW_WEB", False),
            allowed_builtins=_get("ADA_ALLOWED_BUILTINS", "Read,Write,Edit,Glob,Grep,LS"),
            redact_secrets=_bool("ADA_REDACT_SECRETS", True),
            audit_log=_bool("ADA_AUDIT_LOG", True),
            trace=_bool("ADA_TRACE", True),
            record_dir=_get("ADA_RECORD_DIR", ""),
            replay_dir=_get("ADA_REPLAY_DIR", ""),
            notify_webhook=_get("ADA_NOTIFY_WEBHOOK", ""),
            notify_slack_webhook=_get("ADA_NOTIFY_SLACK_WEBHOOK", ""),
            notify_desktop=_bool("ADA_NOTIFY_DESKTOP", False),
            notify_events=_get("ADA_NOTIFY_EVENTS", ""),
            notify_email_to=_get("ADA_NOTIFY_EMAIL_TO", ""),
            notify_smtp_host=_get("ADA_NOTIFY_SMTP_HOST", ""),
            notify_smtp_port=_int("ADA_NOTIFY_SMTP_PORT", 587),
            notify_smtp_user=_get("ADA_NOTIFY_SMTP_USER", ""),
            notify_smtp_starttls=_bool("ADA_NOTIFY_SMTP_STARTTLS", True),
            github_repos=_get("ADA_GITHUB_REPOS", ""),
            github_label=_get("ADA_GITHUB_LABEL", "ada"),
            embeddings_backend=_get("ADA_EMBEDDINGS_BACKEND", "fastembed"),
            embed_model=_get("ADA_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
            data_dir=Path(_get("ADA_DATA_DIR", ".ada_data")),
            docs_dir=Path(_get("ADA_DOCS_DIR", "docs")),
            project=_get("ADA_PROJECT", "default"),
            memory_scope=_get("ADA_MEMORY_SCOPE", "project"),
            workspace_dir=Path(_get("ADA_WORKSPACE_DIR", "workspace")),
        )
        return apply_overrides(s) if overlay else s

    @property
    def has_api_key(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def repo_backed(self) -> bool:
        """True when this run should operate on a real checked-out repository."""
        return bool(self.repo_url or self.repo_path)

    @property
    def allowed_builtins_list(self) -> list[str]:
        names = [n.strip() for n in (self.allowed_builtins or "").split(",") if n.strip()]
        if self.allow_web:
            names += ["WebSearch", "WebFetch"]
        return names

    def run_workspace(self, run_id: str) -> Path:
        """The per-run sandbox directory where agents write code."""
        return self.workspace_dir / run_id

    @property
    def requires_api_key(self) -> bool:
        """The Anthropic backend needs a key; the Claude SDK backend uses Claude Code login."""
        return self.llm_backend == "anthropic"

    @property
    def db_path(self) -> Path:
        """The active project's memory store."""
        return self.project_dir / "memory.db"

    @property
    def graph_path(self) -> Path:
        """The active project's knowledge graph (KG is project-scoped only)."""
        return self.project_dir / "knowledge_graph.json"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def project_dir(self) -> Path:
        return self.projects_dir / (self.project or "default")

    @property
    def global_db_path(self) -> Path:
        """The shared, cross-project memory store."""
        return self.data_dir / "global" / "memory.db"

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "projects.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.global_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)


# ===========================================================================
# Global settings console — JSON overlay + editable whitelist.
#
# The console persists edits to <data_dir>/settings.json; Settings.load()
# applies that overlay LAST, so console values win over the environment for
# the keys it has overridden (see the module docstring). SETTINGS_SCHEMA is
# the single source of truth for what the console may list and set.
#
# Deliberately EXCLUDED (never listed, never settable via the overlay):
# anthropic_api_key and any tokens/passwords (the SMTP password is env-only
# ADA_SMTP_PASSWORD and not even a Settings field), data_dir/docs_dir/workspace_dir,
# llm_backend, record_dir/replay_dir, protected_paths (project-level policy),
# git_branch_prefix.
# ===========================================================================

OVERRIDES_FILENAME = "settings.json"

_EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]

SETTINGS_SCHEMA: list[dict] = [
    # --- LLM & Models ---
    {"key": "sdk_model", "group": "LLM & Models", "label": "SDK model",
     "help": "Model override for the Claude SDK backend — blank keeps the Claude Code default.",
     "type": "str"},
    {"key": "orchestrator_model", "group": "LLM & Models", "label": "Orchestrator model",
     "help": "Model the orchestrator plans and reviews with (API backend).", "type": "str"},
    {"key": "agent_model", "group": "LLM & Models", "label": "Agent model",
     "help": "Model the specialist agents execute subtasks with (API backend).", "type": "str"},
    {"key": "orchestrator_effort", "group": "LLM & Models", "label": "Orchestrator effort",
     "help": "Reasoning effort for planning and review.", "type": "choice",
     "choices": _EFFORT_CHOICES},
    {"key": "agent_effort", "group": "LLM & Models", "label": "Agent effort",
     "help": "Reasoning effort for subtask execution.", "type": "choice",
     "choices": _EFFORT_CHOICES},
    {"key": "reviewer_effort", "group": "LLM & Models", "label": "Reviewer effort",
     "help": "Reasoning effort for verdict review.", "type": "choice",
     "choices": _EFFORT_CHOICES},
    {"key": "request_timeout", "group": "LLM & Models", "label": "Request timeout (s)",
     "help": "Per-LLM-call HTTP timeout in seconds.", "type": "float"},
    {"key": "llm_max_retries", "group": "LLM & Models", "label": "LLM max retries",
     "help": "Bounded backoff retries on transient 429/5xx/timeout.", "type": "int"},
    # --- Guardrails & Budget ---
    {"key": "budget_usd", "group": "Guardrails & Budget", "label": "Budget (USD)",
     "help": "Stop scheduling new work past this cost cap — 0 means no cap.", "type": "float"},
    {"key": "agent_max_turns", "group": "Guardrails & Budget", "label": "Agent max turns",
     "help": "Per-agent tool-loop cap — lower is cheaper.", "type": "int"},
    {"key": "subtask_max_seconds", "group": "Guardrails & Budget", "label": "Subtask max seconds",
     "help": "Wall-clock cap per subtask attempt — 0 means no cap.", "type": "float"},
    {"key": "attention_timeout", "group": "Guardrails & Budget", "label": "Attention timeout (s)",
     "help": "How long an agent waits for an operator answer (counts toward the subtask cap).",
     "type": "float"},
    {"key": "max_plan_subtasks", "group": "Guardrails & Budget", "label": "Max plan subtasks",
     "help": "Reject a plan larger than this (hallucination guard).", "type": "int"},
    {"key": "max_concurrent_runs", "group": "Guardrails & Budget", "label": "Max concurrent runs",
     "help": "How many queued tasks execute at once — 1 is serial.", "type": "int"},
    {"key": "max_concurrent_sessions", "group": "Guardrails & Budget",
     "label": "Max concurrent sessions",
     "help": "Agent-session pool size within a run.", "type": "int"},
    {"key": "max_retries", "group": "Guardrails & Budget", "label": "Max review retries",
     "help": "How many times a failed subtask is retried after review.", "type": "int"},
    # --- Execution & Safety ---
    {"key": "sandbox", "group": "Execution & Safety", "label": "Sandbox backend",
     "help": "Isolation tier for the shell commands agents run: subprocess scrubs the "
             "environment and applies rlimits; bwrap adds filesystem/network namespaces "
             "(Linux, needs bubblewrap); container runs each command in a throwaway Docker "
             "container; none disables sandboxing. If bwrap/docker is missing, commands fall "
             "back to the subprocess tier with a one-time warning. This sandboxes commands "
             "only — the Claude Agent SDK model loop stays on the host and keeps using your "
             "Claude Code login (no API key involved).", "type": "choice",
     "choices": ["subprocess", "bwrap", "container", "none"]},
    {"key": "sandbox_image", "group": "Execution & Safety", "label": "Sandbox image",
     "help": "Docker image for the container tier — blank picks a per-command default "
             "(python:3.12-slim for Python commands, debian:stable-slim otherwise).",
     "type": "str"},
    {"key": "sandbox_allow_network", "group": "Execution & Safety", "label": "Sandbox network",
     "help": "Let bwrap/container-sandboxed commands reach the network — off means "
             "--unshare-net / --network=none. The subprocess tier never blocks the network.",
     "type": "bool"},
    {"key": "sandbox_cpu_seconds", "group": "Execution & Safety", "label": "Sandbox CPU seconds",
     "help": "CPU-time rlimit for sandboxed commands.", "type": "int"},
    {"key": "sandbox_mem_mb", "group": "Execution & Safety", "label": "Sandbox memory (MB)",
     "help": "Address-space rlimit for sandboxed commands.", "type": "int"},
    {"key": "allow_run_command", "group": "Execution & Safety", "label": "Allow run_command",
     "help": "Expose the run_command / install_packages agent tools.", "type": "bool"},
    {"key": "allow_web", "group": "Execution & Safety", "label": "Allow web access",
     "help": "Allow built-in WebSearch/WebFetch in the SDK agent loop.", "type": "bool"},
    {"key": "redact_secrets", "group": "Execution & Safety", "label": "Redact secrets",
     "help": "Scrub secret-shaped strings from tool output, docs and memory.", "type": "bool"},
    {"key": "audit_log", "group": "Execution & Safety", "label": "Audit log",
     "help": "Append every tool dispatch to an audit log.", "type": "bool"},
    {"key": "git_mode", "group": "Execution & Safety", "label": "Git mode",
     "help": "Project outcome: leave a branch for review, or auto-merge on pass.",
     "type": "choice", "choices": ["branch", "merge"]},
    {"key": "worktree_per_subtask", "group": "Execution & Safety",
     "label": "Worktree per subtask",
     "help": "Isolate parallel subtasks in git worktrees, merged on pass.", "type": "bool"},
    # --- Verification ---
    {"key": "verify_run_tests", "group": "Verification", "label": "Run generated tests",
     "help": "Run generated tests in the workspace after a task.", "type": "bool"},
    {"key": "verify_timeout", "group": "Verification", "label": "Verify timeout (s)",
     "help": "Time budget for the post-task test run.", "type": "float"},
    {"key": "objective_review", "group": "Verification", "label": "Objective review",
     "help": "Gate verdicts on file contents plus per-subtask tests.", "type": "bool"},
    {"key": "lint_check", "group": "Verification", "label": "Lint check",
     "help": "Include ruff/pyflakes signal in the objective gate.", "type": "bool"},
    {"key": "degrade_on_partial", "group": "Verification", "label": "Degrade on partial",
     "help": "A subtask with real output that failed review passes with caveats "
             "so dependents still run.", "type": "bool"},
    {"key": "adaptive_replan", "group": "Verification", "label": "Adaptive replan",
     "help": "Let the orchestrator amend the plan DAG between batches — on by default; "
             "failed subtasks get a bounded repair attempt.", "type": "bool"},
    # --- Memory & Knowledge ---
    {"key": "memory_scope", "group": "Memory & Knowledge", "label": "Memory scope",
     "help": "Where new memories are written — recall always reads project + global.",
     "type": "choice", "choices": ["project", "global"]},
    {"key": "embeddings_backend", "group": "Memory & Knowledge", "label": "Embeddings backend",
     "help": "Vector backend for memory recall.", "type": "choice",
     "choices": ["fastembed", "hash"], "restart_required": True},
    {"key": "embed_model", "group": "Memory & Knowledge", "label": "Embedding model",
     "help": "Sentence-embedding model used by the fastembed backend.", "type": "str",
     "restart_required": True},
    # --- Observability ---
    {"key": "trace", "group": "Observability", "label": "Trace",
     "help": "Record an LLM/tool span log per run.", "type": "bool"},
    # --- Notifications ---
    {"key": "notify_webhook", "group": "Notifications", "label": "Webhook URL",
     "help": "POST a JSON ping to this http(s) URL when a run needs you or finishes — "
             "blank disables the webhook channel.", "type": "str"},
    {"key": "notify_desktop", "group": "Notifications", "label": "Desktop notifications",
     "help": "Show a macOS desktop banner on notify events (macOS only; ignored elsewhere).",
     "type": "bool"},
    {"key": "notify_slack_webhook", "group": "Notifications", "label": "Slack webhook URL",
     "help": "Slack incoming-webhook URL — posts a Slack-formatted message on notify "
             "events; blank disables the Slack channel.", "type": "str"},
    {"key": "notify_events", "group": "Notifications", "label": "Notify events",
     "help": "Comma-separated event types to notify on (ask, permission, done, error) — "
             "blank means all four.", "type": "str"},
    {"key": "notify_email_to", "group": "Notifications", "label": "Email to",
     "help": "Send a plain-text email to this address on notify events — blank disables "
             "the email channel (an SMTP host is required too).", "type": "str"},
    {"key": "notify_smtp_host", "group": "Notifications", "label": "SMTP host",
     "help": "SMTP server for the email channel — blank disables it.", "type": "str"},
    {"key": "notify_smtp_port", "group": "Notifications", "label": "SMTP port",
     "help": "SMTP server port (587 is the usual STARTTLS submission port).",
     "type": "int"},
    {"key": "notify_smtp_user", "group": "Notifications", "label": "SMTP user",
     "help": "SMTP login, also used as the From address. The password is never set "
             "here — it comes only from the ADA_SMTP_PASSWORD environment variable.",
     "type": "str"},
    {"key": "notify_smtp_starttls", "group": "Notifications", "label": "SMTP STARTTLS",
     "help": "Upgrade the SMTP connection with STARTTLS before authenticating.",
     "type": "bool"},
    # --- GitHub ---
    {"key": "github_repos", "group": "GitHub", "label": "Repo mapping",
     "help": "Comma-separated owner/repo=project-slug pairs the issue poller watches, "
             "e.g. 'acme/api=api-backend'. The token is never set here — it comes only "
             "from the ADA_GITHUB_TOKEN or GITHUB_TOKEN environment variable.",
     "type": "str"},
    {"key": "github_label", "group": "GitHub", "label": "Issue label",
     "help": "Only open issues carrying this label are turned into tasks.", "type": "str"},
]

_SCHEMA_BY_KEY: dict[str, dict] = {e["key"]: e for e in SETTINGS_SCHEMA}


def is_editable(key: str) -> bool:
    """True when the settings console may list/set this key."""
    return key in _SCHEMA_BY_KEY


def env_var_for(key: str) -> str:
    """The ADA_* environment variable backing a whitelisted settings key."""
    return "ADA_" + key.upper()


def load_overrides(data_dir: Path) -> dict:
    """The console's overlay dict from <data_dir>/settings.json — {} on missing/corrupt."""
    path = Path(data_dir) / OVERRIDES_FILENAME
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_overrides(data_dir: Path, overrides: dict) -> None:
    """Merge-write the overlay: existing keys are kept, given keys are updated,
    and a value of ``None`` deletes a key (revert to env/default)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    current = load_overrides(data_dir)
    for key, value in overrides.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    (data_dir / OVERRIDES_FILENAME).write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n")


_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def coerce_setting(key: str, value: Any) -> Any:
    """Coerce a console/overlay value to the dataclass field's type.

    Raises ValueError for unknown keys, uncoercible values, or out-of-set
    choice values. The target type comes from the Settings dataclass itself."""
    entry = _SCHEMA_BY_KEY.get(key)
    if entry is None:
        raise ValueError(f"unknown setting: {key}")
    ftype = str({f.name: f.type for f in dataclasses.fields(Settings)}[key])
    try:
        if ftype == "bool":
            if isinstance(value, bool):
                out = value
            elif isinstance(value, (int, float)) and value in (0, 1):
                out = bool(value)
            elif isinstance(value, str) and value.strip().lower() in _BOOL_TRUE | _BOOL_FALSE:
                out = value.strip().lower() in _BOOL_TRUE
            else:
                raise ValueError
        elif ftype == "int":
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
            out = int(value)
        elif ftype == "float":
            if isinstance(value, bool):
                raise ValueError
            out = float(value)
        elif ftype.startswith("tuple"):
            if isinstance(value, str):
                out = tuple(p.strip() for p in value.split(",") if p.strip())
            elif isinstance(value, (list, tuple)):
                out = tuple(str(v) for v in value)
            else:
                raise ValueError
        else:  # str
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError
            out = str(value)
    except (TypeError, ValueError):
        raise ValueError(f"cannot coerce {value!r} for setting '{key}' ({ftype})") from None
    if entry["type"] == "choice" and out not in entry.get("choices", []):
        raise ValueError(
            f"'{key}' must be one of: " + ", ".join(entry.get("choices", [])))
    return out


def apply_overrides(settings: Settings) -> Settings:
    """Return ``settings`` with the console overlay applied on top (console wins).

    Unknown/non-whitelisted keys and uncoercible values in the overlay are
    ignored defensively — a hand-edited settings.json can never break startup."""
    overrides = load_overrides(Path(settings.data_dir))
    patch: dict[str, Any] = {}
    for key, raw in overrides.items():
        if key not in _SCHEMA_BY_KEY:
            continue
        try:
            patch[key] = coerce_setting(key, raw)
        except ValueError:
            continue
    return dataclasses.replace(settings, **patch) if patch else settings
