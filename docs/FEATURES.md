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
- Static-analysis gate (`objective_static`, default on): detected linters and
  typecheckers — ruff (config or `.py` files), eslint (config +
  `node_modules/.bin` or PATH), tsc (`tsconfig.json`, `--noEmit`) — are counted
  once at the pre-run baseline and re-counted per reviewed subtask; the delta
  joins the reviewer's objective note ("static checks: ruff +3 (baseline 12),
  tsc +0") and a positive delta demotes the verdict exactly like a
  newly-failing test, so "tests pass but the diff added 40 lint errors" fails
  honestly. Zero/negative deltas add the note only; a check that times out or
  crashes is logged and skipped; with no tools detected nothing is appended
  anywhere.
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
- Terminal attention client (`ada attend`, `attend.py`): answer the same
  ask/permission requests without the browser. Polls `GET /api/home` (base URL
  `--url`, default `http://127.0.0.1:8000`; token from `--token` /
  `ADA_API_TOKEN` as a bearer header) every `--interval` seconds, renders each
  new request (agent, project, question, numbered options) and reads the answer
  on stdin — a number picks an option, free text answers verbatim, and
  permission requests take `y` (allow for this run) / `once` (allow once) / `n`
  (deny), with unrecognized input denying-with-reason, never granting. Answers
  are delivered via the console's own steer channel
  (`POST /api/run/{task_id}/steer` with `[answer <id>] …` /
  `[permission <id>] ALLOW ONCE|ALLOW FOR THIS RUN|DENIED: …` notes).
  Unreachable server → retry with backoff; Ctrl-C exits cleanly; `--once`
  processes the currently-open items and exits (scripting/cron).
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
- Semantic code index (`knowledge/code_index.py`, wired into the engine via the
  `code_retrieval` setting — on by default): per-project `code_index.db` next
  to `memory.db`; source chunked
  into ~60-line windows (10-line overlap; binaries/lockfiles/minified
  skipped), embedded via the shared embedder (hash backend = lexical-only,
  as everywhere) into the standard vectors table. Incremental
  `index_workspace` (unchanged (path, mtime, size) skipped, deletions
  pruned), hybrid `search` with path:line spans, and `retrieval_context`
  renders a budget-bounded "relevant code" block ready to append as an
  untrusted context part. Index/search degrade silently — corrupt db is
  rebuilt, embedder-down falls back to lexical. Engine wiring: runs on a real
  repo workspace (project checkout or repo-backed) index it at run start
  (worker thread, time-capped, failures ignored) and each subtask gets the
  best-matching chunks as a "Relevant code (indexed)" untrusted part right
  after the repo map. Strictly conditional: greenfield/eval runs never build
  the index, so their prompts are byte-identical with the setting on or off.

## Search & palette
- Global search (`GET /api/search`, `search.py`): one query, ranked hits across
  all projects and four modalities — tasks (run rows, recency-boosted),
  memories (same hybrid recall a project uses), KB chunks (query embedded once,
  reused), file names/paths (never content).
- `cmd+K` command palette in the console fronts the same search.

## Playbooks, schedules & notifications
- Playbooks: 11 pre-tuned task templates (raise-coverage, upgrade-dependency,
  security-audit, refactor-module, document-codebase, fix-failing-tests,
  add-feature-tdd, dependency-refresh, doc-drift, dead-code, cut-a-release),
  one click from the project Overview
  (`GET /api/playbooks`, `POST /api/playbooks/{pid}/run`). The
  `cut-a-release` playbook prepares a release on the task branch (changelog
  from git history, version bump, CHANGELOG.md) and explicitly never tags or
  pushes — the human tags after acceptance.
- Autonomous maintenance mode (`maintenance.py`): per-project opt-in
  housekeeping on a cadence (interval hours or 5-field cron, validated by the
  schedules cron parser). The policy — `{enabled, cadence, budget_usd, tasks,
  last_run_at}` — lives under the `maintenance` key of the project's policy in
  the registry (same mechanism as the run-policy editor). Tasks are a subset
  of dependency-refresh / security-audit / doc-drift / dead-code, each backed
  by a playbook whose prompt makes "nothing to do" a first-class outcome:
  the agent completes WITHOUT changing files and says so, so a clean bill of
  health produces no delivery branch. `due_maintenance()` hands the server
  tick ready-to-enqueue payloads (rendered prompt/title/settings_overrides +
  the policy budget); `mark_maintenance_started()` advances the cadence.
  Wired: `GET/PUT /api/projects/{slug}/maintenance` edits the policy (PUT
  validates, 400 with the validator's message) and the server's ~120s ops tick
  enqueues due entries as normal runs (queue payloads tagged
  `maintenance: true`), then marks the project started once per pass.
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
- Web Push / PWA (`web/push.py` + `static/manifest.webmanifest`, `static/sw.js`,
  `static/icon.svg`): attention requests reach the phone as system
  notifications even with the console closed. VAPID keys are auto-generated on
  first use into `<data_dir>/vapid.json` (0600); browser subscriptions live in
  `<data_dir>/push_subscriptions.json`, deduped by endpoint and pruned when the
  push service reports them gone (404/410). Sending needs the optional
  `pywebpush` dependency (`pip install ai-dev-assistant[push]`); without it —
  or without `cryptography` for key generation — nothing breaks: the feature
  reports itself unavailable with a reason the UI can show. Payloads are
  `{title, body, tag, url}` with `url` a console deep link the service worker
  opens on tap. No offline caching by design — the console is a live dashboard.
  Wired: `GET /api/push/status` (availability + reason, public key,
  subscription count), `POST/DELETE /api/push/subscribe`, `POST /api/push/test`;
  the server's event dispatch pushes the same event kinds the `notify_events`
  selection allows (`{title: "[project] kind", body, tag: task_id,
  url: /app#task=<id>}`), fire-and-forget so a broken push service never
  touches the run path.

## Cost, analytics & benchmarks
- Cost attribution: pricing table populates `cost_usd` (budget guardrail trips
  on it), per-subtask cost deltas, token counts.
- Analytics API (read-only over the run store + event logs):
  `GET /api/analytics/overview | outcomes | project/{slug} | run/{task_id}` —
  spend dashboard, outcome ratios, per-run subtask breakdowns; 30-day spend
  snapshot on Home.
- Budget alerts (`analytics.check_spend_alerts` + `notify.notify_spend_alert`):
  30-day spend is compared against a monthly cap at 50/80/100% thresholds; the
  cap is the `monthly_budget_usd` setting (`ADA_MONTHLY_BUDGET_USD`; alerting
  only, 0 disables — `budget_usd` stays the per-run guardrail), passed in by
  the server's ~120s ops tick — analytics deliberately does not read the
  config schema.
  Each threshold fires at most once per UTC calendar month, tracked in
  `<data_dir>/spend_alerts.json` (reset on month rollover). Alerts are
  formatted and fanned out through the existing notification channels
  (webhook/Slack/email/desktop) as a `spend_alert` event that bypasses the
  per-event opt-in filter — crossing a budget threshold is always worth a ping.
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
- Editor integrations (`integrations/`): the console's REST + WebSocket API is
  editor-agnostic — `integrations/README.md` documents the three calls any
  editor needs (poll `/api/home` attention, answer via the steer note format,
  deep-link `/app#task=<id>`); `integrations/vscode/` ships an experimental
  status-bar + attention-answering extension (no build step, `ada.baseUrl` /
  `ada.token` for remote deployments).

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

## Operations: backup & restore
- `ada backup create [--out DIR]` (`backup.py`): archives the **data dir only**
  into `ada-backup-<UTC ts>.tar.gz` (default `<data_dir>/backups/`) — run store,
  per-project/global memory, knowledge graphs, `projects.json`, settings
  overlay, benchmark history, plus `users.json` and `vapid.json` (the user's
  own auth/push state — treat archives as sensitive). SQLite databases are
  snapshotted via the sqlite3 backup API first, so backing up under a live
  server never captures a torn write; `backups/`, `*.lock`, and WAL/SHM
  sidecars are excluded. Workspace checkouts and generated docs are **not**
  included — both are re-creatable/git-backed.
- `ada backup list` shows `<data_dir>/backups/` newest-first;
  `ada backup restore <archive> [--force]` refuses a non-empty data dir unless
  forced, and with `--force` **moves the current data dir aside** to
  `<data_dir>.pre-restore-<ts>` (never deletes) before extracting behind a
  path-traversal guard. Stop the server before restoring; restart it after.
- Over the API: `POST /api/backup` creates an archive (`{path, size}`),
  `GET /api/backups` lists them, and `GET /api/backup/download?path=` serves
  one — guarded to `<data_dir>/backups/` only (no traversal). Restore is
  deliberately CLI-only: it replaces the live data dir, auth state included.

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
