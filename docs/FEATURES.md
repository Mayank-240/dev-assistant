# Features & Flows

Living inventory of what the assistant does today, verified against the code.
(`ada` is shorthand for the `ai-dev-assistant` entrypoint.) Design history lives
in [PLAN.md](PLAN.md) and [IMPROVEMENTS.md](IMPROVEMENTS.md); how the pieces fit
is in [ARCHITECTURE.md](ARCHITECTURE.md); production setup in
[DEPLOYMENT.md](DEPLOYMENT.md).

## Projects & workspaces
- Project lifecycle: create greenfield (own git checkout) / import a local repo
  **in place** (read-only contract: the assistant never commits, switches
  branches, or writes in your repo — only `ada/*` branches) / import a git URL
  (clone). One-time onboarding + git-diff incremental re-indexing; per-project
  policy (budget, effort, git_mode branch|merge, protected paths — enforced at
  the tool boundary with DENIED-audited refusals); archive/delete (never
  removes a local-origin directory). CLI: `ada project
  new|import|list|show|archive|delete|distill`.
- Each project owns its checkout, `memory.db`, knowledge graph, run history,
  and quality trend; tasks run in git worktrees off the durable checkout
  (outside your directory) on branch `ada/<task-id>`, persisted for
  review/continue/resume.
- Cross-project fan-out (`ada run -p a -p b`, multi-project `/api/run`): one
  prompt over N projects → one child task per project (own worktree, baseline,
  `ada/<child-id>` branch) run in parallel, optional `--stagger` (first child's
  lessons inform the rest), parent budget split into per-child caps, rollup
  report with per-project verdict/cost/quality/branch, `parent_id` lineage,
  failure isolation.
- Cross-project dependencies: an optional `project_deps` map
  (`{slug: [upstream slugs]}` on `POST /api/run`, `deps=` on
  `run_cross_project`) runs dependents in topological **waves**, each upstream's
  status + bounded summary appended to the dependent's prompt under
  `--- Upstream results ---`. Unknown slugs and cycles are rejected up front
  (400). The rollup records the dependency order.
- Workspaces (`workspaces.py`, `workspaces.json`): named groups of
  inter-related projects. A project belongs to at most ONE workspace (assigning
  elsewhere moves it; deleting a workspace only ungroups; deleting a project
  unassigns it). Each workspace stores default dependencies (validated against
  members, acyclic); `workspace_run_spec` expands the workspace — or a subset,
  dropping edges to excluded members — into the fan-out `projects` + `deps`
  payload. API: `GET/POST /api/workspaces`, `PATCH/DELETE /api/workspaces/{ws}`,
  member assign/unassign, `PUT .../deps`, `POST .../run`;
  `GET /api/projects` items carry a `workspace` field.
- Workspace-aware context (`workspace_context`, `ADA_WORKSPACE_CONTEXT`, on by
  default): when the active project has workspace siblings, each subtask's
  context gains one extra part — "Related knowledge from workspace '<name>'" —
  the top 2 memories + top 2 KB hits from up to 3 siblings, attributed
  `[<sibling slug>]`, wrapped as untrusted content, capped ~1500 chars, placed
  after the project's own recall so it yields first under the budget. Sibling
  stores open read-only (never created); projects outside any workspace
  assemble byte-identical prompts.

## Running tasks: planning, agents, execution
- Orchestrator planning: prompt → DAG of subtasks with acceptance criteria,
  routed by capability descriptions + past lessons + human feedback + agent
  track records + repo map. Structural validation (cycles/dupes/dangling/size
  cap).
- Interactive plan mode (CLI `run -i`, web composer): propose → refine in plain
  English (`POST /api/plan/refine`) → approve → run.
- Rolling scheduler (dependents start the moment a dep finishes), session pool
  (concurrency cap, idle reaper, warm reuse), review-retries separate from
  transient-error retries, degrade-on-partial.
- Self-heal (adaptive replan, **on by default**, `adaptive_replan` /
  `ADA_ADAPTIVE_REPLAN`): a failed subtask gets a bounded repair subtask
  (`<id>-fix`, routed to the debugger) injected mid-run and surfaced in the UI
  as a `plan_update`; repairs are capped per run.
- Guardrails: run budget, per-turn budget stop (anthropic backend), tool-gate
  starvation of over-budget SDK agents, per-subtask wall-clock cap, plan-size
  cap.
- Durability: checkpointed plan + subtask states; `ada resume`; `run
  --continue` re-engagement (workspace + context carried into a linked child
  run); persistent queue (reorder/promote/concurrency/pause); cancellation
  kills the full child-process tree.
- **19 built-in specialists** (+ reviewer/reflector/orchestrator): the
  dev-lifecycle spine (product_manager, architect, researcher, coder,
  test_engineer, documenter), quality roles (debugger, refactorer,
  security_auditor, accessibility_auditor, ux_reviewer), domain roles (devops,
  database, frontend, performance, integrator), delivery roles (api_designer,
  migrator, release_manager). Review-oriented roles run read-only.
- Custom agents: operator-defined specialists (name, description, when_to_use,
  system_prompt, tools, optional effort/model) editable from the console's
  **Agents** view or `<data_dir>/custom_agents.json`, validated on load/upsert
  (slug name, no builtin collision, tools must exist, effort tier checked) and
  routable like any built-in. API: `GET /api/agents` →
  `{builtin, custom, tools}`, `POST /api/agents {spec}`,
  `DELETE /api/agents/{name}` (400 for builtins). New runs pick customs up
  automatically.
- Per-role model routing (`role_models` / `ADA_ROLE_MODELS`): comma-separated
  `role=model` pairs route individual roles (customs included) to their own
  model; `reviewer=...` overrides the verdict reviewer; the orchestrator itself
  is not routable; malformed/unknown entries are skipped with a warning. Works
  on both backends. Per-role effort tiers: low/medium/high/xhigh/max.
- Tools: memory/KB/KG; file read (offset/limit) / write / edit (uniqueness,
  replace-all) / 3-way patch / list / ripgrep grep; `symbols` /
  `find_references`; sandboxed `run_command`, validated `install_packages`,
  targeted `run_tests`; git status/diff; messages + blackboard; depth-1
  `delegate`; SSRF-guarded `web_fetch` (opt-in).

## Review & delivery
- Reviewer judges each subtask against its criteria from the files it actually
  changed (untrusted-enveloped), per-criterion verdict with evidence.
- Objective gate on the delta vs a pre-run baseline: subtask-scoped tests,
  new-lint penalty, pluggable signals (typecheck/build/coverage); pre-existing
  failures never blamed; scoped-green-only soft-pass, capped score. Test
  detection: Python, JS, Go, Rust, Java, Ruby. Final workspace test run;
  0-100 quality score.
- Reviews & Permissions panel on every task: verdicts with per-criterion
  evidence + changed files + diffs, resolved policy, tools per agent, denied
  actions from the audit log.
- Per-subtask decisions: **Accept** cherry-picks that subtask's commit into the
  review target (owned checkouts advance directly; in-place projects use
  `ada/integration`) and records the sha (`accepted_commit`); **Reject**
  records feedback for learning; **Rollback**
  (`POST /api/runs/{task_id}/subtasks/{subtask_id}/rollback`) reverts exactly
  the accepted commit on the review target — 409 on conflict, never forced.
- Accept all & deliver (`POST /api/tasks/{task_id}/deliver`): one click merges
  the whole task branch into the review target and records undecided subtasks
  as accepted; idempotent.
- Delivery policy: `git_mode` branch (leave `ada/<id>` for review — default) or
  merge (auto-merge into the review target on a fully-passed run; conflicts
  keep the branch).
- Workspace GC (`gc.py`, `GET/POST /api/projects/{slug}/gc`): dry-run report,
  then reclaim worktrees of finished runs and fully-accepted `ada/*` branches
  older than `gc_keep_days` (default 14). Conservative: persist-for-review is
  the default, `ada/integration` and resumable runs are never collected,
  nothing runs automatically.

## Attention & control
- Attention screens: agents can ask the operator a question or request
  permission mid-run (`ask` / `permission` events with options); the run waits
  up to `attention_timeout`, the console renders an answer screen, and open
  requests surface on Home and through notifications. Answers ride the steer
  channel (`[answer <id>] …`).
- In-run control: pause/resume between batches, steer the next subtask, cancel
  (kills child process groups). Post-run: feedback (rate/accept/comment) feeds
  the next plan.
- Live transcripts: every agent step — thinking, text, tool calls, tool results
  (error-flagged), final result — is persisted (~4k chars per step) as
  `agent_step` events in the task's `events.jsonl` and served via
  `GET /api/runs/{task_id}/transcript/{subtask_id}`; the agent detail modal's
  "Full transcript" is a kind-styled, live-following viewer.

## Memory, knowledge & graph
- Memory: hybrid semantic+lexical recall (RRF), dedup, decay, caps,
  project/global scopes; outcome-aware reflection lessons (DO/AVOID/ROUTING);
  feedback → planning. Curation API:
  `GET/PATCH/DELETE /api/projects/{slug}/memories[/{id}]` — paginated listing,
  edit (re-embeds), forget (drops the vector too).
- Knowledge base: ingest at onboarding + re-index of run artifacts;
  drag-and-drop upload (`POST /api/projects/{slug}/kb/upload`); `kb_search`
  agent tool.
- Layered knowledge graph (per project): edges carry a `layer` — `domain`
  (agent knowledge) vs `run` (engine bookkeeping) — with weights, provenance,
  and merge-on-save. v2 API: `GET /api/projects/{slug}/graph2`
  (`?layer=&min_weight=&limit=`), `GET .../graph2/node/{id}` (`?depth=&layer=`),
  `GET .../graph2/search?q=`; legacy `GET /api/graph` unchanged.
- Distillation (`knowledge/distill.py`): consolidates the domain layer as runs
  accumulate — pure heuristics + local embeddings, no LLM calls, deterministic
  and free. Merges near-duplicate concepts (plural/hyphen/stopword id variants;
  label-embedding cosine ≥ 0.92 with a real embedder — the hash backend skips
  semantic merges), redirecting edges with provenance unioned; prunes stale
  low-weight domain edges (weight < 2, untouched > 45 days, top-20 hubs
  protected) and orphaned concepts. Run-layer edges are never touched, merges
  never cross node types, a second pass is a no-op, graphs under 10 domain
  edges are left alone. Run it via `ada project distill <slug> [--dry-run]`,
  `POST /api/projects/{slug}/graph2/distill {dry_run}`, or the console's graph
  panel.
- Symbol-ranked repo map (AST + import centrality + query relevance);
  per-subtask context pack under a per-model token budget; prompt caching
  (anthropic backend).

## Search & palette
- Global search (`GET /api/search`, `search.py`): one query, ranked hits across
  all projects and four modalities — tasks (run rows, recency-boosted),
  memories (same hybrid recall a project uses), KB chunks (query embedded once,
  reused), file names/paths (never content).
- `cmd+K` command palette in the console fronts the same search.

## Playbooks, schedules & notifications
- Playbooks: 7 pre-tuned task templates (raise-coverage, upgrade-dependency,
  security-audit, refactor-module, document-codebase, fix-failing-tests,
  add-feature-tdd), one click from the project Overview
  (`GET /api/playbooks`, `POST /api/playbooks/{pid}/run`).
- Scheduled tasks (`orchestration/schedules.py`): recurring per-project runs
  with **either** `every_hours` **or** a 5-field `cron` expression (mutually
  exclusive; bad expressions 400); the server's 60s tick enqueues due schedules
  as normal tasks. CRUD: `GET/POST /api/schedules`,
  `PATCH/DELETE /api/schedules/{sid}`.
- Notifications (`notify.py`): fan-out on `ask`/`permission`/`done`/`error`
  (filterable via `notify_events`) to four channels — generic **webhook**
  (SSRF-guarded, redacted), **Slack** incoming webhook (same guard/redaction),
  **email** (stdlib SMTP; password env-only `ADA_SMTP_PASSWORD`), **macOS
  desktop** — plus the in-app notification center. All configurable live from
  the settings console.

## Cost, analytics & benchmarks
- Cost attribution: pricing table populates `cost_usd` (budget guardrail trips
  on it), per-subtask cost deltas, token counts.
- Analytics API (read-only over the run store + event logs):
  `GET /api/analytics/overview | outcomes | project/{slug} | run/{task_id}` —
  spend dashboard, outcome ratios, per-run subtask breakdowns; 30-day spend
  snapshot on Home.
- Benchmark history (`evals/history.py`): `ada eval --record-history` (or
  `python -m ai_dev_assistant.evals.replay_eval --record-history`, or
  `ADA_EVAL_RECORD_HISTORY=1`) appends one JSONL entry per suite run to
  `<data_dir>/benchmarks.jsonl` — timestamp, short git SHA + dirty flag, suite,
  pass rate, quality mean/min, cost, duration, run count. Served via
  `GET /api/benchmarks` (trend + last 50 entries) and shown on Home and the
  console's Benchmarks panel. Recording is strictly opt-in; CI records the
  replay suite and uploads `benchmarks.jsonl` as an artifact.

## GitHub
- Poll-based, no inbound webhooks; token env-only (`ADA_GITHUB_TOKEN` /
  `GITHUB_TOKEN`), sent only as a request header — never in URLs, git argv, or
  env mutations. `push_branch` uses your existing git auth.
- Issues → runs → PRs: open issues carrying `github_label` on repos mapped by
  `github_repos` (owner/repo=project-slug) become task runs; on terminal
  status the branch is pushed and an evidence-first PR opens (brief +
  verification table), with the link commented back on the issue.
  `GET /api/github/status` reports config + tracked/seen counts.
- PR-comment follow-ups (opt-in `github_pr_followups`): the poller watches the
  assistant's own open PRs; each new reviewer-comment batch becomes a follow-up
  run — re-engaging the original task (continue_from lineage, same workspace +
  `ada/<task-id>` branch) when the head branch maps to a known run, else a
  fresh run on the repo's project. When the follow-up reaches a terminal
  status, the poller posts a result comment on the PR — a verdict table + cost
  for a completed run, an honest did-not-complete note otherwise. Per-PR
  cursors persist in `github_seen.json`; failed posts retry (capped at 3); the
  bot's own comment never seeds a new follow-up. Every batch is a billed run.

## Web console (landing, Home, views)
- `/` is a public product landing page; the console shell lives at `/app`;
  data stays behind `/api/*` auth either way.
- Home: open attention requests across running tasks, running/queued/recent
  runs, 30-day spend, benchmark trend, workspace summaries, counts
  (`GET /api/home` — each section degrades to its empty default with an
  `errors` entry, never a 500).
- Sidebar: Home, per-project pages, + New/Import, + New workspace, and global
  views — Workspaces, Agents (roster + custom-agent editor), All activity,
  Settings.
- Project pages: composer (effort, memory scope, budget, plan review),
  playbooks, schedules, policy editor, run history + quality sparkline,
  memory/KG views, KB upload, GC action, an app runner
  (`/api/projects/{slug}/app[/start|/stop|/logs]`) that detects and serves the
  project's runnable app with logs.
- Run view: live DAG + streaming agent cards, timeline with zoom + per-subtask
  cost, budget meter, diff viewer, files browser, transcripts, Reviews &
  Permissions, re-engage/resume/retry, run comparison, feedback.
- Extras: notification bell, `cmd+K` palette, KG filter/focus, A/B replay
  smoke from the UI, first-run tour, accessibility (dialogs, keyboard,
  contrast, reduced motion).

## Security & auth
- SDK built-ins confined by deny-hook; toolbox path sandbox + secret denylist;
  redaction at every durable boundary; untrusted envelopes; audit log.
- Command sandbox tiers (`sandbox` setting, console-editable): subprocess
  (scrubbed env, rlimits, process-group kill, per-run kill tags — default),
  bwrap (Linux namespaces, no network unless allowed), container (throwaway
  Docker, `--network=none` unless allowed); missing binaries fall back to
  subprocess with a one-time warning.
- Web auth: bearer token (`ADA_API_TOKEN`, auto-generated if unset on a
  non-loopback bind) + HttpOnly session cookie via `POST /api/login`; **named
  multi-user tokens** (`web/users.py`, owner-only
  `GET/POST /api/users`, `DELETE /api/users/{name}`; sha256-only storage; runs
  stamped with the starting identity). CORS locked down; hardened non-root
  container deployment (see [DEPLOYMENT.md](DEPLOYMENT.md)).

## Settings & configuration
- Global settings console (sidebar → Settings): **52 whitelisted settings
  across 8 groups** (LLM & Models, Guardrails & Budget, Execution & Safety,
  Verification, Memory & Knowledge, Observability, Notifications, GitHub) with
  source badges (default/env/overlay), per-field reset, live application to new
  runs. Secrets, paths, and backend selection are deliberately excluded —
  env-only.
- Every setting also has an `ADA_*` env var (see `.env.example`); the overlay
  (`<data_dir>/settings.json`) stores only overridden keys.

## Evals & quality
- `ada eval` golden-task harness: **11 golden tasks** (greenfield +
  repo-fixture tasks across Python/JS/Go/Rust/Ruby + the cross-project
  `cross_logging_fix` fan-out task) graded by deterministic held-out graders
  (file_exists / ast_defines / tests_pass) with toolchain-aware skips. Flags:
  `--only/--json/--repeat/--timeout/--replay/--record-cassettes/--ab/--record-history`.
- Offline replay: committed cassettes replay the full plan → schedule → review
  → summarize pipeline with zero network. Cassettes hash exact prompts — after
  editing agent/reviewer prompt templates, regenerate offline with
  `ada eval --record-cassettes`.
- A/B knob harness (`ada eval --ab ADA_KNOB=V1,V2 [--replay]`); retrieval
  benchmark for memory recall.
- CI (three gates): `uv run pytest -q` · `node --test "tests/js/**/*.test.mjs"`
  · `uv run python -m ai_dev_assistant.evals.replay_eval` — plus a
  secret-gated live-eval workflow.
- Per-task artifacts: plan/report/brief/activity docs, streamed `events.jsonl`,
  trace spans, audit log, per-subtask cost.

## Core flows
1. Project task: worktree off the checkout → plan → parallel agents (per-subtask
   worktrees) → delta-gated verify → `ada/*` branch → per-subtask review →
   accept/deliver (or rollback later).
2. Workspace run: one prompt → dependency-ordered waves across member projects
   → per-project children + rollup.
3. Interactive plan → refine → run; re-engage (`--continue`) / resume
   (checkpoints).
4. Queue: submit N → pump at configured concurrency → survives restart.
5. Attention: agent asks / requests permission → Home + notification → answer →
   run continues.
6. GitHub: labeled issue → run → PR → reviewer comments → follow-up run →
   result comment.
7. Evals: live suite · offline replay (CI) · `--ab` · `--record-history` trend.
8. Deploy: compose up → token from logs → sign in → container is the boundary.
