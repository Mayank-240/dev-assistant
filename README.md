# AI Dev Assistant

A self-hosted **multi-agent** system. A "boss" **orchestrator** breaks a task down, routes
each subtask to the **best-suited specialized agent**, and runs them **in parallel**
through a pooled set of sessions. Agents share **memory**, a **knowledge base**, and a
**layered knowledge graph**, and can **message one another**. Every result is **verified
against real test/lint signals**, reviewable **per subtask** (accept, reject, roll back),
and each task is **documented** as a full report plus a one-glance **brief**.

It is **project-first**: a *project* is the durable unit — it owns a repo checkout
(greenfield, cloned, or your local repo operated on **in place** under a read-only
contract: the assistant only ever writes `ada/*` branches, never your files or history),
plus accumulated memory, knowledge, policy, and run history. **Workspaces** group
inter-related projects: a workspace run fans out across its members in
**dependency-ordered waves** — each downstream project's prompt carries its upstreams'
results — and every member's runs draw **shared sibling context** from the rest of the
group. Runs **self-heal** (failed subtasks get bounded repair subtasks), pause on
**attention screens** when an agent needs a human, and stream **live transcripts** of
every agent step to the web console.

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
   Memory (SQLite+vectors) · Knowledge Base (retrieval) · Knowledge Graph (layered)
```

- **Orchestrator** — decomposes the task into a dependency graph (validated for cycles/
  dangling deps), routes each subtask across **19 built-in specialists** plus any
  **custom agents** you define in the UI, and **replans mid-run**: a failed subtask gets
  a bounded repair subtask (on by default).
- **Session pool** — caps concurrency and **terminates idle sessions** (reaper) so
  spawned agents are never over-used; warm sessions are reused.
- **Scheduler** — starts dependents the moment a dep finishes; retries transient LLM
  errors with backoff; **degrades** a real-but-unverified subtask to `passed_with_caveats`
  so dependents keep running instead of cascading `BLOCKED`.
- **Objective verification** — the reviewer reads the actual file contents and the gate
  runs the subtask's tests + lint against a pre-run baseline: failing tests **hard-fail**,
  green tests override an LLM nitpick. Every verdict is reviewable per subtask, with
  accept / reject / rollback.
- **Real-repo tools** — file read/write/edit/patch, ripgrep, symbols/references,
  sandboxed `run_command` (subprocess / bwrap / container tiers), git — all rooted at a
  per-task git worktree with a secret denylist; parallel subtasks get their own worktrees.
- **Memory / KB / KG** — embedded, local, per-project; outcome-aware reflection writes
  deduped, decaying lessons consulted at plan time; the graph's domain layer is
  **distillable** (merge near-duplicates, prune stale edges) from the CLI or UI.
- **Docs** — per task: `plan.md`, `report.md`, `brief.md`, `activity.json`, plus
  `events.jsonl`, `trace.jsonl`, and `audit.jsonl`.

## Run in one line (Docker)

No clone, no build — the image is published to GHCR on every commit:

```sh
docker run -d --name ada -p 8000:8000 -v ada-data:/data \
  -e ANTHROPIC_API_KEY=sk-ant-... ghcr.io/mayank-240/dev-assistant:latest
```

Then open http://localhost:8000 and grab the auto-generated API token from
`docker logs ada | grep "API token"` (or pin one with `-e ADA_API_TOKEN=...`).
The container uses the `anthropic` backend (the Claude Code login backend needs
an interactive host session) and is the supported isolation boundary — see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the hardened compose setup
(`docker compose up -d` from a clone works too).

## Quickstart

Requires [`uv`](https://docs.astral.sh/uv/) (provisions Python 3.12 automatically) and a
logged-in [Claude Code](https://claude.com/claude-code) on the machine (the SDK reuses it).

```sh
cd ai-dev-assistant
uv venv --python 3.12
uv pip install -e ".[dev]"

# run the test suite (no login needed — offline fakes)
uv run pytest -q

# launch the web console  →  landing at http://127.0.0.1:8000, console at /app
uv run ai-dev-assistant server

# …or run a task headless from the terminal
uv run ai-dev-assistant run "Add input validation to a sample function and document it"

# interactive plan mode: propose a plan, refine it in plain English, then run
uv run ai-dev-assistant run -i "Build a REST API for notes with tests"
#   plan> add a security review step          ← orchestrator revises the DAG
#   plan> use the database agent for the schema
#   plan> <Enter>                              ← approve & run

# re-engage a completed task — continue its workspace + context with a follow-up
uv run ai-dev-assistant run --continue 20260628-120254-d8ddbd "now add error handling and tests"

# work on a REAL repository: import it as a project (local paths are operated on
# in place — the assistant only ever writes ada/* branches, never your files):
uv run ai-dev-assistant project import ~/code/myproject
uv run ai-dev-assistant run -p myproject "Fix the failing test in the auth module and refactor it"

# score the assistant against the golden-task suite:
uv run ai-dev-assistant eval
```

The console opens on a **Home** screen: anything waiting on you (attention requests),
running/queued/recent runs, a 30-day spend snapshot, the benchmark trend, and your
workspaces. From there: per-project pages (composer, run history, policy, playbooks,
schedules, memory/KG views), live run views with streaming agent cards and full
transcripts, a Reviews & Permissions panel per task, and `cmd+K` search across tasks,
memories, knowledge, and files. Headless runs write `docs/<task-id>/{plan,report,brief}.md`.

To use the raw Anthropic API instead of your Claude Code login, copy `.env.example` to
`.env`, set `ADA_LLM_BACKEND=anthropic`, and add your `ANTHROPIC_API_KEY`.

## What it does

The compact map — [`docs/FEATURES.md`](docs/FEATURES.md) is the full living inventory:

- **Projects & workspaces** — durable checkouts, per-project policy and knowledge,
  cross-project fan-out, workspaces with dependency-ordered waves + sibling context.
- **Planning & agents** — interactive plan mode, 19 built-in specialists, custom agents
  defined from the UI, per-role model routing and effort tiers.
- **Review & delivery** — per-subtask verdicts with evidence and diffs, accept /
  reject / rollback, branch or auto-merge delivery, workspace GC.
- **Attention & control** — ask/permission screens, pause/steer, live transcripts,
  re-engage and resume.
- **Memory & knowledge** — hybrid semantic recall, layered knowledge graph with
  distillation, global search + `cmd+K` palette.
- **Automation** — playbooks, scheduled runs (interval or cron), notifications
  (Slack / email / webhook / desktop), GitHub issues → runs → PRs with
  reviewer-comment follow-ups that report back on the PR.
- **Cost & quality** — spend analytics, per-run quality scores, golden-task evals,
  benchmark history across commits.
- **Security & auth** — sandbox tiers (subprocess / bwrap / container), secret
  redaction, audit log, bearer-token auth with named multi-user tokens.

## Configuration

Every knob has an `ADA_*` env var (see `.env.example`); the **settings console**
(sidebar → Settings) edits 52 whitelisted settings across 8 groups live — models and
per-role routing, budgets, sandbox tier, verification, notifications, GitHub.

## Layout

```
src/ai_dev_assistant/
  llm/           provider interface + backends, resilience, pricing, jsonout (JSON
                 repair), record/replay cassettes, schemas
  agents/        BaseAgent, orchestrator, reviewer, reflector, registry (19 built-in
                 specialists), custom (operator-defined agents)
  orchestration/ task model (DAG + soft-success), scheduler, session pool, run store,
                 run control (pause/steer/answer), fan-out, schedules, trace, events
  memory/        SQLite store (dedup + decay), embeddings (dim-guarded), vector store
  knowledge/     knowledge base, layered knowledge graph, distillation, repo map,
                 cross-project combine
  security/      secret redaction, untrusted-content envelope, audit log
  evals/         golden-task harness, graders, replay eval, A/B, benchmark history
  tools/         agent tools — files/grep/symbols, run_command, git, messaging — sandboxed
  projects.py    project registry (checkouts, policy, indexing state)
  workspaces.py  workspace groups + dependency maps → fan-out run specs
  engine.py      wires everything together and emits live events
  github.py      issues → runs → PRs, reviewer-comment follow-ups
  analytics.py · search.py · playbooks.py · notify.py · gc.py · verification.py ·
  vcs.py · context.py · execution.py · docs/ (per-task writer)
  web/           FastAPI server (landing at /, console at /app), named users, static UI
  cli.py         entrypoint (run · project · resume · server · eval)
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the module map and how a run
flows end to end, and [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) to run it in production.
