// Unit tests for the global settings console models in web/static/util.js:
// settingsGroupsModel, fieldControlModel, coerceFieldInput.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  settingsGroupsModel, fieldControlModel, coerceFieldInput,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- fieldControlModel ----

test("fieldControlModel maps types to controls", () => {
  assert.equal(fieldControlModel({ key: "trace", type: "bool" }).control, "checkbox");
  assert.equal(fieldControlModel({ key: "n", type: "int" }).control, "number");
  assert.equal(fieldControlModel({ key: "n", type: "float" }).control, "number");
  assert.equal(fieldControlModel({ key: "m", type: "str" }).control, "text");
  assert.equal(fieldControlModel({ key: "s", type: "choice" }).control, "select");
});

test("fieldControlModel number step: int -> 1, float -> any", () => {
  assert.equal(fieldControlModel({ key: "n", type: "int" }).step, "1");
  assert.equal(fieldControlModel({ key: "n", type: "float" }).step, "any");
  assert.equal(fieldControlModel({ key: "s", type: "str" }).step, "");
});

test("fieldControlModel bool carries checked, not value coercion surprises", () => {
  assert.equal(fieldControlModel({ key: "t", type: "bool", value: true }).checked, true);
  assert.equal(fieldControlModel({ key: "t", type: "bool", value: false }).checked, false);
  assert.equal(fieldControlModel({ key: "s", type: "str", value: "x" }).checked, false);
});

test("fieldControlModel source badge: default | .env | override", () => {
  const dflt = fieldControlModel({ key: "k", source: "default" });
  assert.deepEqual([dflt.sourceLabel, dflt.sourceCls, dflt.showReset],
    ["default", "gs-src-default", false]);
  const env = fieldControlModel({ key: "k", source: "env" });
  assert.deepEqual([env.sourceLabel, env.sourceCls, env.showReset],
    [".env", "gs-src-env", false]);
  const ov = fieldControlModel({ key: "k", source: "override" });
  assert.deepEqual([ov.sourceLabel, ov.sourceCls, ov.showReset],
    ["override", "gs-src-override", true]);
  // unknown source degrades to default
  assert.equal(fieldControlModel({ key: "k", source: "???" }).source, "default");
});

test("fieldControlModel carries restart flag, choices and help", () => {
  const f = fieldControlModel({
    key: "embeddings_backend", label: "Embeddings backend", help: "Vector backend.",
    type: "choice", choices: ["fastembed", "hash"], value: "hash", restart_required: true,
  });
  assert.equal(f.restart, true);
  assert.deepEqual(f.choices, ["fastembed", "hash"]);
  assert.equal(f.value, "hash");
  assert.equal(f.help, "Vector backend.");
});

test("fieldControlModel tolerates missing input", () => {
  const f = fieldControlModel(null);
  assert.equal(f.control, "text");
  assert.equal(f.value, "");
  assert.equal(f.restart, false);
});

// ---- settingsGroupsModel ----

const payload = {
  groups: [
    { name: "Observability", fields: [
      { key: "trace", label: "Trace", help: "h", type: "bool", value: true,
        source: "default", restart_required: false, choices: [] },
    ] },
    { name: "Guardrails & Budget", fields: [
      { key: "budget_usd", label: "Budget", help: "", type: "float", value: 2.5,
        source: "override", restart_required: false, choices: [] },
    ] },
  ],
  info: { llm_backend: "claude_sdk", data_dir: ".ada_data", projects: 3 },
};

test("settingsGroupsModel maps groups + fields in order", () => {
  const m = settingsGroupsModel(payload);
  assert.deepEqual(m.groups.map(g => g.name), ["Observability", "Guardrails & Budget"]);
  assert.equal(m.groups[0].fields[0].control, "checkbox");
  assert.equal(m.groups[1].fields[0].showReset, true);
});

test("settingsGroupsModel builds the read-only info rows", () => {
  const m = settingsGroupsModel(payload);
  assert.deepEqual(m.info, [
    { label: "backend", value: "claude_sdk" },
    { label: "data dir", value: ".ada_data" },
    { label: "projects", value: "3" },
  ]);
});

test("settingsGroupsModel tolerates empty/missing payloads", () => {
  assert.deepEqual(settingsGroupsModel(null).groups, []);
  assert.equal(settingsGroupsModel({}).info[0].value, "—");
  assert.equal(settingsGroupsModel({ info: { projects: 0 } }).info[2].value, "0");
});

// ---- coerceFieldInput ----

test("coerceFieldInput bool passes checkbox state through", () => {
  assert.deepEqual(coerceFieldInput({ type: "bool" }, true), { ok: true, value: true });
  assert.deepEqual(coerceFieldInput({ type: "bool" }, false), { ok: true, value: false });
});

test("coerceFieldInput int accepts whole numbers only", () => {
  assert.deepEqual(coerceFieldInput({ type: "int" }, "12"), { ok: true, value: 12 });
  assert.deepEqual(coerceFieldInput({ type: "int" }, " -3 "), { ok: true, value: -3 });
  assert.equal(coerceFieldInput({ type: "int", label: "Turns" }, "3.5").ok, false);
  assert.equal(coerceFieldInput({ type: "int" }, "abc").ok, false);
  assert.equal(coerceFieldInput({ type: "int" }, "").ok, false);
});

test("coerceFieldInput float accepts finite numbers", () => {
  assert.deepEqual(coerceFieldInput({ type: "float" }, "2.5"), { ok: true, value: 2.5 });
  assert.deepEqual(coerceFieldInput({ type: "float" }, "0"), { ok: true, value: 0 });
  assert.equal(coerceFieldInput({ type: "float" }, "abc").ok, false);
  assert.equal(coerceFieldInput({ type: "float" }, "").ok, false);
});

test("coerceFieldInput choice must be a member", () => {
  const f = { type: "choice", choices: ["branch", "merge"], label: "Git mode" };
  assert.deepEqual(coerceFieldInput(f, "merge"), { ok: true, value: "merge" });
  const bad = coerceFieldInput(f, "rebase");
  assert.equal(bad.ok, false);
  assert.match(bad.error, /Git mode must be one of/);
});

test("coerceFieldInput str trims and passes through", () => {
  assert.deepEqual(coerceFieldInput({ type: "str" }, "  claude-x "),
    { ok: true, value: "claude-x" });
  assert.deepEqual(coerceFieldInput({ type: "str" }, ""), { ok: true, value: "" });
});

test("coerceFieldInput error message names the field label", () => {
  const r = coerceFieldInput({ type: "int", label: "Agent max turns" }, "many");
  assert.equal(r.ok, false);
  assert.match(r.error, /^Agent max turns /);
});
