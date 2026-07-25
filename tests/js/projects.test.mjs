// Unit tests for the project-status / activity-strip pure models in web/static/util.js.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { projectStatusLine, activityStripModel } =
  require("../../src/ai_dev_assistant/web/static/util.js");

// ---- projectStatusLine ----

test("projectStatusLine hides on null / empty status", () => {
  assert.equal(projectStatusLine(null).visible, false);
  assert.equal(projectStatusLine({}).visible, false);
  assert.equal(projectStatusLine({ dirty: true }).visible, false);
});

test("projectStatusLine renders branch @ short-head", () => {
  const m = projectStatusLine({ branch: "main", head: "abcdef0123456789", dirty: false });
  assert.equal(m.visible, true);
  assert.equal(m.text, "main @ abcdef0");
  assert.equal(m.dirty, false);
  assert.equal(m.archived, false);
});

test("projectStatusLine flags dirty and archived", () => {
  const m = projectStatusLine({ branch: "dev", head: "1234567", dirty: true, archived: true });
  assert.equal(m.dirty, true);
  assert.equal(m.archived, true);
});

test("projectStatusLine tolerates a missing branch (detached head)", () => {
  const m = projectStatusLine({ branch: "", head: "cafebabe99" });
  assert.equal(m.visible, true);
  assert.equal(m.text, "(no branch) @ cafebab");
});

test("projectStatusLine tolerates a branch with no head (fresh repo)", () => {
  const m = projectStatusLine({ branch: "main", head: "" });
  assert.equal(m.visible, true);
  assert.equal(m.text, "main");
});

// ---- activityStripModel ----

test("activityStripModel is idle for null / empty activity", () => {
  assert.deepEqual(activityStripModel(null), { state: "idle", text: "idle", running: 0, queued: 0 });
  assert.deepEqual(activityStripModel({ running: [], queued: [] }),
    { state: "idle", text: "idle", running: 0, queued: 0 });
});

test("activityStripModel shows the running task title", () => {
  const m = activityStripModel({ running: [{ id: "t1", title: "Fix the parser" }], queued: [] });
  assert.equal(m.state, "running");
  assert.equal(m.text, "running · Fix the parser");
  assert.equal(m.running, 1);
});

test("activityStripModel falls back to the task id when title is blank", () => {
  const m = activityStripModel({ running: [{ id: "t9" }], queued: [] });
  assert.equal(m.text, "running · t9");
});

test("activityStripModel counts extra running tasks and the queue", () => {
  const m = activityStripModel({
    running: [{ id: "a", title: "First" }, { id: "b", title: "Second" }],
    queued: [{ id: "c" }, { id: "d" }, { id: "e" }],
  });
  assert.equal(m.state, "running");
  assert.equal(m.text, "running · First (+1 more) · 3 queued");
  assert.equal(m.running, 2);
  assert.equal(m.queued, 3);
});

test("activityStripModel reports queued-only projects", () => {
  const m = activityStripModel({ running: [], queued: [{ id: "x" }, { id: "y" }] });
  assert.equal(m.state, "queued");
  assert.equal(m.text, "2 queued");
});
