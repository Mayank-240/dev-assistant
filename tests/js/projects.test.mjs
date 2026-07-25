// Unit tests for the project-status / activity-strip pure models in web/static/util.js.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  projectStatusLine, activityStripModel,
  projectTaglineModel, deliveryControlModel, filterTasksByProject, projectHomeHeaderModel,
} = require("../../src/ai_dev_assistant/web/static/util.js");

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

// ---- projectTaglineModel (topbar identity line) ----

test("projectTaglineModel joins name and branch @ short-head", () => {
  const m = projectTaglineModel("Notes API", { branch: "main", head: "abcdef0123456789", dirty: true });
  assert.equal(m.text, "Notes API · ⎇ main @ abcdef0");
  assert.equal(m.branchText, "main @ abcdef0");
  assert.equal(m.dirty, true);
  assert.equal(m.archived, false);
});

test("projectTaglineModel falls back to the bare name without repo status", () => {
  const m = projectTaglineModel("Default", null);
  assert.equal(m.text, "Default");
  assert.equal(m.branchText, "");
  assert.equal(m.dirty, false);
});

test("projectTaglineModel carries the archived flag", () => {
  const m = projectTaglineModel("Old", { branch: "main", head: "1234567", archived: true });
  assert.equal(m.archived, true);
});

// ---- deliveryControlModel (composer git control) ----

test("deliveryControlModel keeps the checkbox for the scratch default (no repo)", () => {
  assert.equal(deliveryControlModel({ slug: "default", root: "" }).control, "checkbox");
  assert.equal(deliveryControlModel(null).control, "checkbox");
});

test("deliveryControlModel renders the policy hint when the project has a repo", () => {
  const m = deliveryControlModel({ slug: "api", root: "/repos/api", policy: { git_mode: "merge" } });
  assert.equal(m.control, "hint");
  assert.equal(m.gitMode, "merge");
  assert.equal(m.text, "Delivery: ada/<task-id> branch for review — policy: merge");
});

test("deliveryControlModel defaults git_mode to branch", () => {
  const m = deliveryControlModel({ root: "/repos/api", policy: {} });
  assert.equal(m.gitMode, "branch");
  assert.match(m.text, /policy: branch$/);
});

// ---- filterTasksByProject (recent-tasks scoping) ----

const _tasks = [
  { id: "a", project: "api" },
  { id: "b", project: "default" },
  { id: "c" },                    // legacy row without a project — never hidden
  { id: "d", project: null },     // pre-project-column run store row — never hidden
];

test("filterTasksByProject scopes to the selected project by default", () => {
  const out = filterTasksByProject(_tasks, "api", false);
  assert.deepEqual(out.map(t => t.id), ["a", "c", "d"]);
});

test("filterTasksByProject returns everything with the all toggle", () => {
  assert.equal(filterTasksByProject(_tasks, "api", true).length, 4);
});

test("filterTasksByProject tolerates a missing slug and bad input", () => {
  assert.equal(filterTasksByProject(_tasks, "", false).length, 4);
  assert.deepEqual(filterTasksByProject(null, "api", false), []);
});

// ---- projectHomeHeaderModel (Project Home header + scratch empty state) ----

test("projectHomeHeaderModel describes a repo-backed project", () => {
  const m = projectHomeHeaderModel({ slug: "api", name: "Notes API", origin: "git", root: "/repos/api" });
  assert.equal(m.name, "Notes API");
  assert.equal(m.origin, "git");
  assert.equal(m.root, "/repos/api");
  assert.equal(m.scratch, false);
  assert.equal(m.emptyText, "");
});

test("projectHomeHeaderModel flags the scratch default with a purposeful empty state", () => {
  const m = projectHomeHeaderModel({ slug: "default", name: "Default", root: "" });
  assert.equal(m.scratch, true);
  assert.equal(m.origin, "greenfield");
  assert.match(m.emptyText, /^Scratch project — tasks run greenfield\./);
});

test("projectHomeHeaderModel falls back to the slug as name", () => {
  assert.equal(projectHomeHeaderModel({ slug: "x" }).name, "x");
});
