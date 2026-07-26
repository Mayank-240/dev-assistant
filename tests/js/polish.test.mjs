// UI smoothing pass — pure models in web/static/util.js:
// workspace "Recent tasks" aggregation, agent tool-chip collapse,
// backend chip label, and the graph's golden-angle seed layout.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  wsRecentTasksModel, agentToolsModel, backendLabel, seedLayout,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- wsRecentTasksModel ----

const _perProject = [
  { slug: "api", name: "Notes API", runs: [
    { id: "t1", title: "Fix parser", status: "completed", created_at: 100, quality_score: 90, cost_usd: 0.5 },
    { id: "t3", title: "Old run", status: "failed", created_at: 10 },
  ] },
  { slug: "web", name: "Web App", runs: [
    { id: "t2", title: "Ship banner", status: "running", created_at: 200 },
  ] },
];

test("wsRecentTasksModel merges member runs newest-first with project fields", () => {
  const m = wsRecentTasksModel(_perProject);
  assert.equal(m.empty, false);
  assert.deepEqual(m.rows.map(r => r.taskId), ["t2", "t1", "t3"]);
  assert.equal(m.rows[0].project, "web");
  assert.equal(m.rows[0].projectName, "Web App");
  assert.equal(m.rows[0].status, "running");
  assert.equal(m.rows[0].live, true);
  assert.equal(m.rows[1].live, false);
  assert.equal(m.rows[1].qualityLabel, "quality 90/100");
  assert.equal(m.rows[1].costLabel, "$0.5000");
});

test("wsRecentTasksModel caps at 8 by default and honors an explicit cap", () => {
  const runs = Array.from({ length: 12 }, (_, i) =>
    ({ id: "r" + i, title: "run " + i, status: "completed", created_at: i }));
  const m = wsRecentTasksModel([{ slug: "api", name: "API", runs }]);
  assert.equal(m.rows.length, 8);
  assert.equal(m.rows[0].taskId, "r11");   // newest first
  assert.equal(wsRecentTasksModel([{ slug: "api", runs }], 3).rows.length, 3);
});

test("wsRecentTasksModel dedupes by task id across member projects", () => {
  const m = wsRecentTasksModel([
    { slug: "api", runs: [{ id: "shared", title: "Fan-out", created_at: 5 }] },
    { slug: "web", runs: [{ id: "shared", title: "Fan-out", created_at: 5 }] },
  ]);
  assert.equal(m.rows.length, 1);
  assert.equal(m.rows[0].project, "api");   // first appearance wins
});

test("wsRecentTasksModel tolerates junk and reports empty", () => {
  assert.deepEqual(wsRecentTasksModel(null), { rows: [], empty: true });
  assert.equal(wsRecentTasksModel([{ slug: "api" }, null, { runs: "nope" }]).empty, true);
  const m = wsRecentTasksModel([{ slug: "api", runs: [{ id: "x" }, {}, null] }]);
  assert.equal(m.rows.length, 1);           // rows without an id are dropped
  assert.equal(m.rows[0].title, "x");       // title falls back to the id
  assert.equal(m.rows[0].createdAt, null);
});

// ---- agentToolsModel ----

test("agentToolsModel counts tools with a pluralized chip label", () => {
  const m = agentToolsModel(["read_file", "write_file", "bash"]);
  assert.equal(m.count, 3);
  assert.equal(m.label, "3 tools");
  assert.deepEqual(m.tools, ["read_file", "write_file", "bash"]);
  assert.equal(agentToolsModel(["bash"]).label, "1 tool");
});

test("agentToolsModel treats a missing tool list as zero tools", () => {
  assert.deepEqual(agentToolsModel(null), { tools: [], count: 0, label: "0 tools" });
  assert.equal(agentToolsModel("bash").count, 0);
});

// ---- backendLabel ----

test("backendLabel builds the chip text from /api/config", () => {
  assert.equal(backendLabel({ llm_backend: "claude_sdk" }), "backend: claude_sdk");
  assert.equal(backendLabel({ backend: "mock" }), "backend: mock");  // SSE-status shape
});

test("backendLabel is empty when the backend is unknown (keep the placeholder)", () => {
  assert.equal(backendLabel({}), "");
  assert.equal(backendLabel(null), "");
  assert.equal(backendLabel({ llm_backend: "   " }), "");
});

// ---- seedLayout ----

test("seedLayout returns n deterministic positions", () => {
  const a = seedLayout(30, 800, 520);
  const b = seedLayout(30, 800, 520);
  assert.equal(a.length, 30);
  assert.deepEqual(a, b);
  assert.deepEqual(seedLayout(0, 800, 520), []);
});

test("seedLayout keeps every position inside the margin box", () => {
  const pts = seedLayout(150, 800, 520);
  pts.forEach(p => {
    assert.ok(p.x >= 24 && p.x <= 776, "x in bounds: " + p.x);
    assert.ok(p.y >= 24 && p.y <= 496, "y in bounds: " + p.y);
  });
});

test("seedLayout spreads nodes instead of clustering them on a ring", () => {
  const pts = seedLayout(40, 800, 520);
  // no two seeds coincide, and the closest pair keeps real separation
  let minD = Infinity;
  for (let i = 0; i < pts.length; i++) {
    for (let j = i + 1; j < pts.length; j++) {
      minD = Math.min(minD, Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y));
    }
  }
  assert.ok(minD > 20, "min pairwise distance " + minD);
  // the seeds span most of the canvas, not one corner or one circle
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  assert.ok(Math.max(...xs) - Math.min(...xs) > 400);
  assert.ok(Math.max(...ys) - Math.min(...ys) > 260);
});

test("seedLayout centers a single node", () => {
  assert.deepEqual(seedLayout(1, 800, 520), [{ x: 400, y: 260 }]);
});
