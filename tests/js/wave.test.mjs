// Unit tests for the feature-wave pure models in web/static/util.js:
// command palette, schedules, spend analytics, per-run cost, A/B table,
// notification center, playbook forms and the first-run tour.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  fmtRelTime, paletteResultsModel, scheduleRowModel, scheduleFormModel,
  spendOverviewModel, spendOutcomesModel, runCostModel, abTableModel,
  notifCenterModel, playbookFormModel, tourStepsModel,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- fmtRelTime ----
test("fmtRelTime renders future, past and edge values", () => {
  const now = 1_000_000;
  assert.equal(fmtRelTime(now + 30, now), "due now");
  assert.equal(fmtRelTime(now + 120, now), "in 2m");
  assert.equal(fmtRelTime(now + 2 * 3600 + 600, now), "in 2h 10m");
  assert.equal(fmtRelTime(now + 3 * 86400, now), "in 3d");
  assert.equal(fmtRelTime(now - 20, now), "just now");
  assert.equal(fmtRelTime(now - 300, now), "5m ago");
  assert.equal(fmtRelTime(null, now), "—");
  assert.equal(fmtRelTime("nope", now), "—");
});

// ---- paletteResultsModel ----
const PROJECTS = [
  { slug: "alpha", name: "Alpha API" },
  { slug: "default", name: "default" },   // scratch default is never surfaced
  { slug: "beta", name: "Beta" },
];

test("palette: static commands match by name and built-ins by label", () => {
  const m = paletteResultsModel("alp", [], PROJECTS);
  assert.equal(m.groups[0].kind, "command");
  assert.ok(m.flat.some(it => it.action === "project" && it.slug === "alpha"));
  assert.ok(!m.flat.some(it => it.slug === "default"));
  const s = paletteResultsModel("sett", [], PROJECTS);
  assert.ok(s.flat.some(it => it.action === "settings"));
  const a = paletteResultsModel("activ", [], PROJECTS);
  assert.ok(a.flat.some(it => it.action === "activity"));
});

test("palette: hits group by kind in stable order with refs preserved", () => {
  const hits = [
    { kind: "file", project: "alpha", title: "a.py", snippet: "", ref: { task: "t1", path: "a.py" } },
    { kind: "task", project: "beta", title: "Fix bug", snippet: "…", ref: { task_id: "t9" } },
    { kind: "task", project: "alpha", title: "Add tests", ref: { task_id: "t2" } },
    { kind: "memory", project: "alpha", title: "note", ref: { id: "m1" } },
  ];
  const m = paletteResultsModel("zzz-no-command-match", hits, PROJECTS);
  assert.deepEqual(m.groups.map(g => g.kind), ["task", "memory", "file"]);
  assert.equal(m.groups[0].items.length, 2);
  assert.equal(m.flat.length, 4);
  assert.equal(m.flat[0].ref.task_id, "t9");
  assert.equal(m.empty, false);
});

test("palette: empty query and no hits -> empty model", () => {
  const m = paletteResultsModel("", [], PROJECTS);
  assert.equal(m.empty, true);
  assert.deepEqual(m.flat, []);
});

test("palette: malformed hits are skipped, ref defaults to object", () => {
  const m = paletteResultsModel("xyzquery", [null, { kind: "kb", title: "doc" }], PROJECTS);
  assert.equal(m.flat.length, 1);
  assert.deepEqual(m.flat[0].ref, {});
  assert.equal(m.flat[0].type, "kb");
});

// ---- scheduleRowModel ----
test("scheduleRowModel formats interval, next run and disabled state", () => {
  const now = 5_000_000;
  const on = scheduleRowModel({
    id: "s1", title: "Nightly", prompt: "run tests", enabled: 1,
    every_hours: 24, next_run_at: now + 3600, last_task_id: "t7", budget_usd: 2,
  }, now);
  assert.equal(on.everyLabel, "every 24h");
  assert.equal(on.nextLabel, "next in 1h");
  assert.equal(on.lastTaskId, "t7");
  assert.equal(on.budgetLabel, "$2.00");
  const off = scheduleRowModel({ id: "s2", prompt: "p", enabled: 0, every_hours: 0.25 }, now);
  assert.equal(off.enabled, false);
  assert.equal(off.nextLabel, "paused");
  assert.equal(off.everyLabel, "every 15m");
  assert.equal(off.title, "p");   // falls back to the prompt
  const due = scheduleRowModel({ id: "s3", prompt: "p", enabled: 1, every_hours: 1, next_run_at: null }, now);
  assert.equal(due.nextLabel, "due now");
});

// ---- scheduleFormModel ----
test("scheduleFormModel validates prompt and the 0.25h minimum", () => {
  const bad = scheduleFormModel({ prompt: "  ", every_hours: "0.1" });
  assert.equal(bad.ok, false);
  assert.equal(bad.errors.length, 2);
  const ok = scheduleFormModel({ prompt: "do it", title: " T ", every_hours: "0.25", budget_usd: "1.5" });
  assert.equal(ok.ok, true);
  assert.deepEqual(ok.body, { prompt: "do it", title: "T", every_hours: 0.25, budget_usd: 1.5 });
  const noBudget = scheduleFormModel({ prompt: "x", every_hours: 2 });
  assert.equal(noBudget.ok, true);
  assert.equal(noBudget.body.budget_usd, 0);
  assert.equal(noBudget.body.title, null);
  const negBudget = scheduleFormModel({ prompt: "x", every_hours: 2, budget_usd: "-1" });
  assert.equal(negBudget.ok, false);
});

// ---- spendOverviewModel ----
test("spendOverviewModel builds cards, bars and project rows", () => {
  const m = spendOverviewModel({
    window_days: 30, total_usd: 12.3456, runs: 7, tokens_in: 1500, tokens_out: 200,
    by_day: [{ date: "2026-07-24", usd: 2, runs: 1 }, { date: "2026-07-25", usd: 4, runs: 2 },
             { date: "2026-07-26", usd: 0, runs: 0 }],
    by_project: [{ project: "alpha", usd: 10, runs: 5, avg_quality: 88.5 },
                 { project: "beta", usd: 2.3456, runs: 2, avg_quality: null }],
  });
  assert.equal(m.totalLabel, "$12.35");
  assert.equal(m.runs, 7);
  assert.equal(m.tokensLabel, "1.5k in + 200 out");
  assert.equal(m.bars.length, 3);
  assert.equal(m.bars[1].hPct, 100);           // max day
  assert.equal(m.bars[0].hPct, 50);
  assert.equal(m.bars[2].hPct, 0);             // zero-spend day stays zero-height
  assert.equal(m.projects[0].usdLabel, "$10.00");
  assert.equal(m.projects[1].qualityLabel, "—");
});

test("spendOverviewModel tolerates an empty payload", () => {
  const m = spendOverviewModel(null);
  assert.equal(m.totalLabel, "$0.00");
  assert.deepEqual(m.bars, []);
  assert.deepEqual(m.projects, []);
});

// ---- spendOutcomesModel ----
test("spendOutcomesModel renders ratios and em-dashes null denominators", () => {
  const m = spendOutcomesModel({
    total_usd: 9, completed_runs: 3, passed_subtasks: 12, accepted_subtasks: 0,
    usd_per_completed_run: 3, usd_per_passed_subtask: 0.75, usd_per_accepted_subtask: null,
  });
  assert.equal(m.rows[0].value, "$3.0000");
  assert.equal(m.rows[1].count, 12);
  assert.equal(m.rows[2].value, "—");
  assert.equal(m.totalLabel, "$9.00");
});

// ---- runCostModel ----
test("runCostModel maps subtasks, totals and the timeline lookup", () => {
  const m = runCostModel({
    task_id: "t1",
    subtasks: [{ subtask: "s1", agent: "coder", usd: 0.12, tokens_in: 1000, tokens_out: 50 },
               { subtask: "s2", agent: null, usd: 0.03, tokens_in: 10, tokens_out: 5 }],
    totals: { usd: 0.15, tokens_in: 1010, tokens_out: 55 },
    run_total_usd: 0.2, unattributed_usd: 0.05,
  });
  assert.equal(m.empty, false);
  assert.equal(m.rows.length, 2);
  assert.equal(m.rows[0].usdLabel, "$0.1200");
  assert.equal(m.rows[1].agent, "—");
  assert.equal(m.totalLabel, "$0.1500");
  assert.equal(m.runTotalLabel, "$0.2000");
  assert.equal(m.unattributedLabel, "$0.0500");
  assert.deepEqual(m.bySubtask, { s1: 0.12, s2: 0.03 });
});

test("runCostModel: empty breakdown -> empty flag, no phantom labels", () => {
  const m = runCostModel({ task_id: "t", subtasks: [], totals: { usd: 0 } });
  assert.equal(m.empty, true);
  assert.equal(m.runTotalLabel, "");
  assert.equal(m.unattributedLabel, "");
});

// ---- abTableModel ----
test("abTableModel flags the best arm and formats metrics", () => {
  const m = abTableModel({
    knob: "ADA_AGENT_EFFORT", best: "high",
    arms: [
      { value: "low", pass_rate: 0.5, mean_quality: 70, mean_cost_usd: 0.01,
        mean_wall_s: 3.2, report: { attempts: 2 } },
      { value: "high", pass_rate: 1, mean_quality: null, mean_cost_usd: 0.02,
        mean_wall_s: 4, report: { attempts: 2 } },
    ],
  });
  assert.equal(m.knob, "ADA_AGENT_EFFORT");
  assert.equal(m.verdict, "Best arm: ADA_AGENT_EFFORT = high");
  assert.equal(m.rows[0].best, false);
  assert.equal(m.rows[1].best, true);
  assert.equal(m.rows[0].passLabel, "50%");
  assert.equal(m.rows[1].qualityLabel, "n/a");
  assert.equal(m.rows[0].costLabel, "$0.0100");
  assert.equal(m.rows[1].wallLabel, "4.0s");
  assert.equal(m.rows[0].attempts, 2);
});

test("abTableModel: no arms -> no verdict, no rows", () => {
  const m = abTableModel({ knob: "ADA_X", best: null, arms: [] });
  assert.equal(m.verdict, "");
  assert.deepEqual(m.rows, []);
});

// ---- notifCenterModel ----
test("notifCenterModel sorts newest first and counts unread", () => {
  const now = 1000;
  const m = notifCenterModel([
    { id: "a", ts: 100, kind: "done", text: "first", taskId: "t1", read: true },
    { id: "b", ts: 900, kind: "ask", text: "question", taskId: "t2", read: false },
    { id: "c", ts: 500, kind: "error", text: "boom", read: false },
  ], now);
  assert.deepEqual(m.rows.map(r => r.id), ["b", "c", "a"]);
  assert.equal(m.unread, 2);
  assert.equal(m.badge, "2");
  assert.equal(m.rows[0].icon, "?");
  assert.equal(m.rows[1].cls, "nf-error");
  assert.equal(m.rows[2].read, true);
  assert.equal(m.empty, false);
});

test("notifCenterModel badge caps at 9+ and empties cleanly", () => {
  const items = Array.from({ length: 12 }, (_, i) => ({ id: String(i), ts: i, kind: "done", text: "x" }));
  assert.equal(notifCenterModel(items, 100).badge, "9+");
  const empty = notifCenterModel([], 100);
  assert.equal(empty.empty, true);
  assert.equal(empty.badge, "");
});

// ---- playbookFormModel ----
const PB = {
  id: "raise-coverage", name: "Raise test coverage",
  params: [
    { key: "path", label: "Module or path to cover", type: "str", required: true },
    { key: "target_percent", label: "Coverage target (percent)", type: "int",
      default: 85, required: false },
    { key: "mode", label: "Mode", type: "choice", choices: ["fast", "deep"], default: "fast" },
  ],
};

test("playbookFormModel prefills defaults and maps controls", () => {
  const m = playbookFormModel(PB, {});
  assert.equal(m.fields.length, 3);
  assert.equal(m.fields[0].control, "text");
  assert.equal(m.fields[1].control, "number");
  assert.equal(m.fields[1].value, "85");
  assert.equal(m.fields[2].control, "select");
  assert.deepEqual(m.fields[2].choices, ["fast", "deep"]);
  assert.equal(m.ok, false);                   // required `path` still blank
  assert.match(m.errors[0], /Module or path/);
});

test("playbookFormModel validates int and choice params like the server", () => {
  const bad = playbookFormModel(PB, { path: "src/x.py", target_percent: "abc", mode: "nope" });
  assert.equal(bad.ok, false);
  assert.equal(bad.errors.length, 2);
  assert.match(bad.errors.join(" "), /whole number/);
  assert.match(bad.errors.join(" "), /one of/);
  const ok = playbookFormModel(PB, { path: " src/x.py ", target_percent: "90", mode: "deep" });
  assert.equal(ok.ok, true);
  assert.deepEqual(ok.params, { path: "src/x.py", target_percent: 90, mode: "deep" });
});

test("playbookFormModel omits blank defaulted params (server applies default)", () => {
  const m = playbookFormModel(PB, { path: "a", target_percent: "", mode: "" });
  assert.equal(m.ok, true);
  assert.deepEqual(m.params, { path: "a" });
});

test("playbookFormModel: no params -> ok with empty payload", () => {
  const m = playbookFormModel({ id: "x", params: [] }, {});
  assert.equal(m.ok, true);
  assert.deepEqual(m.fields, []);
  assert.deepEqual(m.params, {});
});

// ---- tourStepsModel ----
test("tourStepsModel shows only once and only with zero projects", () => {
  assert.equal(tourStepsModel(0, null).show, true);
  assert.equal(tourStepsModel(0, "1").show, false);
  assert.equal(tourStepsModel(2, null).show, false);
  const m = tourStepsModel(0, null);
  assert.equal(m.steps.length, 4);
  assert.deepEqual(m.steps.map(s => s.id), ["welcome", "projects", "composer", "settings"]);
  assert.equal(m.steps[0].target, "");        // welcome is centered, no spotlight
  m.steps.slice(1).forEach(s => assert.ok(s.target));
  m.steps.forEach(s => { assert.ok(s.title); assert.ok(s.body); });
});
