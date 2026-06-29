# Architecture & Capabilities

This document maps the system's capabilities, organized by the five tiers they were built
in. Each tier builds on the last: reliability first (so runs finish and report the truth),
then real-repo capability, then trustworthy verification, then cross-run learning, then
observability/eval/security/deploy.

## The run, end to end

```
ada run "<task>"
  └─ (optional) materialize a real repo into workspace/<id>/        [Tier 2]
  └─ orchestrator.make_plan  ← repo map + prior lessons + agent track record   [Tier 2/4]
  └─ run.validate()  — reject cycles / dup ids / dangling deps        [Tier 3]
  └─ scheduler.run(run)                                                [Tier 1]
        for each ready batch (deps satisfied incl. soft-success):
          execute subtask  → sandboxed agent w/ real file/exec/git tools   [Tier 2]
          verify subtask   → LLM reads file CONTENTS, then objective gate   [Tier 3]
                              (failing tests hard-fail; green tests override nitpicks)
          on exhausted retries with real output → PASSED_WITH_CAVEATS  [Tier 1]
        transient LLM errors retried with backoff (not a review retry)  [Tier 1]
        (optional) adaptive replan injects a repair subtask             [Tier 3]
  └─ run whole-workspace tests · enrich KG · git branch+commit          [Tier 2/3]
  └─ summarize · reflect (outcome-aware lessons) · index artifacts into KB   [Tier 4]
  └─ record agent outcomes · compute quality score · write docs+events+trace [Tier 4/5]
  └─ honest rollup status: completed / partial / failed                 [Tier 1]
```

## Tier 1 — Reliability: runs finish and report the truth

- **Degrade-on-partial** (`scheduler.py`, `task.py`): a subtask that produced real output
  but failed review becomes `PASSED_WITH_CAVEATS`. It satisfies dependencies (so dependents
  run) and tags the result with a caveat. This kills the dominant `0/3`/`1/3` cascade where
  one strict-review miss `BLOCKED`ed the whole downstream subtree.
- **Honest status rollup** (`engine.py`, `run_store.py`): the run's terminal status is
  derived from subtask outcomes — `completed` (all passed) / `partial` / `failed` — and
  web/LLM errors mark the run `failed` instead of stranding it `running`.
- **JSON self-repair** (`jsonout.py`): `repair_json` recovers a valid object from prose,
  mid-text fences, trailing commas, and smart quotes, so a slightly-malformed plan/verdict
  no longer aborts the run.
- **Provider resilience** (`resilience.py`, `client.py`): every LLM HTTP call has a timeout
  and bounded exponential backoff (honoring `Retry-After`); persistent transient failures
  raise `TransientLLMError`, which the scheduler retries with backoff **without** consuming
  the substantive review-retry budget.
- **Concurrency safety**: locks around the shared SQLite connection, the NetworkX graph, and
  an atomic `UsageTotals`.
- **SDK fixes**: the Claude SDK backend now honors the per-call `model` argument.

## Tier 2 — Real-repo capability + safe execution

- **Repository binding** (`vcs.py`, `engine.py`): `ADA_REPO_URL` clones, `ADA_REPO_PATH`
  copies a working tree into the sandbox (never mutated in place). The agent toolbox is
  rooted at the workspace (fixing the old `read_file`-reads-the-assistant's-own-tree bug).
- **Real file tools** (`tools/registry.py`): `write_file`, `edit_file`, `apply_patch`,
  `list_dir`, `grep`, `git_status`, `git_diff` — plus a hardened `read_file` with a
  secret-file denylist and symlink-escape guard.
- **Sandboxed execution** (`execution.py`): `run_command`/`install_packages` run with a
  scrubbed environment (no API keys leak), POSIX rlimits (CPU/address-space), and a new
  process group killed as a whole on timeout. The SDK built-in tool allowlist denies `Bash`
  and web access by default.
- **Codebase onboarding** (`knowledge/repo_map.py`): a token-bounded repo map feeds the
  planner, and source is indexed into the KB (so `kb_search` goes live) and KG.
- **Delivery** (`ADA_GIT_FINALIZE`): commit the workspace on an `ada/<run-id>` branch.

## Tier 3 — Trustworthy verification & smarter decomposition

- **Objective-gated verdicts** (`verification.py`): the reviewer reads actual file contents
  (not just names); the gate runs the subtask's tests + lint. Failing tests **hard-fail**
  regardless of the LLM's opinion; green tests downgrade an LLM nitpick to a soft pass.
- **Per-criterion verdicts** (`schemas.py`): `Verdict.criteria` gives an evidence-backed
  breakdown of which acceptance criterion failed.
- **DAG + plan validation** (`task.py`, `orchestrator.py`): Kahn topo-sort rejects cycles,
  duplicate ids, and dangling deps before scheduling; empty acceptance criteria are
  backfilled so the reviewer is never ungrounded.
- **Interactive plan mode** (`orchestrator.refine_plan`, `/api/plan/refine`, `run -i`):
  propose a plan, then refine it with natural-language instructions ("add a security review
  step", "merge steps 2 and 3") — the orchestrator returns a revised, re-validated DAG — and
  loop until you approve. Works in the web plan panel and the CLI.
- **Re-engage a completed task** (`engine.run(continue_from=...)`, `run --continue`, web
  "↻ Re-engage"): a finished task can be continued — its workspace is carried forward (copied
  into the new run), its prompt + outcome frame the new plan, and the run is linked to its
  parent (`runs.parent_id`). Each follow-up is its own run that builds on the last.
- **Adaptive replanning** (`engine.py`, `scheduler.py`): a bounded hook injects a repair
  subtask for a failed one (`ADA_ADAPTIVE_REPLAN`).
- **Token-budgeted context** (`context.py`): the agent prompt is assembled to a budget
  instead of unbounded concatenation.

## Tier 4 — Cross-run learning that actually improves

- **Outcome-aware reflection** (`agents/reflector.py`): one structured call distills typed
  `what_worked` / `what_to_avoid` / `routing_notes` lessons conditioned on the real pass
  ratio — no more writing happy-path lessons for a failed run.
- **Memory hygiene** (`memory/`): relevance-thresholded recall, recency **decay**,
  `remember_unique` dedup, and a vector **dimension guard** so a fastembed→hash fallback can
  no longer crash planning.
- **Live KB** (`engine._index_artifacts`): briefs + produced source are re-indexed each run.
- **Human feedback** (`run_store.py`, web): rate / accept / comment a finished run — the
  highest-signal learning input.
- **Learned routing**: per-agent pass rates are recorded and surfaced to the planner.

## Tier 5 — Observability, evaluation, cost, security, deploy

- **Eval harness** (`evals/`, `ada eval`): golden tasks graded by deterministic graders
  (`file_exists`, `ast_defines`, `tests_pass`) into a scorecard (pass/fail, cost, wall-time).
- **Record/replay** (`llm/record_replay.py`): capture real provider responses to cassettes
  and replay them offline for deterministic regression of the LLM/JSON-repair layer.
- **Cost attribution** (`llm/pricing.py`, `usage.py`): a pricing table populates `cost_usd`
  on the API backend (so the budget guardrail trips), with per-subtask cost deltas.
- **Quality score + dashboard**: a 0-100 score per run, plus a web Dashboard with total cost,
  average quality, status breakdown, and per-agent track record.
- **Durable telemetry**: `events.jsonl` (replayable event log), `trace.jsonl` (per-phase
  spans), and `audit.jsonl` (every tool dispatch) under `docs/<id>/`.
- **In-run control**: pause/resume between batches and steer the next subtask from the UI.
- **Defense in depth** (`security/redaction.py`): secret redaction on tool output/docs/
  memory, an `<untrusted>` envelope for external content, and an audit log.
- **Deploy**: a working multi-stage `Dockerfile` (runs the real app as an unprivileged user)
  with `/healthz` and `/readyz`.

## Configuration

Every capability above is gated by an `ADA_*` environment variable — see
[`.env.example`](../.env.example) for the full list and defaults.
