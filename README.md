# AI Dev Assistant

A self-hosted **multi-agent** system. A "boss" **orchestrator** breaks a task down, routes
each subtask to the **best-suited specialized agent**, and runs them **in parallel**
through a pooled set of sessions. Agents share **memory**, a **knowledge base**, and a
**knowledge graph**, and can **message one another**. Every result is **verified**, and each
task is **documented** as a full report plus a one-glance **brief**. There's a **modern web
UI** with live progress.

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

- **Orchestrator** — decomposes the task into a dependency graph and routes each subtask.
- **Session pool** — caps concurrency and **terminates idle sessions** (reaper) so
  spawned agents are never over-used; warm sessions are reused.
- **Scheduler** — runs independent subtasks concurrently; re-queues ones that fail review.
- **Memory / KB / KG** — embedded, local, behind interfaces (swap to Neo4j/Qdrant later).
- **Docs** — per task: `docs/<task-id>/plan.md`, `report.md`, `brief.md`, and `docs/INDEX.md`.

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
  llm/           provider interface + backends (claude_sdk_provider, anthropic_provider), schemas
  agents/        BaseAgent, orchestrator, researcher, coder, reviewer, documenter, registry
  orchestration/ task model, message bus, session pool, scheduler, events
  memory/        SQLite store, embeddings, vector store
  knowledge/     knowledge base + knowledge graph
  tools/         tools exposed to agents (memory, kb_search, kg_query/write, fs, messaging)
  docs/          per-task documentation writer
  web/           FastAPI server + modern single-page UI (static/)
  engine.py      wires everything together and emits live events
  cli.py         entrypoint (run · serve)
```

This is a **walking skeleton**: thin but end-to-end. See the deferred items at the bottom
of the plan for what comes next (web UI, Neo4j/Qdrant backends, sandboxed execution).
