# AI Dev Assistant — Feature Plan: the Project-Level Assistant

Status: **draft for decision** — nothing below is built yet. The assistant is not
deployed anywhere, so this plan assumes a clean break: no migrations, no
backward-compatibility shims. Legacy per-run behavior is replaced, not preserved.

---

## 1. Vision

Move the assistant from a *task runner* (every run starts from scratch in a
throwaway workspace) to a *project workbench*: the *project* is the durable unit
that owns a repository, accumulated knowledge, history, and policy — and tasks
are events inside it. The product thesis stays what it was: **verified,
measurable work a user doesn't have to re-check** — projects are how that trust
compounds instead of resetting every run.

### Goals
- A project owns one durable repo checkout; tasks stop re-cloning/re-indexing.
- Context compounds per project: memory, knowledge, baselines, track records,
  quality trends.
- Tasks can target one project or fan out across several, with honest
  per-project verification and one rolled-up report.
- Every surface (CLI, web, API) is project-first.

### Non-goals (this plan)
- Multi-user auth / SaaS hosting (stays single-team, self-hosted).
- IDE plugins.
- GitHub App / PR automation (next plan — the review surface here is local).
- New agent roles.

---

## 2. Domain model

```
Project
├─ identity        slug, name, created_at
├─ source          origin: greenfield | local-path | git-url; default_branch
├─ checkout        workspace/<slug>/repo — the ONE durable git working copy
├─ knowledge       data_dir/projects/<slug>/{memory.db, knowledge_graph.json}
│                  + KB index + repo map cache + last_indexed_commit
├─ baseline        test/lint/signal snapshot @ commit (refreshed when HEAD moves)
├─ policy          default budget, effort, git mode, protected paths, sandbox mode
└─ history         runs, feedback, agent track record, quality trend (project-scoped)

Task (always belongs to exactly one project)
├─ worktree        checkout/.ada_worktrees/<task-id> (existing machinery, one level up)
├─ run             plan → schedule → verify → checkpoint (existing engine)
└─ outcome         branch ada/<task-id> merged or left for review, per policy

Cross-project task (meta-task)
├─ targets         [project slugs]
├─ children        one ordinary Task per target project
└─ rollup          per-project verdict table + combined cost + links
```

Global memory (cross-project lessons) already exists and is retained; knowledge
graphs remain strictly per-project.

---

## 3. Features

### F1 — Project lifecycle
- **Create (greenfield):** provisions the data dir *and* `workspace/<slug>/repo`
  with `git init` + an empty initial commit. All future tasks build up this repo.
- **Import from local path:** clone the path into the project checkout (the
  user's original directory is never mutated — same guarantee as today).
- **Import from git URL:** clone (optionally at a ref) into the checkout.
- **On create/import — one-time onboarding:** ranked repo map, KB ingestion,
  KG enrichment, baseline capture. Stored with `last_indexed_commit`.
- **Incremental refresh:** before each task, re-index only files changed since
  `last_indexed_commit` (git diff), re-capture the baseline only when HEAD moved.
- **Archive/delete:** archive hides a project; delete removes data dir +
  checkout after confirmation. No orphaned run docs.

### F2 — In-project tasks
- A task runs in a **git worktree off the project checkout** (reusing the
  wave-3 primitives) — never a copy. Parallel subtask worktrees nest under the
  task worktree exactly as they do today.
- **Verification** gates on the *project* baseline (delta semantics unchanged).
- **Outcome per project policy:**
  - `merge` — auto-merge the task branch into `default_branch` on pass;
  - `branch` — leave `ada/<task-id>` for review (default);
  - conflicts always fall back to `branch` + a clear note.
- **Protected paths:** policy globs (e.g. `infra/**`, `*.lock`) the agents'
  write tools refuse to touch for this project.
- **Continuity:** `--continue` and `resume` operate on task lineage within the
  project; no workspace copying.
- Run records carry `project_id`; queue entries carry the target project so a
  restarted server resumes into the right checkout.

### F3 — Cross-project tasks
- **Fan-out:** user selects N projects; a coordinator pass produces one child
  task per project, grounded in *that* project's repo map + lessons + track
  record.
- **Execution:** children are ordinary F2 tasks — own worktree, own baseline,
  own branch — running in parallel, bounded by the session pool. Optional
  `stagger` mode runs the first child alone so its lessons (written to global
  memory) inform the rest — measurable via the A/B harness.
- **Budget:** one parent budget split across children with per-child caps; the
  parent stops scheduling new children past the cap (existing semantics, one
  level up).
- **Rollup:** parent report = per-project verdict table (passed/branch/conflict,
  cost, quality), combined brief, links to each child's docs and branch. Parent
  ↔ child lineage uses the existing `parent_id` chain.
- **Failure isolation:** one child failing never blocks the others; the rollup
  is honest about partial success (reusing degrade semantics).

### F4 — Project home (web UI)
- The current project *selector* becomes a project *home*: repo status (branch,
  HEAD, dirty?), last-indexed info, run history with quality trend, memory & KG
  tabs (exist), policy editor, and the task composer scoped to the project.
- **Live project activity (requirement):** the UI must show what is currently
  going on in any project — per-project: the running task(s) with their live
  event stream, queued tasks, and the last completed run; globally: an
  at-a-glance activity strip listing every project's current state
  (idle / running <task> / N queued). Backed by a
  `GET /api/projects/{slug}/activity` endpoint joining running set + queue +
  run history by project.
- **Reviews & Permissions panel (requirement):** one click from any task view:
  - *Reviews*: every subtask's verdict — pass/fail, score, per-criterion
    breakdown, objective evidence (tests/lint/signals vs baseline), attempts,
    and the changed files it was judged on;
  - *Permissions*: the task's effective permissions — resolved policy in force
    (budget, effort, git mode, protected paths, sandbox mode, web access),
    the tool allowlist per agent, and any DENIED actions pulled from the audit
    log (path escapes, secret-file hits, budget-gate denials, protected-path
    refusals).
- **Cross-project composer:** project multi-select + the existing controls; the
  parent task view shows a children grid with live per-project status.
- **Review surface (per-subtask, per decision #5):** diff + criteria checklist
  + verification evidence per subtask, with per-subtask accept (= merge that
  subtask's commit) / reject (= record feedback, keep on the task branch).

### F5 — CLI
```
ada project new <name>
ada project import <path-or-url> [--name X] [--ref Y]
ada project list / show <slug> / archive <slug> / delete <slug>
ada run -p <slug> "task"                # in-project (default: sole project if only one)
ada run -p a -p b -p c "task"           # cross-project fan-out
ada resume <task-id>                    # unchanged, project-aware
ada eval / --ab                         # gains -p to scope to a project's history
```
`ADA_REPO_PATH`/`ADA_REPO_URL` env binding is **removed** (clean break) —
importing a project is the one way to bind a repo.

### F6 — API
- `/api/projects`: full CRUD + import + `GET .../status` (repo state) +
  `GET .../trend` (quality/cost over time).
- `RunRequest`: `projects: [slug, ...]` (1 = in-project, >1 = fan-out) replaces
  the per-run `repo_path/repo_url` fields (removed).
- Parent/children endpoints for cross-project tasks; everything else unchanged.

### F7 — Policy & safety at project level
- Policy fields: `budget_usd`, `effort`, `git_mode (merge|branch)`,
  `protected_paths[]`, `sandbox`, `allow_web`, `max_plan_subtasks`.
  Precedence: task override → project policy → global defaults.
- Audit/trace/events stay per-task but are browsable from the project home.
- Untrusted-repo stance: importing a URL the user hasn't vetted surfaces a
  banner recommending container deployment (ties to DEPLOYMENT.md); optionally
  a policy flag `container_required` that refuses to run outside one.

### F8 — Evals follow the model
- Golden tasks run inside ephemeral projects (create → import fixture → task →
  grade → delete), exercising the real F1/F2 path.
- New golden coverage: one cross-project fixture task (two tiny repos, shared
  fix) grading the fan-out + rollup.
- Per-project quality trend feeds the dashboard; `--ab` can scope to a project.

---

## 4. Build order

Three slices, each landing green (tests + replay eval + CI) before the next:

| Slice | Contents | Rough size |
|---|---|---|
| **1. Projects own repos** | F1 (lifecycle, import, one-time + incremental indexing), F5 project commands, F6 project endpoints, run records gain `project_id` | the foundation; touches config/engine/vcs/projects/server/cli |
| **2. Tasks live in projects** | F2 (worktree-off-checkout, policy incl. protected paths + git modes), F4 project home + review v1, F8 eval harness moves to ephemeral projects | engine + web, biggest slice |
| **3. Fan-out** | F3 (coordinator, parallel children, stagger option, budget split, rollup), cross-project composer + parent view, cross-project golden task | mostly new orchestration code |

Clean-break removals alongside slice 1: per-run repo binding (env + RunRequest
fields), per-run workspace copies, `--continue`'s workspace-copy path.

---

## 5. Decisions (settled 2026-07-25)

1. **Default git mode: `branch`.** Passing tasks leave `ada/<task-id>` for
   review; merge happens on acceptance in the review surface.
2. **Local import: operate in place.** The user's directory *is* the project
   checkout. Safety contract that makes this acceptable:
   - task worktrees live OUTSIDE the user's dir (`workspace/<slug>/worktrees/…`
     via `git worktree add` pointing at the user's repo) — no `.ada_worktrees`
     debris inside it;
   - the assistant NEVER switches the user's checked-out branch, never commits
     to it, never touches their working tree — it only creates `ada/*` branches
     and refs; acceptance merges into `default_branch` only via the review flow;
   - a dirty user working tree never gets snapshot-committed; tasks branch from
     HEAD and a warning notes uncommitted changes aren't visible to agents.
   Git-URL imports still clone into `workspace/<slug>/repo`.
3. **Cross-project stagger: OFF by default**, `--stagger` opt-in; the A/B
   harness decides whether it earns a better default.
4. **`default` scratch project: kept** — `ada run "…"` with no `-p` lands there.
5. **Review v1: per-subtask acceptance.** Worktree-per-subtask merges give
   exact per-subtask commits, so acceptance = cherry-pick/merge the accepted
   subtasks' commits into `default_branch`; rejected subtasks record feedback
   and their commits stay on the task branch. Requires per-subtask diff
   attribution in the UI (the engine already tracks per-subtask changed files
   and merge commits). This grows slice 2 and makes `worktree_per_subtask` the
   DEFAULT for project tasks rather than opt-in — it is what makes per-subtask
   acceptance possible.

---

## 6. Success criteria

- Second task in a project starts with **zero** re-indexing cost (measured in
  run events) and plans with project history in context.
- A 3-project fan-out produces 3 verified branches + one rollup report, with
  per-child cost attribution, in one command.
- Full suite + replay eval green; eval harness exercises the real project path.
- No per-run repo binding remains anywhere in code or docs.
