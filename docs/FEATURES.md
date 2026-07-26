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
- 14 specialists + reviewer/reflector/orchestrator.
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
  `ada eval` (`--only/--json/--repeat/--timeout/--replay/--record-cassettes/--ab`).
- 11 golden tasks (multi-language + one cross-project fan-out task) with
  held-out grading + toolchain skips;
  offline replay cassettes in CI; secret-gated live-eval workflow; A/B knob
  harness; retrieval benchmark. CI: pytest + node tests + replay eval.
- Per-task artifacts: plan/report/brief/activity docs, streamed events.jsonl,
  trace spans, audit log, per-subtask cost.

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
  batch is a billed run.
- Cron schedules: schedule create/update accept a 5-field `cron` expression as
  an alternative to `every_hours` (mutually exclusive; validated with a clear
  400 on bad expressions); the 60s tick fires on matching local-time minutes.
- Slack + email notification channels: Slack incoming webhook (same SSRF guard
  and redaction as the generic webhook) and stdlib SMTP email (password
  env-only via `ADA_SMTP_PASSWORD`) ride the same notify dispatch as
  webhook/desktop, configurable live from the settings console.
