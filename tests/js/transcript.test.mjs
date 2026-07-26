// Unit tests for the full-transcript panel helpers in web/static/util.js.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  transcriptLineModel, transcriptViewModel, formatStepLine, clipText, reduceRunEvent,
  initialRunAggregates, makeAgentRecord,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- transcriptLineModel: one step -> {cls, prefix, text} ----

test("thinking steps render dim/italic class with no prefix", () => {
  assert.deepEqual(transcriptLineModel({ kind: "thinking", text: "hmm" }),
    { cls: "tl-thinking", prefix: "", text: "hmm" });
});

test("plain text steps use the default class", () => {
  assert.deepEqual(transcriptLineModel({ kind: "text", text: "done part 1" }),
    { cls: "tl-text", prefix: "", text: "done part 1" });
  // missing kind defaults to text
  assert.equal(transcriptLineModel({ text: "x" }).cls, "tl-text");
});

test("tool steps show name + input behind a mono prefix", () => {
  assert.deepEqual(transcriptLineModel({ kind: "tool", tool: "write_file", input: '{"path":"a.py"}' }),
    { cls: "tl-tool", prefix: "▸ ", text: 'write_file {"path":"a.py"}' });
  // no input -> just the name; no name -> placeholder
  assert.equal(transcriptLineModel({ kind: "tool", tool: "ls" }).text, "ls");
  assert.equal(transcriptLineModel({ kind: "tool" }).text, "?");
});

test("tool_result steps are indented and attribute the tool", () => {
  const m = transcriptLineModel({ kind: "tool_result", tool: "run_tests", text: "3 passed" });
  assert.deepEqual(m, { cls: "tl-tool-result", prefix: "↳ ", text: "run_tests → 3 passed" });
  // unattributed result (SDK could not map the tool_use_id)
  assert.equal(transcriptLineModel({ kind: "tool_result", text: "ok" }).text, "ok");
});

test("errored tool_result gets the red-tint class", () => {
  const m = transcriptLineModel({ kind: "tool_result", tool: "bash", text: "boom", is_error: true });
  assert.equal(m.cls, "tl-tool-result tl-err");
});

test("final result steps get the bordered class", () => {
  assert.deepEqual(transcriptLineModel({ kind: "result", text: "All done." }),
    { cls: "tl-final", prefix: "", text: "All done." });
});

test("transcriptLineModel tolerates null/empty steps", () => {
  assert.deepEqual(transcriptLineModel(null), { cls: "tl-text", prefix: "", text: "" });
});

// ---- transcriptViewModel: endpoint payload -> panel view model ----

test("transcriptViewModel maps endpoint payload to header + lines", () => {
  const vm = transcriptViewModel({
    agent: "coder", count: 3,
    steps: [
      { kind: "thinking", text: "plan" },
      { kind: "tool", tool: "write_file", input: "{}" },
      { kind: "tool_result", tool: "write_file", text: "written", is_error: false },
    ],
  });
  assert.equal(vm.agent, "coder");
  assert.equal(vm.count, 3);
  assert.equal(vm.countLabel, "3 steps");
  assert.deepEqual(vm.lines.map(l => l.cls), ["tl-thinking", "tl-tool", "tl-tool-result"]);
});

test("transcriptViewModel handles empty/missing payloads", () => {
  assert.deepEqual(transcriptViewModel(null), { agent: "", count: 0, countLabel: "0 steps", lines: [] });
  assert.equal(transcriptViewModel({ steps: [{ text: "a" }] }).countLabel, "1 step");
  // count falls back to steps.length when absent
  assert.equal(transcriptViewModel({ steps: [{}, {}] }).count, 2);
});

// ---- formatStepLine: new kinds in the compact card ticker ----

test("formatStepLine renders tool_result and result kinds", () => {
  assert.equal(formatStepLine({ kind: "tool_result", tool: "bash", text: "ok" }), "↳ bash: ok");
  assert.equal(formatStepLine({ kind: "tool_result", text: "ok" }), "↳ ok");
  assert.equal(formatStepLine({ kind: "result", text: "final" }), "final");
});

// ---- clipText: display clip for compact streams ----

test("clipText clips long text with an ellipsis and passes short text through", () => {
  assert.equal(clipText("short", 300), "short");
  const clipped = clipText("x".repeat(400), 300);
  assert.equal(clipped.length, 301);
  assert.ok(clipped.endsWith("…"));
  assert.equal(clipText(null), "");
});

// ---- reduceRunEvent: agent_step keeps is_error for the transcript/modal ----

test("agent_step aggregation preserves is_error", () => {
  const state = initialRunAggregates();
  state.agentData.s1 = makeAgentRecord({ id: "s1", agent: "coder" });
  reduceRunEvent(state, { type: "agent_step", data: { id: "s1", kind: "tool_result", tool: "bash", text: "err", is_error: true } });
  reduceRunEvent(state, { type: "agent_step", data: { id: "s1", kind: "text", text: "fine" } });
  assert.equal(state.agentData.s1.steps.length, 2);
  assert.equal(state.agentData.s1.steps[0].is_error, true);
  assert.equal(state.agentData.s1.steps[1].is_error, false);
});
