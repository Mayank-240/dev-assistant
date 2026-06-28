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

    # --- Session pool ---
    max_concurrent_runs: int = 1    # how many queued tasks execute at once (1 = serial)
    max_concurrent_sessions: int = 4
    session_idle_ttl: float = 30.0
    reaper_interval: float = 5.0
    max_retries: int = 1

    # --- Verification ---
    verify_run_tests: bool = True   # run generated tests in the workspace after a task
    verify_timeout: float = 120.0

    # --- Cost guardrail ---
    budget_usd: float = 0.0         # 0 = no cap; otherwise stop scheduling new work past this
    agent_max_turns: int = 24       # per-agent tool-loop cap (lower = cheaper)

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
            max_concurrent_runs=_int("ADA_MAX_CONCURRENT_RUNS", 1),
            max_concurrent_sessions=_int("ADA_MAX_CONCURRENT_SESSIONS", 4),
            session_idle_ttl=_float("ADA_SESSION_IDLE_TTL", 30.0),
            reaper_interval=_float("ADA_REAPER_INTERVAL", 5.0),
            max_retries=_int("ADA_MAX_RETRIES", 1),
            verify_run_tests=_get("ADA_VERIFY_RUN_TESTS", "true").lower() not in ("0", "false", "no"),
            verify_timeout=_float("ADA_VERIFY_TIMEOUT", 120.0),
            budget_usd=_float("ADA_BUDGET_USD", 0.0),
            agent_max_turns=_int("ADA_AGENT_MAX_TURNS", 24),
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
