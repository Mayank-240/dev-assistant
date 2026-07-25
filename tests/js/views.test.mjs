// Unit tests for the project-first IA view models in web/static/util.js:
// sidebar project rows, project tab state, merged task-list rows, composer model.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  sidebarProjectRows, projectTabsModel, projectTaskRows, composerModel,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- sidebarProjectRows ----

const _projects = [
  { slug: "api", name: "Notes API" },
  { slug: "web", name: "Web App", archived: true },
];

test("sidebarProjectRows maps projects with idle state by default", () => {
  const rows = sidebarProjectRows(_projects, {}, "api", null);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0],
    { slug: "api", name: "Notes API", state: "idle", archived: false, current: true, multi: false });
  assert.equal(rows[1].archived, true);
  assert.equal(rows[1].current, false);
});

test("sidebarProjectRows carries running/queued state from activity", () => {
  const acts = {
    api: { running: [{ id: "t1", title: "Fix" }], queued: [] },
    web: { running: [], queued: [{ id: "t2" }] },
  };
  const rows = sidebarProjectRows(_projects, acts, "web", null);
  assert.equal(rows[0].state, "running");
  assert.equal(rows[1].state, "queued");
  assert.equal(rows[1].current, true);
});

test("sidebarProjectRows omits the multi entry when there is no fan-out work", () => {
  const rows = sidebarProjectRows(_projects, {}, "api", { activity: null, hasRuns: false });
  assert.equal(rows.some(r => r.multi), false);
});

test("sidebarProjectRows appends '⋔ Across projects' when fan-out is live", () => {
  const rows = sidebarProjectRows(_projects, {}, "api",
    { activity: { running: [{ id: "fan-1" }], queued: [] }, hasRuns: false });
  const m = rows[rows.length - 1];
  assert.equal(m.multi, true);
  assert.equal(m.slug, "multi");
  assert.equal(m.name, "⋔ Across projects");
  assert.equal(m.state, "running");
});

test("sidebarProjectRows shows the multi entry for historical fan-out runs", () => {
  const rows = sidebarProjectRows(_projects, {}, "multi", { activity: null, hasRuns: true });
  const m = rows[rows.length - 1];
  assert.equal(m.multi, true);
  assert.equal(m.state, "idle");
  assert.equal(m.current, true);
});

test("sidebarProjectRows tolerates bad input", () => {
  assert.deepEqual(sidebarProjectRows(null, null, "x", null), []);
});

// ---- projectTabsModel ----

test("projectTabsModel lists all four tabs for a real project", () => {
  const tabs = projectTabsModel("api", "knowledge");
  assert.deepEqual(tabs.map(t => t.id), ["overview", "tasks", "knowledge", "settings"]);
  assert.deepEqual(tabs.filter(t => t.selected).map(t => t.id), ["knowledge"]);
  assert.equal(tabs[0].label, "Overview");
});

test("projectTabsModel falls back to the first tab for an unknown active", () => {
  const tabs = projectTabsModel("api", "nope");
  assert.equal(tabs.find(t => t.selected).id, "overview");
});

test("projectTabsModel restricts the multi pseudo-entry to its task list", () => {
  const tabs = projectTabsModel("multi", "overview");
  assert.deepEqual(tabs.map(t => t.id), ["tasks"]);
  assert.equal(tabs[0].selected, true);
  assert.equal(tabs[0].label, "Tasks");
});

// ---- projectTaskRows ----

test("projectTaskRows orders queued, then running, then history", () => {
  const rows = projectTaskRows(
    [{ id: "old", title: "Old", status: "completed", quality_score: 90, cost_usd: 0.5, tests: "passed" }],
    { queued: [{ id: "q1", title: "Waiting", position: 1 }],
      running: [{ id: "r1", title: "Live" }] });
  assert.deepEqual(rows.map(r => r.id), ["q1", "r1", "old"]);
  assert.equal(rows[0].status, "queued");
  assert.equal(rows[0].position, 1);
  assert.equal(rows[1].status, "running");
  assert.equal(rows[1].live, true);
  assert.equal(rows[2].status, "completed");
  assert.equal(rows[2].quality, 90);
  assert.equal(rows[2].cost, 0.5);
  assert.equal(rows[2].tests, "passed");
  assert.equal(rows[2].live, false);
});

test("projectTaskRows dedupes live rows and folds stored metrics in", () => {
  const rows = projectTaskRows(
    [{ id: "r1", title: "Live task", status: "running", quality_score: null, cost_usd: 0.2 }],
    { running: [{ id: "r1" }], queued: [] });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "running");    // live status wins
  assert.equal(rows[0].cost, 0.2);            // stored metric folded in
  assert.equal(rows[0].title, "Live task");   // stored title beats the bare id
});

test("projectTaskRows marks run-store running/queued rows as live", () => {
  const rows = projectTaskRows([{ id: "a", title: "A", status: "running" }], null);
  assert.equal(rows[0].live, true);
});

test("projectTaskRows tolerates missing inputs", () => {
  assert.deepEqual(projectTaskRows(null, null), []);
  assert.deepEqual(projectTaskRows([], {}), []);
});

// ---- composerModel ----

test("composerModel asks what to work on in the project by name", () => {
  const m = composerModel({ slug: "api", name: "Notes API", root: "" });
  assert.equal(m.heading, "What should we work on in Notes API?");
  assert.equal(m.delivery.control, "checkbox");   // scratch: per-run checkbox survives
});

test("composerModel routes repo projects to the policy delivery hint", () => {
  const m = composerModel({ slug: "api", name: "Notes API", root: "/repos/api",
                            policy: { git_mode: "merge" } });
  assert.equal(m.delivery.control, "hint");
  assert.equal(m.delivery.gitMode, "merge");
});

test("composerModel falls back to the slug then a generic name", () => {
  assert.equal(composerModel({ slug: "api" }).heading, "What should we work on in api?");
  assert.equal(composerModel(null).heading, "What should we work on in this project?");
});
