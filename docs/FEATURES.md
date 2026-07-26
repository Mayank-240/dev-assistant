# Features & Flows

Living inventory of what the assistant does. Sections marked **[planned]** are
specified in [PLAN.md](PLAN.md) and not yet built.

## Task execution core
- Orchestrator planning: prompt → DAG of subtasks with acceptance criteria,
  routed by capability descriptions + past lessons + human feedback + agent
  track records + repo map. Structural validation (cycles/dupes/dangling/size).
- Interactive plan mode (CLI `-i`, web): propose → refine in English → approve.
- Rolling scheduler (dependents start the moment a dep finishes), session pool
  (cap, idle reaper, warm reuse), review-retries separate from transient
  retries, degrade-on-partial, opt-in adaptive replan.
- Guardrails: run budget, per-turn budget stop (anthropic), tool-gate starvation
  of over-budget SDK agents, per-subtask wall-clock cap, plan-size cap.
- Durability: checkpointed plan + subtask states; `resume`; `--continue`
  re-engagement; persistent queue (reorder/promote/concurrency/pause);
  cancellation kills the full child-process tree.

## Agents & tools
- 19 built-in specialists + reviewer/reflector/orchestrator: the dev-lifecycle
  spine (product_manager, architect, researcher, coder, test_engineer,
  documenter), quality roles (debugger, refactorer, security_auditor,
  accessibility_auditor, ux_reviewer), domain roles (devops, database,
  frontend, performance, integrator), and delivery roles (api_designer,
  migrator, release_manager). Review-oriented roles (documenter,
  security_auditor, accessibility_auditor, ux_reviewer) run read-only.
- Custom agents: operator-defined specialists in
  `<data_dir>/custom_agents.json` (name, description, when_to_use,
  system_prompt, tools, optional effort/model) join the roster next to
  built-ins and are routable by the orchestrator like any other role.
  Entries are validated on load (slug name, no collision with built-ins,
  tools must exist in the toolbox, effort tier checked); bad entries are
  skipped with a logged warning, and a missing/corrupt file simply means no
  customs. `role_models` routing and per-role effort apply to customs too.
- Tools: memory/KB/KG; file read (offset/limit) / write / edit (uniqueness,
  replace-all) / 3-way patch / list / ripgrep grep; `symbols` /
  `find_references`; sandboxed `run_command`, validated `install_packages`,
  targeted `run_tests`; git status/diff; messages + blackboard; depth-1
  `delegate`; SSRF-guarded `web_fetch` (opt-in).

## Verification
- Reviewer judges each subtask against its criteria from the files it actually
  changed (untrusted-enveloped), per-subtask verdict with criteria breakdown.
- Objective gate on the delta vs a pre-run baseline: subtask-scoped tests,
  new-lint penalty, pluggable signals (typecheck/build/coverage); pre-existing
  failures never blamed; scoped-green-only soft-pass, capped score.
- Test detection: Python, JS, Go, Rust, Java, Ruby. Final workspace test run;
  0-100 quality score.

## Context, memory & learning
- Symbol-ranked repo map (AST + import centrality + query relevance);
  per-subtask context pack under a per-model token budget; prompt caching.
- Memory: hybrid semantic+lexical recall (RRF), dedup, decay, caps,
  project/global scopes; KB ingest/search; per-project knowledge graph
  (merge-on-save); reflection lessons (DO/AVOID/ROUTING); feedback → planning.
- Knowledge-graph distillation: `ai-dev-assistant project distill <slug>
  [--dry-run]` consolidates the graph's domain layer as runs accumulate — pure
  heuristics plus local embeddings, no LLM calls, deterministic and free. It
  merges near-duplicate concepts (plural/hyphen/stopword id variants; label
  embedding cosine ≥ 0.92 when a real embedder is active — the hash backend
  skips semantic merges), redirecting the dropped node's edges with provenance
  unioned and load-merge weight semantics; prunes stale low-weight domain edges
  (weight < 2 and untouched > 45 days, with the top-20 weighted hubs protected)
  and removes orphaned degree-0 concepts. Run-layer bookkeeping edges are never
  touched, merges never cross node types, a second pass is a no-op, and graphs
  under 10 domain edges are left alone. `--dry-run` prints the proposed
  merge/prune/orphan report without changing anything.

## Real-repo work
- Materialize local path / git URL; branch+commit delivery; per-subtask git
  worktrees with conflict-aware merge-back.

## Security
- SDK built-ins confined by deny-hook; toolbox path sandbox + secret denylist;
  exec hardening (scrubbed env, rlimits, process-group kill, per-run kill tags)
  with optional bwrap/Docker no-network backends; redaction at every durable
  boundary; untrusted envelopes; audit log; bearer-token web auth + CORS;
  hardened non-root container deployment (see DEPLOYMENT.md).

## Web UI
- Composer (effort, memory scope, budget, plan review), live run view (DAG,
  streaming agent cards, timeline, metrics, budget meter), diff viewer, files
  browser, run comparison, dashboard + quality trend, queue management,
  pause/steer, feedback, resume, project selector, memory/KG views, token-auth,
  accessibility (dialogs, keyboard, contrast, reduced motion).
- Full per-subtask agent transcripts: every step an agent takes — thinking, text,
  tool calls, tool results (error-flagged) and the final result — is persisted
  (~4k chars per step) as `agent_step` events in the task's `events.jsonl` and
  served via `GET /api/runs/{task_id}/transcript/{subtask_id}`. The agent detail
  modal's "Full transcript" action opens a kind-styled, live-following viewer.

## CLI · evals · CI
- `ada run` (`-i`, `--continue`, `--ingest`) · `ada resume` · `ada server` ·
  `ada eval` (`--only/--json/--repeat/--timeout/--replay/--record-cassettes/--ab/--record-history`).
- 11 golden tasks (multi-language + one cross-project fan-out task) with
  held-out grading + toolchain skips;
  offline replay cassettes in CI; secret-gated live-eval workflow; A/B knob
  harness; retrieval benchmark. CI: pytest + node tests + replay eval.
- Per-task artifacts: plan/report/brief/activity docs, streamed events.jsonl,
  trace spans, audit log, per-subtask cost.
- Benchmark tracking over time: `ada eval --record-history` (and
  `python -m ai_dev_assistant.evals.replay_eval --record-history`, or
  `ADA_EVAL_RECORD_HISTORY=1`) appends one JSONL entry per suite run to
  `<data_dir>/benchmarks.jsonl` — timestamp, short git SHA + dirty flag, suite,
  pass rate, quality mean/min, cost, duration, run count — so you can see
  whether prompt/agent changes actually improve the assistant across commits.
  View via `evals.history.trend_report(settings)`: latest entry, deltas vs the
  previous same-suite entry, and a per-SHA series ready for charting (a UI
  surface may come later). Recording is strictly opt-in; default eval behavior
  is unchanged.

## Core flows
1. Greenfield task: prompt → plan → parallel agents → verify → tests → docs →
   optional branch.
2. Repo-backed task: materialize → onboard → baseline → plan → execute
   (worktrees) → delta-gated verify → branch delivery.
3. Interactive plan → refine → run.
4. Queue: submit N → pump at configured concurrency → survives restart.
5. Re-engage (`--continue`) / Resume (checkpoints).
6. Pause/steer mid-run; feedback afterward → next plan.
7. Evals: live suite · offline replay (CI) · `--ab` comparison.
8. Deploy: compose up → token from logs → container is the boundary.

## Project-level assistant (PLAN.md — all three slices SHIPPED)
- Project lifecycle: create greenfield (own git checkout) / import local
  **in place** (read-only contract: the assistant never commits, switches
  branches, or writes in your repo — only `ada/*` branches) / import git URL
  (clone); one-time onboarding + git-diff incremental re-indexing; policy
  storage; archive/delete (never removes a local-origin directory).
- In-project tasks: git worktrees off the durable checkout (outside your dir),
  per-subtask worktrees + commits ON by default, per-project policy (budget,
  effort, git_mode branch|merge, protected paths — enforced at the tool
  boundary with DENIED-audited refusals), branch-mode outcomes; worktrees
  persist for review/continue/resume.
- Per-subtask review: Reviews & Permissions panel on every task — verdicts with
  per-criterion evidence + changed files + diffs, resolved policy, tools per
  agent, denied actions from the audit log; Accept cherry-picks that subtask's
  commit into the review target (owned checkouts advance directly; in-place
  projects use `ada/integration`), Reject records feedback for learning.
- Live activity: per-project status + activity strip, all-projects activity
  table, project home with policy editor, run history, and quality sparkline.
- Cross-project fan-out (slice 3): one prompt over N projects → one child task
  per project (own worktree, baseline, `ada/<child-id>` branch), run in
  parallel with optional `--stagger` (first child's lessons inform the rest),
  parent budget split into per-child caps, and a rollup report — per-project
  verdict table with cost/quality/branch/review-target, parent ↔ child
  `parent_id` lineage (parent row `project="multi"`), failure isolation (one
  child failing never blocks the others); cross-project composer + parent view.
- Cross-project task dependencies: fan-out runs accept an optional
  `project_deps` map (`{slug: [upstream slugs]}` on `POST /api/run`, `deps=` on
  `run_cross_project`) — dependents run in topological waves after their
  upstreams, with each upstream's status and summary (bounded, failures
  included) appended to the dependent's prompt under an
  `--- Upstream results ---` section. Unknown slugs and cycles are rejected up
  front (HTTP 400); without deps, behavior and child prompts are unchanged. The
  rollup report records the dependency order.
- Workspaces: a named group of inter-related projects, stored in
  `workspaces.json` next to the project registry. A project belongs to at most
  ONE workspace — assigning it elsewhere moves it; deleting a workspace removes
  only the group (projects survive ungrouped), and deleting a project
  unassigns it. Each workspace carries default dependencies
  (`{slug: [upstream slugs]}`, validated against members and acyclic via the
  fan-out's own validator), and `workspace_run_spec(settings, ws_slug, subset)`
  expands the workspace — or a validated subset, dropping edges to excluded
  members — into the `projects` + `deps` payload fed straight into the
  existing cross-project fan-out run path.
- Workspace-aware context (`workspace_context`, env `ADA_WORKSPACE_CONTEXT`,
  on by default): when the active project belongs to a workspace with at least
  one sibling, each subtask's context gains one extra part — "Related knowledge
  from workspace '<name>'" — holding the top memories (top 2, same min-score as
  the project's own recall) and KB hits (top 2) from up to 3 sibling projects,
  queried with the same text as the project's own knowledge lookup. Every item
  is attributed `[<sibling slug>] …`, the whole section is wrapped as untrusted
  content (`source="workspace-sibling"`), capped at ~1500 chars, and placed
  AFTER the project's own recall/KB parts so it yields first under the context
  budget. Sibling stores are opened read-only via the memory module's
  never-create wrapper — a sibling without a `memory.db` contributes nothing
  and no files are created; per-sibling failures are swallowed (debug log).
  Projects outside any workspace assemble byte-identical prompts to before.
- Cross-project golden eval task (`cross_logging_fix`): two sub-repo fixtures
  imported as two ephemeral projects, driven through the real fan-out path and
  graded per child on held-out tests plus a rollup grader.
- Project-first CLI (`ada project …`, `ada run -p a -p b`) and API; evals run
  through ephemeral projects; per-run repo binding removed.

## Feature wave (shipped)
- Global settings console (sidebar → Settings): 38 whitelisted settings across 8
  groups with source badges, per-field reset, live application to new runs.
- Playbooks: 7 pre-tuned task templates, one click from the project Overview.
- Scheduled tasks: recurring per-project runs with a 60s server tick.
- Notifications: webhook + macOS desktop channels, in-app notification center.
- Global search + cmd+K palette across tasks, memories, KB, and files.
- Cost analytics: spend dashboard, outcome ratios, per-run subtask breakdowns.
- Per-role model routing (`role_models` setting, env `ADA_ROLE_MODELS`):
  comma-separated `role=model` pairs route individual agent roles to their own
  model while everything unmapped stays on the single default — e.g.
  `documenter=claude-haiku-4-5,test_engineer=claude-sonnet-4-6` sends docs and
  test-writing to cheaper models while coder/architect keep the default,
  cutting cost without touching quality-critical work. `reviewer=...` overrides
  the verdict reviewer (otherwise governed by `orchestrator_model`); the
  orchestrator itself is not routable. Malformed pairs and unknown role names
  are skipped with a logged warning. Works on both backends — on the default
  `claude_sdk` backend the values are Claude Code model ids
  (haiku/sonnet/opus family); blank keeps today's single-model behavior.
- GitHub integration: labeled issues → runs → evidence-first PRs (poll-based;
  token env-only via ADA_GITHUB_TOKEN).
- A/B replay smoke from the UI; KB drag-and-drop upload; knowledge-graph
  filter/focus; timeline zoom with per-subtask cost; first-run tour.

## QoL wave (shipped)
- Memory curation API: `GET/PATCH/DELETE /api/projects/{slug}/memories[/{id}]`
  — paginated listing (limit/offset/scope), edit (re-embeds), forget (drops the
  vector too); 404 on unknown project or memory id.
- Workspace GC: `GET /api/projects/{slug}/gc` (dry-run report) and
  `POST /api/projects/{slug}/gc` (`{keep_days?, ids?}`) reclaim worktrees of
  finished runs and fully-accepted `ada/*` branches older than the retention
  (`gc_keep_days` setting, default 14; env `ADA_GC_KEEP_DAYS`). Conservative by
  design: persist-for-review stays the default, `ada/integration` and resumable
  runs are never collected, and nothing runs automatically.
- Rollback of accepted subtasks: Accept now records the cherry-pick sha
  (`accepted_commit`), and `POST /api/runs/{task_id}/subtasks/{subtask_id}/rollback`
  reverts exactly that commit on the review target (owned checkouts in place;
  in-place projects via `ada/integration` temp worktree) — 409 with the error on
  conflict, never forced.
- GitHub PR-comment follow-ups (opt-in `github_pr_followups` /
  `ADA_GITHUB_PR_FOLLOWUPS`): the poller watches the assistant's own open PRs;
  each new reviewer comment batch becomes a follow-up run — re-engaging the
  original task (continue_from lineage, same workspace + `ada/<task-id>` branch)
  when the head branch maps to a known run, else a fresh run on the repo's
  project. Per-PR last-seen timestamps persist in `github_seen.json`; every
  batch is a billed run. The loop closes back to the reviewer: when a
  follow-up run reaches a terminal status, the poller posts a result comment
  on the PR — "Addressed reviewer feedback — branch updated." with a compact
  subtask-verdict table and the cost for a completed run, or an honest
  did-not-complete note naming the status otherwise. Pending result comments
  persist under `pr_pending` in `github_seen.json`; a failed post retries on
  later ticks (capped at 3 attempts), and a successful post bumps the PR's
  last-seen cursor so the bot's own comment never seeds a new follow-up.
- Cron schedules: schedule create/update accept a 5-field `cron` expression as
  an alternative to `every_hours` (mutually exclusive; validated with a clear
  400 on bad expressions); the 60s tick fires on matching local-time minutes.
- Slack + email notification channels: Slack incoming webhook (same SSRF guard
  and redaction as the generic webhook) and stdlib SMTP email (password
  env-only via `ADA_SMTP_PASSWORD`) ride the same notify dispatch as
  webhook/desktop, configurable live from the settings console.

## Away-wave server wiring (shipped)
- Knowledge graph v2 API: `GET /api/projects/{slug}/graph2`
  (`?layer=&min_weight=&limit=` → export_view + stats),
  `GET .../graph2/node/{node_id}` (`?depth=&layer=`, path-style node ids
  supported) and `GET .../graph2/search?q=` over the same per-project KG file,
  opened read-only. The legacy `GET /api/graph` shape is unchanged.
- Workspaces API: `GET/POST /api/workspaces`, `PATCH/DELETE /api/workspaces/{ws}`,
  member assign/unassign (`POST/DELETE /api/workspaces/{ws}/projects[...]`),
  `PUT /api/workspaces/{ws}/deps` (validated; 400 with the reason), and
  `POST /api/workspaces/{ws}/run` — expands via `workspace_run_spec` and
  re-enters the multi-project `/api/run` path (identical payload plus an
  additive `workspace` attribution key). `GET /api/projects` items carry an
  additive `workspace: slug|null` field.
- Custom agents API: `GET /api/agents` now returns
  `{builtin, custom, tools}` (builtins with tools/effort, customs as their raw
  editable specs, plus the toolbox universe); `POST /api/agents {spec}`
  validates and upserts (400 with the reason);
  `DELETE /api/agents/{name}` (400 for builtins, 404 when absent). New runs
  pick customs up automatically via per-engine `build_agents`.
- Home aggregation: `GET /api/home` — open ask/permission requests across
  running tasks, running/queued/recent runs, a 30-day spend snapshot,
  benchmark trend, workspace summaries, and counts; each section degrades to its
  empty default with an `errors` entry, never a 500.
