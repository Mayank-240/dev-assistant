// Unit tests for the agent-surfaces pure models in web/static/util.js:
// roster stats line, starter templates, project scope and the test-drive panel.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  agentStatsModel, AGENT_TEMPLATES, agentTemplate, templateTools,
  agentTemplatePrefill, agentScopeModel, testDriveModel,
  agentFormModel, agentFormValidate,
} = require("../../src/ai_dev_assistant/web/static/util.js");

const TOOLBOX = [
  "recall", "remember", "kb_search", "kg_query", "kg_write", "read_file", "write_file",
  "edit_file", "apply_patch", "list_dir", "grep", "symbols", "find_references",
  "run_command", "install_packages", "git_status", "git_diff", "send_message",
  "read_messages", "blackboard_read", "blackboard_write", "run_tests", "delegate",
  "web_fetch", "ask_operator", "request_permission",
];

// ---- agentStatsModel ----
test("agent stats: healthy sample renders line, rate and bar", () => {
  const m = agentStatsModel({ stats: { n: 8, passed: 6, pass_rate: 0.75 } });
  assert.equal(m.present, true);
  assert.equal(m.lowData, false);
  assert.equal(m.line, "routed 8 · 75%");
  assert.equal(m.rateLabel, "75%");
  assert.equal(m.barPct, 75);
  assert.ok(m.title.includes("8"));
});

test("agent stats: low data (n<5) dashes the rate with a low-data tooltip", () => {
  const m = agentStatsModel({ stats: { n: 2, passed: 2, pass_rate: 1.0 } });
  assert.equal(m.present, true);
  assert.equal(m.lowData, true);
  assert.equal(m.line, "routed 2 · —");
  assert.equal(m.barPct, 0);
  assert.equal(m.title, "low data");
});

test("agent stats: absent, empty, or never-routed entries render nothing", () => {
  assert.equal(agentStatsModel({}).present, false);                      // no stats key
  assert.equal(agentStatsModel(null).present, false);
  assert.equal(agentStatsModel({ stats: null }).present, false);
  assert.equal(agentStatsModel({ stats: { n: 0, passed: 0, pass_rate: null } }).present, false);
});

// ---- templates ----
test("templates: 4 starters, each a complete valid prefill against the real toolbox", () => {
  assert.equal(AGENT_TEMPLATES.length, 4);
  assert.deepEqual(AGENT_TEMPLATES.map(t => t.key), [
    "code-style-enforcer", "data-scientist", "compliance-reviewer", "prompt-engineer",
  ]);
  for (const t of AGENT_TEMPLATES) {
    const p = agentTemplatePrefill(t, TOOLBOX);
    assert.ok(p.name && p.description && p.when_to_use, t.key + " has the text fields");
    assert.ok(p.system_prompt.length > 300, t.key + " prompt is a full persona");
    assert.ok(p.tools.length >= 5, t.key + " keeps a sensible tool subset");
    assert.equal(p.effort, "");
    assert.equal(p.model, "");
    // and the prefill passes the same validation the save path applies
    const v = agentFormValidate(p, TOOLBOX);
    assert.deepEqual(v.errors, [], t.key + " validates clean");
  }
  assert.equal(agentTemplate("prompt-engineer").name, "prompt_engineer");
  assert.equal(agentTemplate("nope"), null);
});

test("templates: tool intents intersect with the live toolbox, order kept", () => {
  assert.deepEqual(
    templateTools(["read_file", "quantum_debug", "grep"], ["grep", "read_file"]),
    ["read_file", "grep"]);
  assert.deepEqual(templateTools(["read_file"], []), []);
  assert.deepEqual(templateTools(null, TOOLBOX), []);
  // a slimmed toolbox shrinks a template's prefill instead of invalidating it
  const p = agentTemplatePrefill(agentTemplate("data-scientist"), ["read_file", "grep"]);
  assert.deepEqual(p.tools, ["read_file", "grep"]);
});

// ---- scope ----
test("scope chip: global vs project slug", () => {
  const g = agentScopeModel({ name: "x" });
  assert.equal(g.scoped, false);
  assert.equal(g.label, "global");
  const s = agentScopeModel({ project: "alpha" });
  assert.equal(s.scoped, true);
  assert.equal(s.label, "alpha");
  assert.ok(s.title.includes("alpha"));
});

test("scope: form model surfaces project and validate passes it through only when set", () => {
  assert.equal(agentFormModel({ project: "alpha" }, TOOLBOX).project, "alpha");
  assert.equal(agentFormModel(null, TOOLBOX).project, "");
  const base = { name: "helper", description: "d", when_to_use: "w",
                 system_prompt: "s", tools: ["grep"], effort: "", model: "" };
  const scoped = agentFormValidate({ ...base, project: "alpha" }, TOOLBOX);
  assert.equal(scoped.ok, true);
  assert.equal(scoped.spec.project, "alpha");
  const global = agentFormValidate({ ...base, project: "  " }, TOOLBOX);
  assert.equal(global.ok, true);
  assert.ok(!("project" in global.spec), "blank scope omits the key (server default = global)");
});

// ---- test drive ----
test("test drive: success renders text plus cost · seconds meta", () => {
  const m = testDriveModel({ ok: true, status: 200 },
                           { result: "Hi, I check style first.", cost_usd: 0.0132, seconds: 3.41 });
  assert.equal(m.ok, true);
  assert.equal(m.text, "Hi, I check style first.");
  assert.equal(m.metaLabel, "$0.0132 · 3.4s");
  assert.equal(m.error, "");
});

test("test drive: error variants — body error, bare HTTP failure, empty body", () => {
  const e1 = testDriveModel({ ok: false, status: 504 }, { error: "test drive timed out after 120s" });
  assert.equal(e1.ok, false);
  assert.equal(e1.error, "test drive timed out after 120s");
  const e2 = testDriveModel({ ok: false, status: 503 }, {});
  assert.equal(e2.ok, false);
  assert.equal(e2.error, "HTTP 503");
  // a 200 body carrying an error field is still an error
  const e3 = testDriveModel({ ok: true, status: 200 }, { error: "nope" });
  assert.equal(e3.ok, false);
  assert.equal(e3.text, "");
});
