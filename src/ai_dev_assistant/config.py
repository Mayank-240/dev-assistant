"""Runtime configuration, loaded from the environment (.env supported).

Everything tunable lives here so the rest of the code never reads os.environ
directly. Construct once via ``Settings.load()`` and pass it down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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
    adaptive_replan: bool = False   # let the orchestrator amend the DAG between batches
    clarify: bool = False           # ask the user clarifying questions before planning

    # --- Cost guardrail ---
    budget_usd: float = 0.0         # 0 = no cap; otherwise stop scheduling new work past this
    agent_max_turns: int = 24       # per-agent tool-loop cap (lower = cheaper)

    # --- Real-repo binding (Tier 2) ---
    repo_url: str = ""              # git URL to clone into the workspace before the run
    repo_path: str = ""            # local repo path to copy/worktree into the workspace
    repo_ref: str = ""             # branch/tag/sha to check out
    git_finalize: bool = False      # at the end, commit the workspace on a new branch
    git_branch_prefix: str = "ada/"

    # --- Sandbox / execution safety (Tier 2/5) ---
    sandbox: str = "subprocess"     # "subprocess" (scrubbed env + rlimits) | "none"
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
    def load(cls) -> "Settings":
        return cls(
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
            adaptive_replan=_bool("ADA_ADAPTIVE_REPLAN", False),
            clarify=_bool("ADA_CLARIFY", False),
            budget_usd=_float("ADA_BUDGET_USD", 0.0),
            agent_max_turns=_int("ADA_AGENT_MAX_TURNS", 24),
            repo_url=_get("ADA_REPO_URL", ""),
            repo_path=_get("ADA_REPO_PATH", ""),
            repo_ref=_get("ADA_REPO_REF", ""),
            git_finalize=_bool("ADA_GIT_FINALIZE", False),
            git_branch_prefix=_get("ADA_GIT_BRANCH_PREFIX", "ada/"),
            sandbox=_get("ADA_SANDBOX", "subprocess"),
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
            embeddings_backend=_get("ADA_EMBEDDINGS_BACKEND", "fastembed"),
            embed_model=_get("ADA_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
            data_dir=Path(_get("ADA_DATA_DIR", ".ada_data")),
            docs_dir=Path(_get("ADA_DOCS_DIR", "docs")),
            project=_get("ADA_PROJECT", "default"),
            memory_scope=_get("ADA_MEMORY_SCOPE", "project"),
            workspace_dir=Path(_get("ADA_WORKSPACE_DIR", "workspace")),
        )

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
