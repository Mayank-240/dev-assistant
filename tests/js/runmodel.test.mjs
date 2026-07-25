// Unit tests for the run-view model helpers in web/static/util.js:
// event-stream aggregation, budget meter, compare rows, timeline rows,
// resume eligibility. Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  makeAgentRecord, initialRunAggregates, reduceRunEvent, runProgress, formatStepLine,
  computeBudgetMeter, timelineRows, compareRowModel, isResumable, RESUMABLE_STATUSES,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- makeAgentRecord ----
test("makeAgentRecord builds a queued record with defaults", () => {
  const r = makeAgentRecord({ id: "s1", agent: "coder" });
  assert.equal(r.id, "s1");
  assert.equal(r.agent, "coder");
  assert.equal(r.title, "");
  assert.deepEqual(r.depends_on, []);
  assert.equal(r.status, "queued");
  assert.equal(r.score, null);
  assert.deepEqual(r.steps, []);
  assert.equal(r.start, null);
  assert.equal(r.end, null);
});

// ---- reduceRunEvent: a full synthetic run ----
function planEvent() {
  return {
    type: "plan",
    data: {
      subtasks: [
        { id: "s1", agent: "coder", title: "Write it", depends_on: [] },
        { id: "s2", agent: "test_engineer", title: "Test it", depends_on: ["s1"] },
      ],
    },
  };
}

test("reduceRunEvent aggregates a plan/start/review/done stream in order", () => {
  const s = initialRunAggregates();
  assert.equal(s.phase, "plan");

  reduceRunEvent(s, planEvent());
  assert.equal(s.total, 2);
  assert.equal(s.reviewed.size, 0);
  assert.equal(s.phase, "execute");
  assert.equal(s.agentData.s1.status, "queued");
  assert.deepEqual(s.agentData.s2.depends_on, ["s1"]);

  reduceRunEvent(s, { type: "subtask_start", ts: 10, data: { id: "s1", agent: "coder" } });
  assert.equal(s.agentData.s1.status, "running");
  assert.equal(s.agentData.s1.start, 10);
  assert.deepEqual(s.timeline.s1, { agent: "coder", start: 10, end: 10 });

  reduceRunEvent(s, { type: "agent_step", data: { id: "s1", kind: "tool", tool: "Write", input: "x.py" } });
  reduceRunEvent(s, { type: "agent_step", data: { id: "s1", text: "done writing" } });
  assert.equal(s.agentData.s1.steps.length, 2);
  assert.equal(s.agentData.s1.steps[1].kind, "text");

  reduceRunEvent(s, {
    type: "subtask_review", ts: 15,
    data: { id: "s1", passed: true, score: 9, attempts: 1, reasons: ["clean"], result: "ok", cost_usd: 0.01 },
  });
  assert.equal(s.agentData.s1.status, "passed");
  assert.equal(s.agentData.s1.passed, true);
  assert.equal(s.agentData.s1.score, 9);
  assert.equal(s.agentData.s1.end, 15);
  assert.equal(s.timeline.s1.end, 15);
  assert.ok(s.reviewed.has("s1"));
  assert.equal(s.costUsd, 0.01);

  reduceRunEvent(s, { type: "message", data: { sender: "coder", content: "hi" } });
  assert.equal(s.messages, 1);

  reduceRunEvent(s, { type: "subtask_start", ts: 16, data: { id: "s2", agent: "test_engineer" } });
  reduceRunEvent(s, {
    type: "subtask_review", ts: 20,
    data: { id: "s2", passed: false, score: 3, reasons: ["flaky"], cost_usd: 0.04 },
  });
  assert.equal(s.agentData.s2.status, "failed");
  assert.equal(s.agentData.s2.passed, false);
  assert.deepEqual(s.agentData.s2.reasons, ["flaky"]);
  assert.equal(s.reviewed.size, 2);
  assert.equal(s.costUsd, 0.04);   // cost is cumulative-as-reported: latest wins

  reduceRunEvent(s, { type: "execution", data: { ran: true, passed: true } });
  assert.equal(s.phase, "verify");

  reduceRunEvent(s, { type: "done", data: { passed: 1, total: 2, cost_usd: 0.05 } });
  assert.equal(s.phase, "done");
  assert.equal(s.costUsd, 0.05);
});

test("reduceRunEvent keeps attempts when review omits them and records objective note + cost", () => {
  const s = initialRunAggregates();
  reduceRunEvent(s, planEvent());
  reduceRunEvent(s, { type: "subtask_review", ts: 5, data: { id: "s1", passed: true, attempts: 2 } });
  reduceRunEvent(s, { type: "subtask_review", ts: 6, data: { id: "s1", passed: true, objective_note: "meets spec" } });
  assert.equal(s.agentData.s1.attempts, 2);          // not clobbered by the second review
  assert.equal(s.agentData.s1.objective_note, "meets spec");
  assert.equal(s.agentData.s1.cost, null);
});

test("reduceRunEvent tolerates out-of-order and unknown events", () => {
  const s = initialRunAggregates();
  // review before plan/start: nothing to update, but the id is still counted as reviewed
  reduceRunEvent(s, { type: "subtask_review", ts: 3, data: { id: "ghost", passed: true } });
  assert.equal(s.reviewed.size, 1);
  assert.equal(s.timeline.ghost, undefined);         // no timeline entry invented
  // events with no data / unknown types are no-ops
  reduceRunEvent(s, { type: "status", message: "warming up" });
  reduceRunEvent(s, { type: "wibble" });
  reduceRunEvent(s, { type: "subtask_start", ts: 1, data: {} });
  assert.deepEqual(Object.keys(s.timeline), []);
  // "Documenting…" status flips the phase
  reduceRunEvent(s, { type: "status", message: "Documenting results" });
  assert.equal(s.phase, "document");
});

test("reduceRunEvent 'diff' attaches the file lists to the agent record", () => {
  const s = initialRunAggregates();
  reduceRunEvent(s, planEvent());
  reduceRunEvent(s, { type: "diff", data: { id: "s1", added: ["a.py"], modified: ["b.py"] } });
  assert.deepEqual(s.agentData.s1.diff, { added: ["a.py"], modified: ["b.py"] });
});

// ---- runProgress ----
test("runProgress reports pct and label", () => {
  const s = initialRunAggregates();
  assert.deepEqual(runProgress(s), { done: 0, total: 0, pct: 0, label: "" });
  reduceRunEvent(s, planEvent());
  reduceRunEvent(s, { type: "subtask_review", ts: 1, data: { id: "s1", passed: true } });
  assert.deepEqual(runProgress(s), { done: 1, total: 2, pct: 50, label: "1/2 subtasks" });
  assert.deepEqual(runProgress(null), { done: 0, total: 0, pct: 0, label: "" });
});

// ---- formatStepLine ----
test("formatStepLine renders tool, thinking and text steps", () => {
  assert.equal(formatStepLine({ kind: "tool", tool: "Bash", input: "ls" }), "→ Bash ls");
  assert.equal(formatStepLine({ kind: "tool", tool: "Bash" }), "→ Bash");
  assert.equal(formatStepLine({ kind: "thinking", text: "hmm" }), "… hmm");
  assert.equal(formatStepLine({ kind: "text", text: "plain" }), "plain");
  assert.equal(formatStepLine({}), "");
  assert.equal(formatStepLine(null), "");
});

// ---- computeBudgetMeter ----
test("computeBudgetMeter hides when there is no (or an invalid) budget", () => {
  assert.deepEqual(computeBudgetMeter(1.5, 0), { visible: false });
  assert.deepEqual(computeBudgetMeter(1.5, null), { visible: false });
  assert.deepEqual(computeBudgetMeter(1.5, undefined), { visible: false });
  assert.deepEqual(computeBudgetMeter(1.5, -2), { visible: false });
  assert.deepEqual(computeBudgetMeter(1.5, NaN), { visible: false });
});

test("computeBudgetMeter severity thresholds at 79/80/100 percent", () => {
  const ok = computeBudgetMeter(0.79, 1);
  assert.deepEqual([ok.visible, ok.pct, ok.severity], [true, 79, ""]);
  const warn = computeBudgetMeter(0.80, 1);
  assert.deepEqual([warn.pct, warn.severity], [80, "warn"]);
  const over = computeBudgetMeter(1.0, 1);
  assert.deepEqual([over.pct, over.severity], [100, "over"]);
});

test("computeBudgetMeter clamps overspend at 100% and formats the readout", () => {
  const m = computeBudgetMeter(5, 2);
  assert.equal(m.pct, 100);
  assert.equal(m.severity, "over");
  assert.equal(m.text, "$5.0000 / $2.00");
  assert.equal(computeBudgetMeter(null, 2).text, "$0.0000 / $2.00");
});

// ---- timelineRows ----
test("timelineRows sorts by start and computes bar geometry", () => {
  const rows = timelineRows({
    b: { agent: "docs", start: 5, end: 10 },
    a: { agent: "coder", start: 0, end: 10 },
  });
  assert.deepEqual(rows.map(r => r.id), ["a", "b"]);
  assert.equal(rows[0].leftPct, 0);
  assert.equal(rows[0].widthPct, 100);
  assert.equal(rows[0].durLabel, "10.0s");
  assert.equal(rows[1].leftPct, 50);
  assert.equal(rows[1].widthPct, 50);
  assert.equal(rows[1].agent, "docs");
});

test("timelineRows handles a missing end (still running) with a 1% minimum bar", () => {
  const rows = timelineRows({
    a: { agent: "coder", start: 0, end: 100 },
    b: { agent: "docs", start: 50, end: null },
  });
  const b = rows.find(r => r.id === "b");
  assert.equal(b.dur, 0);
  assert.equal(b.widthPct, 1);      // clamped so the bar stays visible
  assert.equal(b.durLabel, "0.0s");
});

test("timelineRows returns [] for an empty timeline", () => {
  assert.deepEqual(timelineRows({}), []);
  assert.deepEqual(timelineRows(null), []);
});

// ---- compareRowModel ----
test("compareRowModel formats a fully populated run", () => {
  const a = {
    status: "completed", run_status: "partial", quality_score: 88,
    subtasks_passed: 3, subtasks_total: 4, tests: "passed", cost_usd: 0.1234,
    input_tokens: 1500, output_tokens: 500, duration_s: 65, sessions_spawned: 4, sessions_reaped: 1,
  };
  const rows = compareRowModel(a, {});
  const byLabel = Object.fromEntries(rows.map(r => [r.label, r.a]));
  assert.equal(byLabel.Status, "completed · partial");
  assert.equal(byLabel.Quality, "88/100");
  assert.equal(byLabel["Subtasks passed"], "3/4");
  assert.equal(byLabel.Tests, "passed");
  assert.equal(byLabel.Cost, "$0.1234");
  assert.equal(byLabel.Tokens, "1.5k in + 500 out");
  assert.equal(byLabel.Duration, "1m 5s");
  assert.equal(byLabel.Sessions, "4 spawned · 1 reaped");
});

test("compareRowModel renders em dashes for missing fields and null duration", () => {
  const rows = compareRowModel({}, null);
  assert.equal(rows.length, 8);
  for (const r of rows) {
    assert.equal(r.a, "—", r.label + " (a)");
    assert.equal(r.b, "—", r.label + " (b)");
  }
});

test("compareRowModel omits run_status when it matches status", () => {
  const rows = compareRowModel({ status: "completed", run_status: "completed" }, {});
  assert.equal(rows[0].a, "completed");
});

// ---- resume eligibility ----
test("isResumable accepts exactly the resumable statuses", () => {
  for (const st of ["interrupted", "failed", "over_budget", "cancelled", "partial"]) {
    assert.equal(isResumable(st), true, st);
  }
  assert.equal(isResumable("completed"), false);
  assert.equal(isResumable("running"), false);
  assert.equal(isResumable("queued"), false);
  assert.equal(isResumable(""), false);
  assert.equal(isResumable(undefined), false);
  assert.equal(RESUMABLE_STATUSES.size, 5);
});
