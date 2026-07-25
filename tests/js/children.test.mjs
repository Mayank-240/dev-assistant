// F3: cross-project fan-out — children reducers + grid models (web/static/util.js).
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  initialRunAggregates, reduceRunEvent, runProgress,
  makeChildRecord, childrenFromRows, childrenGridModel,
} = require("../../src/ai_dev_assistant/web/static/util.js");

const ev = (type, data) => ({ type, message: "", data, ts: 1 });

// ---- reduceRunEvent: plan with children starts a fan-out parent ----

test("plan event with children populates state.children instead of agentData", () => {
  const s = initialRunAggregates();
  reduceRunEvent(s, ev("plan", { children: [
    { slug: "api", task_id: "t-1" }, { slug: "web", task_id: "t-2" },
  ] }));
  assert.equal(s.children.length, 2);
  assert.deepEqual(s.children.map(c => c.slug), ["api", "web"]);
  assert.deepEqual(s.children.map(c => c.status), ["queued", "queued"]);
  assert.equal(s.total, 2);
  assert.deepEqual(s.agentData, {});   // no agent cards for a fan-out parent
  assert.equal(s.phase, "execute");
});

test("plan event without children stays a normal run (children null)", () => {
  const s = initialRunAggregates();
  reduceRunEvent(s, ev("plan", { subtasks: [{ id: "s1", agent: "coder", title: "x" }] }));
  assert.equal(s.children, null);
  assert.equal(s.total, 1);
});

test("child_start marks the matching child running (matched by task_id)", () => {
  const s = initialRunAggregates();
  reduceRunEvent(s, ev("plan", { children: [{ slug: "api", task_id: "t-1" }] }));
  reduceRunEvent(s, ev("child_start", { slug: "api", task_id: "t-1" }));
  assert.equal(s.children[0].status, "running");
});

test("child_done records status, quality, cost and branch, and advances progress", () => {
  const s = initialRunAggregates();
  reduceRunEvent(s, ev("plan", { children: [
    { slug: "api", task_id: "t-1" }, { slug: "web", task_id: "t-2" },
  ] }));
  reduceRunEvent(s, ev("child_done", {
    slug: "api", task_id: "t-1", status: "completed",
    quality: 91, cost_usd: 0.42, branch: "ada/t-1",
  }));
  const c = s.children[0];
  assert.equal(c.status, "completed");
  assert.equal(c.quality, 91);
  assert.equal(c.cost_usd, 0.42);
  assert.equal(c.branch, "ada/t-1");
  const p = runProgress(s);
  assert.equal(p.done, 1);
  assert.equal(p.label, "1/2 projects");   // fan-out progress counts projects
});

test("child events for an unknown child append it (late-announced child)", () => {
  const s = initialRunAggregates();
  reduceRunEvent(s, ev("plan", { children: [{ slug: "api", task_id: "t-1" }] }));
  reduceRunEvent(s, ev("child_done", { slug: "cli", task_id: "t-9", status: "failed" }));
  assert.equal(s.children.length, 2);
  assert.equal(s.children[1].status, "failed");
});

test("child_start matches by slug when the event carries no task_id", () => {
  const s = initialRunAggregates();
  reduceRunEvent(s, ev("plan", { children: [{ slug: "api", task_id: "t-1" }] }));
  reduceRunEvent(s, ev("child_start", { slug: "api" }));
  assert.equal(s.children[0].status, "running");
  assert.equal(s.children[0].task_id, "t-1");  // keeps the known id
});

// ---- childrenFromRows: /api/tasks/{id}/children rows -> child records ----

test("childrenFromRows maps endpoint rows onto child records", () => {
  const kids = childrenFromRows([
    { id: "c-1", slug: "api", project: "api", status: "completed",
      quality_score: 88, cost_usd: 0.3, task_branch: "ada/c-1", review_target: "main" },
    { id: "c-2", project: "web", status: "failed" },
  ]);
  assert.deepEqual(kids[0], makeChildRecord({
    slug: "api", task_id: "c-1", status: "completed",
    quality: 88, cost_usd: 0.3, branch: "ada/c-1",
  }));
  assert.equal(kids[1].slug, "web");   // falls back to `project` when slug is absent
  assert.equal(kids[1].status, "failed");
  assert.equal(kids[1].quality, null);
});

// ---- childrenGridModel: child records -> render models ----

test("childrenGridModel maps statuses to card and pill classes", () => {
  const models = childrenGridModel([
    makeChildRecord({ slug: "a", task_id: "1", status: "queued" }),
    makeChildRecord({ slug: "b", task_id: "2", status: "running" }),
    makeChildRecord({ slug: "c", task_id: "3", status: "completed" }),
    makeChildRecord({ slug: "d", task_id: "4", status: "partial" }),
    makeChildRecord({ slug: "e", task_id: "5", status: "failed" }),
    makeChildRecord({ slug: "f", task_id: "6", status: "over_budget" }),
  ]);
  assert.deepEqual(models.map(m => m.cls),
    ["queued", "running", "passed", "partial", "failed", "failed"]);
  assert.deepEqual(models.map(m => m.pill),
    ["", "pill-running", "pill-done", "pill-warn", "pill-err", "pill-err"]);
});

test("childrenGridModel formats quality, cost and branch labels", () => {
  const [m] = childrenGridModel([makeChildRecord({
    slug: "api", task_id: "t-1", status: "completed",
    quality: 91, cost_usd: 0.4211, branch: "ada/t-1",
  })]);
  assert.equal(m.qualityLabel, "quality 91/100");
  assert.equal(m.costLabel, "$0.4211");
  assert.equal(m.branch, "ada/t-1");
  assert.equal(m.taskId, "t-1");
});

test("childrenGridModel dashes out missing quality/cost", () => {
  const [m] = childrenGridModel([makeChildRecord({ slug: "api", task_id: "t-1" })]);
  assert.equal(m.qualityLabel, "quality —");
  assert.equal(m.costLabel, "$—");
  assert.equal(m.branch, "");
});

test("live children open the stream; finished children open docs", () => {
  const models = childrenGridModel([
    makeChildRecord({ slug: "a", task_id: "1", status: "running" }),
    makeChildRecord({ slug: "b", task_id: "2", status: "queued" }),
    makeChildRecord({ slug: "c", task_id: "3", status: "completed" }),
    makeChildRecord({ slug: "d", task_id: "4", status: "failed" }),
  ]);
  assert.deepEqual(models.map(m => m.open), ["live", "live", "docs", "docs"]);
});

test("childrenGridModel handles null/empty input", () => {
  assert.deepEqual(childrenGridModel(null), []);
  assert.deepEqual(childrenGridModel([]), []);
});
