# AI Dev Assistant

A self-hosted **multi-agent** system. A "boss" **orchestrator** breaks a task down, routes
each subtask to the **best-suited specialized agent**, and runs them **in parallel**
through a pooled set of sessions. Agents share **memory**, a **knowledge base**, and a
**knowledge graph**, and can **message one another**. Every result is **verified against
real test/lint signals**, and each task is **documented** as a full report plus a
one-glance **brief**. There's a **modern web UI** with live progress.

It works on **real repositories** — clone or open an existing codebase, make verified
multi-file changes with real file/grep/patch/exec/git tools in a **sandbox**, and deliver
the work as a **branch + commit**. Runs are **reliable** (a strict-review miss degrades
gracefully instead of cascading failure), **measurable** (a golden-task `ada eval` harness,
per-run quality scores, cost attribution, span tracing, durable event logs), and they
**learn** across runs (outcome-aware reflection, deduplicated long-term memory, human
feedback, and learned agent routing).

**No API key needed.** By default it runs on the **Claude Agent SDK**, which uses your
existing Claude Code login. (You can switch to the raw Anthropic API with a key via
`ADA_LLM_BACKEND=anthropic`.)

## How it works

```
task ─▶ Orchestrator ──decompose──▶ Plan (DAG of subtasks + acceptance criteria)
                       └─route─────▶ best agent per subtask (capability match)
                                      │
          ┌───────────────parallel, bounded by the session pool──────────────┐
     Researcher          Coder            Reviewer (verify)        Documenter
          └──── message bus + shared blackboard ────┘
                                      │
   Memory (SQLite+vectors) · Knowledge Base (retrieval) · Knowledge Graph (NetworkX)
```

- **Orchestrator** — decomposes the task into a dependency graph (validated for cycles/
  dangling deps), routes each subtask, and can **adaptively replan** to repair a failure.
- **Session pool** — caps concurrency and **terminates idle sessions** (reaper) so
  spawned agents are never over-used; warm sessions are reused.
- **Scheduler** — runs independent subtasks concurrently; retries transient LLM errors with
  backoff; **degrades** a real-but-unverified subtask to `passed_with_caveats` so dependents
  keep running instead of cascading `BLOCKED`.
- **Objective verification** — the reviewer reads the actual file contents and the gate runs
  the subtask's tests + lint: failing tests **hard-fail**, green tests override an LLM nitpick.
- **Real-repo tools** — `read_file`/`write_file`/`edit_file`/`apply_patch`/`grep`/`run_command`
  (sandboxed) /`git_*`, all rooted at a per-run workspace with a secret denylist.
- **Memory / KB / KG** — embedded, local, behind interfaces (swap to Neo4j/Qdrant later);
  outcome-aware reflection writes deduped, decaying lessons consulted at plan time.
- **Docs** — per task: `plan.md`, `report.md`, `brief.md`, plus `events.jsonl`, `trace.jsonl`,
  and `audit.jsonl`, and `docs/INDEX.md`.

## Quickstart

Requires [`uv`](https://docs.astral.sh/uv/) (provisions Python 3.12 automatically) and a
logged-in [Claude Code](https://claude.com/claude-code) on the machine (the SDK reuses it).

```sh
cd ai-dev-assistant
uv venv --python 3.12
uv pip install -e ".[dev]"

# run the test suite (no login needed — offline fakes)
uv run pytest -q

# launch the web UI  →  http://127.0.0.1:8000
uv run ai-dev-assistant serve

# …or run a task headless from the terminal
uv run ai-dev-assistant run "Add input validation to a sample function and document it"

# interactive plan mode: propose a plan, refine it in plain English, then run
uv run ai-dev-assistant run -i "Build a REST API for notes with tests"
#   plan> add a security review step          ← orchestrator revises the DAG
#   plan> use the database agent for the schema
#   plan> <Enter>                              ← approve & run

# work on a REAL repository and deliver the change as a branch:
ADA_REPO_PATH=~/code/myproject ADA_GIT_FINALIZE=true \
  uv run ai-dev-assistant run "Fix the failing test in the auth module and refactor it"

# score the assistant against the golden-task suite:
uv run ai-dev-assistant eval
```

The UI lets you submit a task and watch the orchestrator plan it, agents run in parallel
(live cards), metrics tick up (sessions/reaped, KG nodes/edges, memories, messages), and
the final brief render — with a click-through to the full plan/report. Headless runs write
`docs/<task-id>/{plan,report,brief}.md` and update `docs/INDEX.md`.

To use the raw Anthropic API instead of your Claude Code login, copy `.env.example` to
`.env`, set `ADA_LLM_BACKEND=anthropic`, and add your `ANTHROPIC_API_KEY`.

## Configuration

All knobs live in `.env` (see `.env.example`): models and per-role effort, the session
concurrency cap and idle TTL, retry count, and the embeddings backend
(`fastembed` for real semantic recall, or `hash` for a no-download offline fallback).

## Layout

```
src/ai_dev_assistant/
  llm/           provider interface + backends, resilience (timeout/backoff), pricing,
                 jsonout (JSON repair), record/replay cassettes, schemas
  agents/        BaseAgent, orchestrator, reviewer, reflector, registry (14 specialists)
  orchestration/ task model (DAG + soft-success), scheduler, session pool, run store,
                 run control (pause/steer), trace, message bus, events
  memory/        SQLite store (dedup + decay), embeddings (dim-guarded), vector store
  knowledge/     knowledge base + knowledge graph + repo_map (codebase onboarding)
  security/      secret redaction, untrusted-content envelope, audit log
  evals/         golden-task harness + deterministic graders (file_exists/ast_defines/tests_pass)
  tools/         agent tools — memory/kb/kg, file read/write/edit/patch, grep, run_command,
                 git, messaging — sandboxed + audited
  verification.py objective signals (file contents, tests, lint) folded into the verdict
  vcs.py         git: materialize a repo into the workspace, branch/commit delivery
  context.py     token-budgeted prompt assembly
  execution.py   sandboxed command runner (scrubbed env, rlimits, process-group kill)
  docs/          per-task documentation writer
  web/           FastAPI server + modern single-page UI (static/) — dashboard, pause/steer,
                 feedback, durable event/trace endpoints
  engine.py      wires everything together and emits live events
  cli.py         entrypoint (run · serve · eval)
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full capability map and how the
five reliability/capability tiers fit together.
