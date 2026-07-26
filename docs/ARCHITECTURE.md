# Architecture & Capabilities

How the system fits together at HEAD. It was built reliability-first — runs finish and
report the truth, then real-repo capability, trustworthy verification, cross-run
learning, and observability/security on top — and is now **project-first**: projects own
durable checkouts and knowledge, workspaces group projects, and every surface (CLI, web,
API) goes through them. [FEATURES.md](FEATURES.md) is the feature inventory; this file
is the structural map.

## Module map

```
engine.py           wires a run together: workspace setup, plan, schedule, finalize
projects.py         project registry — checkouts (greenfield/clone/in-place), policy,
                    indexing state, review targets, deliver/accept plumbing
workspaces.py       named project groups + default dependency maps → fan-out run specs
orchestration/      task DAG + soft-success, rolling scheduler, session pool, run store
                    (SQLite), run control (pause/steer/answer), fanout (cross-project
                    children + waves), schedules (interval/cron), events, trace, bus
agents/             BaseAgent, orchestrator (plan/refine/replan), reviewer, reflector,
                    registry (19 built-in specs), custom.py (operator-defined agents)
tools/registry.py   the agent toolbox — files/patch/grep/symbols, run_command, git,
                    messaging, delegate — path-confined, policy-enforced, audited
llm/                provider interface + claude_sdk/anthropic backends, resilience,
                    pricing, jsonout repair, record/replay cassettes, schemas
memory/             SQLite memory store (dedup/decay), embeddings, vector store
knowledge/          KB, layered graph (graph.py), distill.py (consolidation), repo_map,
                    combine.py (read-only multi-project views), extract
verification.py     objective signals (file contents, scoped tests, lint) → verdicts
execution.py        sandbox tiers for spawned commands: subprocess/bwrap/container
vcs.py              git plumbing: worktrees, branches, cherry-pick/revert, merges
gc.py               workspace GC — reclaim finished worktrees + delivered ada/* branches
github.py           issues→runs→PRs, PR-comment follow-ups (pure, poll-based core)
analytics.py        read-only spend/outcome aggregation over the run store
search.py           global search across tasks/memories/KB/files
playbooks.py        7 task templates · notify.py  webhook/Slack/email/desktop channels
evals/              golden harness, graders, replay_eval, ab, history (benchmarks.jsonl)
security/           secret redaction, untrusted envelopes
web/server.py       FastAPI app (~100 endpoints), auth middleware, pollers (schedules,
                    GitHub), landing at / and console at /app (static/)
web/users.py        named multi-user tokens (sha256-only) over the owner ADA_API_TOKEN
docs/writer.py      per-task plan/report/brief/activity docs + task index
cli.py              run · project · resume · server · eval
```

## The run, end to end

```
ada run -p <slug> "<task>"          (or web composer / queue / schedule / GitHub issue)
  └─ workspace: git worktree off the project checkout, branch ada/<task-id>
     (persists after the run — review, files, continue, resume all read it)
  └─ orchestrator.make_plan  ← repo map + lessons + feedback + agent track record
       └─ optional interactive refine loop (plain-English instructions → revised DAG)
  └─ validate: cycles / dup ids / dangling deps / size cap; policy snapshot emitted
  └─ scheduler.run — rolling: a subtask starts the moment its deps are satisfied
        execute  → agent from the session pool, per-subtask git worktree
                   (worktree_per_subtask, on by default) merged back conflict-aware;
                   toolbox path-confined + policy-enforced; every step streamed as
                   agent_step events (live transcripts)
        attention → an agent can ask/request permission: the run emits the event,
                   notifies (Slack/email/webhook/desktop), and waits (bounded) for
                   the operator's answer via the steer channel
        verify   → reviewer reads actual file contents; objective gate runs scoped
                   tests + lint vs the pre-run baseline (failing tests hard-fail,
                   green tests override an LLM nitpick); per-criterion verdicts
        self-heal → a FAILED subtask gets a bounded repair subtask (debugger) via
                   the replan hook (adaptive_replan, on by default); retries with
                   real output degrade to PASSED_WITH_CAVEATS so dependents run
  └─ finalize: workspace test run · KG enrichment · delivery (branch kept for review,
     or auto-merge when git_mode=merge and the run fully passed) · summarize · reflect
     (outcome-aware lessons) · index artifacts into the KB · agent track records ·
     quality score · docs + events + trace + audit · honest rollup status
  └─ afterwards: per-subtask accept (cherry-pick to the review target) / reject /
     rollback (revert the accepted sha), deliver-all, feedback, re-engage, GC
```

Cross-project runs wrap this: `fanout.run_cross_project` spawns one child run per
project (own worktree/baseline/branch, split budget), honoring a dependency map as
topological waves with upstream summaries injected into dependent prompts. Workspaces
expand to exactly that payload via `workspace_run_spec`.

## Reliability spine

- **Degrade-on-partial** (`scheduler.py`): a real-but-unverified subtask becomes
  `PASSED_WITH_CAVEATS`, satisfying dependents — no `0/3` cascades.
- **Honest rollup** (`engine.py`, `run_store.py`): terminal status derived from subtask
  outcomes (completed/partial/failed); crashes never strand a run `running`; startup
  cleanup marks orphaned runs interrupted.
- **JSON self-repair** (`llm/jsonout.py`) recovers plans/verdicts from malformed output;
  **provider resilience** (`llm/resilience.py`) bounds timeouts/backoff and keeps
  transient retries separate from review retries.
- **Durability**: plan + subtask checkpoints in SQLite (`resume`), persistent queue,
  deep cancellation (per-run process-group kill tags).

## Learning & knowledge

- Per-project memory (hybrid semantic+lexical recall, dedup, decay) + KB + a **layered
  knowledge graph**: `domain`-layer edges are agent knowledge, `run`-layer edges are
  engine bookkeeping; weights and provenance merge on save. `knowledge/distill.py`
  consolidates the domain layer (near-duplicate merges, stale-edge pruning) with pure
  heuristics + local embeddings — no LLM calls — via CLI, API, or the console.
- Reflection writes outcome-aware DO/AVOID/ROUTING lessons; human feedback and
  per-agent pass rates feed the next plan. Workspace siblings contribute bounded,
  attributed, read-only context (`combine.py`).
- `evals/history.py` tracks suite scores per git SHA in `benchmarks.jsonl` so
  prompt/agent changes are measurable commit-over-commit.

## Web, auth & users

- `web/server.py` serves the landing page (`/`), the console (`/app`), and the API;
  WebSockets stream live events per task. Background loops tick schedules (60s) and
  poll GitHub.
- Auth: bearer `ADA_API_TOKEN` (forced on for non-loopback binds, auto-generated if
  unset) exchanged for an HttpOnly session cookie at `POST /api/login`. `web/users.py`
  layers **named users** on top: owner-only create/list/revoke, sha256-only token
  storage in `users.json`, identities stamped onto the runs they start; `local` is the
  pseudo-identity when auth is off. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Security, cost, deploy

- Defense in depth: SDK built-ins deny-hooked, toolbox path confinement + protected
  paths (DENIED-audited), secret redaction at every durable boundary, untrusted
  envelopes, audit log. Spawned commands run under `execution.py`'s selected tier
  (subprocess rlimits / bwrap / throwaway container) — the container deployment is the
  real isolation boundary.
- Cost: pricing table → `cost_usd` per call, budget guardrails (run cap, per-turn stop,
  SDK tool-gate starvation), per-subtask cost deltas, analytics rollups.
- Deploy: hardened multi-stage image (non-root, read-only rootfs compose, `/healthz` +
  `/readyz`, HEALTHCHECK).

## Configuration

Every knob is an `ADA_*` env var (see [`.env.example`](../.env.example)); 52 of them are
console-editable at runtime through the settings overlay (`config.SETTINGS_SCHEMA` is
the single source of truth for what the console may touch — secrets and paths are
deliberately excluded).
