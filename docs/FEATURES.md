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

## CLI · evals · CI
- `ada run` (`-i`, `--continue`, `--ingest`) · `ada resume` · `ada server` ·
  `ada eval` (`--only/--json/--repeat/--timeout/--replay/--record-cassettes/--ab`).
- 10 multi-language golden tasks with held-out grading + toolchain skips;
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

## [planned] Project-level assistant (PLAN.md)
- Project lifecycle: create / import local (operate in place, never touches
  your branch or working tree) / import URL; one-time onboarding + incremental
  re-indexing; archive/delete.
- In-project tasks: worktrees off the durable project checkout, per-project
  policy (budget, effort, git mode, protected paths), branch-mode outcomes.
- Per-subtask review: accept/reject individual subtasks' commits; rejections
  feed learning.
- Cross-project fan-out: coordinator → parallel children → budget split →
  rollup report.
- UI commitments: a **live project activity view** (what is running/queued in
  any project right now, at a glance and per project), and a **Reviews &
  Permissions panel** on every task (per-subtask verdicts with evidence, plus
  the task's effective permissions: policy in force, tool allowlist, and any
  denied actions from the audit log).
- Project-first CLI/API; evals on ephemeral projects; per-run repo binding
  removed.
