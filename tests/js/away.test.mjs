// Unit tests for the away-wave pure models in web/static/util.js:
// the Home screen (homeModel), workspace grouping + deps + run payload,
// knowledge graph v2 view/panel models and the custom-agent editor.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  homeModel, sidebarGroups, wsDepsModel, wsRunPayload,
  graph2ViewModel, nodePanelModel,
  agentsListModel, agentFormModel, agentFormValidate,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- homeModel ----
test("homeModel: attention items map kinds/text and set prominence", () => {
  const m = homeModel({
    attention: [
      { task_id: "t1", project: "alpha", kind: "ask", agent: "coder",
        question: "Which DB?", options: ["sqlite", "postgres"] },
      { task_id: "t2", project: "beta", kind: "permission", agent: "coder",
        request: "write infra/main.tf" },
    ],
  });
  assert.equal(m.prominent, true);
  assert.equal(m.attentionCount, 2);
  assert.equal(m.attention[0].text, "Which DB?");
  assert.deepEqual(m.attention[0].options, ["sqlite", "postgres"]);
  assert.equal(m.attention[0].kindLabel, "question");
  assert.equal(m.attention[1].kind, "permission");
  assert.equal(m.attention[1].text, "write infra/main.tf");
  assert.equal(m.attention[1].kindLabel, "permission request");
});

test("homeModel: running/queued merge into one live strip with progress/position", () => {
  const m = homeModel({
    running: [{ task_id: "r1", title: "Fix parser", project: "alpha",
                progress: { passed: 2, total: 5 } },
              { task_id: "r2", project: "beta" }],
    queued: [{ task_id: "q1", title: "Docs", project: "alpha", position: 1 }],
  });
  assert.equal(m.live.length, 3);
  assert.deepEqual(m.live.map(r => r.state), ["running", "running", "queued"]);
  assert.equal(m.live[0].progressLabel, "2/5 subtasks");
  assert.equal(m.live[1].progressLabel, "");
  assert.equal(m.live[1].title, "r2");   // falls back to the task id
  assert.equal(m.live[2].progressLabel, "#1");
});

test("homeModel: recent rows + spend card + workspaces + counts", () => {
  const m = homeModel({
    recent: [{ task_id: "t9", title: "Add tests", project: "alpha",
               status: "completed", quality: 91, cost_usd: 1.5 },
             { task_id: "t8", status: "failed" }],
    spend: { window_days: 30, total_usd: 12.345, runs: 4,
             by_project: [{ project: "alpha", usd: 10 }, { project: "beta", usd: 2.345 }] },
    workspaces: [{ slug: "shop", name: "Shop", projects: 2 },
                 { slug: "solo", name: "Solo", projects: 1 }],
    counts: { projects: 3, workspaces: 2, custom_agents: 1 },
  });
  assert.equal(m.recent[0].qualityLabel, "quality 91/100");
  assert.equal(m.recent[0].costLabel, "$1.5000");
  assert.equal(m.recent[1].qualityLabel, "");
  assert.equal(m.spend.totalLabel, "$12.35");
  assert.deepEqual(m.spend.topProject, { project: "alpha", usdLabel: "$10.00" });
  assert.equal(m.workspaces[0].projectsLabel, "2 projects");
  assert.equal(m.workspaces[1].projectsLabel, "1 project");
  assert.deepEqual(m.counts, { projects: 3, workspaces: 2, custom_agents: 1 });
});

test("homeModel: benchmark deltas get arrows and the series becomes bars", () => {
  const m = homeModel({
    benchmarks: {
      latest: { pass_rate: 0.8, quality_mean: 84 },
      delta: { pass_rate: 0.1, quality_mean: -2, cost_usd: 0 },
      series: [{ sha: "abcdef1234", pass_rate: 0.7 }, { sha: "1234567890", pass_rate: 0.8 }],
    },
  });
  assert.equal(m.benchmarks.available, true);
  assert.equal(m.benchmarks.passLabel, "80%");
  assert.equal(m.benchmarks.qualityLabel, "84");
  assert.equal(m.benchmarks.passDelta.dir, "up");
  assert.equal(m.benchmarks.qualityDelta.dir, "down");
  assert.equal(m.benchmarks.bars.length, 2);
  assert.equal(m.benchmarks.bars[0].hPct, 70);
  assert.equal(m.benchmarks.bars[0].sha, "abcdef1");
});

test("homeModel: empty/malformed payload yields quiet defaults + error passthrough", () => {
  const m = homeModel({ attention: null, benchmarks: { latest: null },
                        errors: ["spend: boom"] });
  assert.equal(m.prominent, false);
  assert.deepEqual(m.live, []);
  assert.equal(m.benchmarks.available, false);
  assert.equal(m.benchmarks.passLabel, "—");
  assert.equal(m.spend.totalLabel, "$0.00");
  assert.equal(m.spend.topProject, null);
  assert.deepEqual(m.errors, ["spend: boom"]);
  assert.deepEqual(homeModel(null).counts, { projects: 0, workspaces: 0, custom_agents: 0 });
});

// ---- sidebarGroups ----
test("sidebarGroups: groups by workspace in first-appearance order, hides default", () => {
  const { groups, ungrouped } = sidebarGroups([
    { slug: "a", workspace: "shop" },
    { slug: "b", workspace: null },
    { slug: "default" },                      // scratch default never surfaces
    { slug: "c", workspace: "infra" },
    { slug: "d", workspace: "shop" },
  ]);
  assert.deepEqual(groups.map(g => g.workspace), ["shop", "infra"]);
  assert.deepEqual(groups[0].projects.map(p => p.slug), ["a", "d"]);
  assert.deepEqual(ungrouped.map(p => p.slug), ["b"]);
});

// ---- wsDepsModel ----
test("wsDepsModel: per-member upstream options exclude self and mark current deps", () => {
  const m = wsDepsModel({
    project_slugs: ["api", "web", "docs"],
    default_deps: { web: ["api"], docs: ["api", "web"] },
  });
  assert.equal(m.editable, true);
  assert.equal(m.rows.length, 3);
  const web = m.rows.find(r => r.slug === "web");
  assert.deepEqual(web.upstreams.map(u => u.slug), ["api", "docs"]);
  assert.deepEqual(web.upstreams.map(u => u.selected), [true, false]);
  const api = m.rows.find(r => r.slug === "api");
  assert.deepEqual(api.upstreams.map(u => u.selected), [false, false]);
  assert.equal(wsDepsModel({ project_slugs: ["one"] }).editable, false);
});

// ---- wsRunPayload ----
test("wsRunPayload: prompt required; full subset collapses to no subset", () => {
  const bad = wsRunPayload({ prompt: "  ", members: ["a", "b"], subset: [] });
  assert.equal(bad.ok, false);
  assert.ok(bad.errors.some(e => /prompt/.test(e)));
  assert.ok(bad.errors.some(e => /member/.test(e)));
  const all = wsRunPayload({ prompt: "ship it", members: ["a", "b"], subset: ["a", "b"] });
  assert.equal(all.ok, true);
  assert.deepEqual(all.body, { prompt: "ship it" });   // subset omitted when = all members
  const some = wsRunPayload({ prompt: "ship it", members: ["a", "b", "c"], subset: ["b", "zzz"] });
  assert.equal(some.ok, true);
  assert.deepEqual(some.body.subset, ["b"]);           // non-members dropped
});

test("wsRunPayload: title/effort/budget are optional and validated", () => {
  const m = wsRunPayload({ prompt: "p", title: " T ", effort: "high", budget: "2.5",
                           members: ["a"], subset: ["a"] });
  assert.equal(m.ok, true);
  assert.deepEqual(m.body, { prompt: "p", title: "T", effort: "high", budget: 2.5 });
  const bad = wsRunPayload({ prompt: "p", budget: "-3", members: [], subset: null });
  assert.equal(bad.ok, false);
  assert.ok(bad.errors.some(e => /budget/.test(e)));
});

// ---- graph2ViewModel ----
const G2 = {
  nodes: [
    { id: "src/app.py", label: "app.py", type: "file", degree: 3, weight: 6 },
    { id: "t1", label: "Fix parser", type: "task", degree: 2, weight: 4 },
    { id: "coder", label: "coder", type: "agent", degree: 1, weight: 1 },
    { id: "auth", label: "auth flow", type: "concept", degree: 0, weight: 0 },
  ],
  edges: [
    { src: "t1", dst: "src/app.py", relation: "produced_file", weight: 3, layer: "run" },
    { src: "t1", dst: "coder", relation: "assigned_to", weight: 1, layer: "run" },
    { src: "src/app.py", dst: "auth", relation: "implements", weight: 1, layer: "domain" },
  ],
};

test("graph2ViewModel: layer filter keeps matching edges and their nodes only", () => {
  const m = graph2ViewModel(G2, { layer: "run", minWeight: 1 });
  assert.deepEqual(m.edges.map(e => e.relation), ["produced_file", "assigned_to"]);
  assert.deepEqual(m.nodes.map(n => n.id).sort(), ["coder", "src/app.py", "t1"]);
  const dom = graph2ViewModel(G2, { layer: "domain", minWeight: 1 });
  assert.deepEqual(dom.nodes.map(n => n.id).sort(), ["auth", "src/app.py"]);
});

test("graph2ViewModel: min-weight filter; unfiltered keeps isolated nodes", () => {
  const all = graph2ViewModel(G2, { layer: "", minWeight: 1 });
  assert.equal(all.nodes.length, 4);          // isolated "auth" survives
  assert.equal(all.statsLabel, "4/4 nodes · 3/3 edges");
  const heavy = graph2ViewModel(G2, { layer: "", minWeight: 2 });
  assert.deepEqual(heavy.edges.map(e => e.weight), [3]);
  assert.deepEqual(heavy.nodes.map(n => n.id).sort(), ["src/app.py", "t1"]);
});

test("graph2ViewModel: sizes scale with weight, colors follow type, legend lists present types", () => {
  const m = graph2ViewModel(G2, {});
  const byId = Object.fromEntries(m.nodes.map(n => [n.id, n]));
  assert.equal(byId["src/app.py"].colorKey, "file");
  assert.equal(byId.t1.colorKey, "task");
  assert.equal(byId.coder.colorKey, "agent");
  assert.equal(byId.auth.colorKey, "concept");
  assert.ok(byId["src/app.py"].r > byId.coder.r);   // weight 6 vs 1
  assert.equal(byId.auth.r, 6);                     // weight 0 -> minimum radius
  assert.deepEqual(m.legend, ["file", "task", "agent", "concept"]);
  assert.equal(byId.t1.label, "Fix parser");
  const sub = graph2ViewModel({ nodes: [{ id: "s", type: "subtask" }], edges: [] }, {});
  assert.equal(sub.nodes[0].colorKey, "task");      // legacy subtask maps to task
});

// ---- nodePanelModel ----
test("nodePanelModel: incident edges get direction, labels and provenance", () => {
  const m = nodePanelModel({
    node: "src/app.py",
    nodes: G2.nodes,
    edges: [
      { src: "t1", dst: "src/app.py", relation: "produced_file", weight: 3,
        layer: "run", last_ts: 1750000000 },
      { src: "src/app.py", dst: "auth", relation: "implements", weight: 1, layer: "domain" },
      { src: "t1", dst: "coder", relation: "assigned_to", weight: 1, layer: "run" },
    ],
  });
  assert.equal(m.empty, false);
  assert.equal(m.label, "app.py");
  assert.equal(m.type, "file");
  assert.equal(m.degree, 3);
  assert.equal(m.edges.length, 2);            // only incident edges
  assert.equal(m.edges[0].dir, "←");
  assert.equal(m.edges[0].otherLabel, "Fix parser");
  assert.equal(m.edges[0].lastTs, 1750000000);
  assert.equal(m.edges[1].dir, "→");
  assert.equal(m.edges[1].lastTs, null);
  assert.equal(m.fileTask, "t1");             // produced_file provenance -> open-in-files
});

test("nodePanelModel: unknown node -> empty panel", () => {
  const m = nodePanelModel({ node: "ghost", nodes: G2.nodes, edges: G2.edges });
  assert.equal(m.empty, true);
  assert.deepEqual(m.edges, []);
  assert.equal(nodePanelModel(null).empty, true);
});

// ---- agentsListModel ----
test("agentsListModel: new {builtin,custom,tools} shape merges names; legacy array tolerated", () => {
  const m = agentsListModel({
    builtin: [{ name: "coder", tools: ["write_file"] }],
    custom: [{ name: "sec_reviewer", system_prompt: "…" }],
    tools: ["read_file", "write_file"],
  });
  assert.deepEqual(m.names, ["coder", "sec_reviewer"]);
  assert.deepEqual(m.tools, ["read_file", "write_file"]);
  assert.equal(m.custom.length, 1);
  const legacy = agentsListModel([{ name: "coder" }, { name: "documenter" }]);
  assert.deepEqual(legacy.names, ["coder", "documenter"]);
  assert.deepEqual(legacy.custom, []);
  assert.deepEqual(agentsListModel(null).names, []);
});

// ---- agentFormModel / agentFormValidate ----
test("agentFormModel: prefills from a stored spec and checks its tools", () => {
  const m = agentFormModel(
    { name: "sec", description: "d", when_to_use: "w", system_prompt: "sp",
      tools: ["read_file"], effort: "high", model: "opus" },
    ["read_file", "write_file"]);
  assert.equal(m.editing, true);
  assert.equal(m.heading, "Edit agent — sec");
  assert.deepEqual(m.tools, [{ name: "read_file", checked: true },
                             { name: "write_file", checked: false }]);
  assert.equal(m.effort, "high");
  const blank = agentFormModel(null, ["read_file"]);
  assert.equal(blank.editing, false);
  assert.equal(blank.heading, "New agent");
  assert.equal(blank.effort, "");
});

test("agentFormValidate: happy path builds the POST spec", () => {
  const v = agentFormValidate({
    name: "  Sec_Bot ", description: " d ", when_to_use: "w", system_prompt: "sp",
    tools: ["read_file"], effort: "", model: " opus ",
  }, ["read_file", "write_file"]);
  assert.equal(v.ok, true);
  assert.deepEqual(v.spec, {
    name: "sec_bot", description: "d", when_to_use: "w", system_prompt: "sp",
    tools: ["read_file"], effort: "", model: "opus",
  });
});

test("agentFormValidate: rejects bad name, empty fields, unknown tools and bad effort", () => {
  const v = agentFormValidate({
    name: "9bad-name", description: "", when_to_use: "w", system_prompt: "sp",
    tools: ["nope"], effort: "extreme",
  }, ["read_file"]);
  assert.equal(v.ok, false);
  assert.ok(v.errors.some(e => /name/.test(e)));
  assert.ok(v.errors.some(e => /description/.test(e)));
  assert.ok(v.errors.some(e => /unknown tools: nope/.test(e)));
  assert.ok(v.errors.some(e => /effort/.test(e)));
  const noTools = agentFormValidate({ name: "ok", description: "d", when_to_use: "w",
                                      system_prompt: "s", tools: [] }, ["read_file"]);
  assert.ok(noTools.errors.some(e => /at least one tool/.test(e)));
});
