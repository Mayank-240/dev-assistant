// Unit tests for the F4 review/permissions/project-home models in util.js.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  renderDiffHtml, reviewCardModel, permissionsModel, policyFormModel, parsePolicyForm,
  sparklinePoints,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- renderDiffHtml ----
test("renderDiffHtml colors adds/dels/hunks and escapes content", () => {
  const html = renderDiffHtml('+++ b/a.py\n@@ -1 +1 @@\n+added <script>\n-removed\n context');
  assert.ok(html.includes('<span class="d-file">+++ b/a.py</span>'));
  assert.ok(html.includes('<span class="d-hunk">@@ -1 +1 @@</span>'));
  assert.ok(html.includes('<span class="d-add">+added &lt;script&gt;</span>'));
  assert.ok(html.includes('<span class="d-del">-removed</span>'));
  assert.ok(!html.includes("<script>"));  // XSS boundary
  assert.ok(html.includes(" context"));   // ctx lines stay unwrapped
});

test("renderDiffHtml returns empty string for empty input", () => {
  assert.equal(renderDiffHtml(""), "");
  assert.equal(renderDiffHtml(null), "");
});

// ---- reviewCardModel ----
test("reviewCardModel maps a passed subtask with a merge commit", () => {
  const m = reviewCardModel({
    id: "s1", title: "Do it", agent: "coder", status: "passed", attempts: 2,
    verdict: { passed: true, score: 92, reasons: ["solid"], suggestions: ["more"] },
    changed: ["a.py", "b.py"], merge_commit: "abc1234def5678", decision: null,
    diff: "+++ x",
  });
  assert.equal(m.badge, "passed");
  assert.equal(m.score, 92);
  assert.deepEqual(m.reasons, ["solid"]);
  assert.deepEqual(m.suggestions, ["more"]);
  assert.deepEqual(m.changed, ["a.py", "b.py"]);
  assert.equal(m.attempts, 2);
  assert.equal(m.mergeShort, "abc1234");
  assert.equal(m.hasDiff, true);
  assert.equal(m.canAccept, true);
  assert.equal(m.canReject, true);
  assert.equal(m.decision, null);
});

test("reviewCardModel: failed verdict, no merge commit, recorded decisions", () => {
  const failed = reviewCardModel({ id: "s2", verdict: { passed: false, score: 30 } });
  assert.equal(failed.badge, "failed");
  assert.equal(failed.canAccept, false);   // nothing to merge
  assert.equal(failed.hasDiff, false);
  const accepted = reviewCardModel({ id: "s3", merge_commit: "abc", decision: "accepted" });
  assert.equal(accepted.canAccept, false); // already accepted
  assert.equal(accepted.canReject, true);
  const rejected = reviewCardModel({ id: "s4", merge_commit: "abc", decision: "rejected" });
  assert.equal(rejected.canReject, false);
  assert.equal(rejected.canAccept, true);  // can still change your mind
});

test("reviewCardModel: pending when no verdict; junk decision dropped", () => {
  const m = reviewCardModel({ id: "s5", decision: "bogus" });
  assert.equal(m.badge, "pending");
  assert.equal(m.score, null);
  assert.equal(m.decision, null);
  assert.deepEqual(m.reasons, []);
});

test("reviewCardModel normalizes criteria breakdown (strings and objects)", () => {
  const m = reviewCardModel({
    id: "s6",
    verdict: { passed: true, criteria: [
      "plain string",
      { name: "has tests", met: true },
      { criterion: "documented", passed: false },
      { name: "" },  // dropped: no name
    ] },
  });
  assert.deepEqual(m.criteria, [
    { name: "plain string", met: null },
    { name: "has tests", met: true },
    { name: "documented", met: false },
  ]);
});

// ---- permissionsModel ----
test("permissionsModel maps policy, tools and denied rows", () => {
  const m = permissionsModel({
    policy: { budget_usd: 5, protected_paths: ["infra/**"], sandbox: true },
    tools_by_agent: { coder: ["write_file"], researcher: [] },
    review_target: "main", task_branch: "ada/t1",
    denied: [{ ts: 2, agent: "coder", tool: "write_file", outcome: "DENIED: protected path" }],
  });
  assert.deepEqual(m.policyRows.map(r => r.key), ["budget_usd", "protected_paths", "sandbox"]);
  assert.equal(m.policyRows.find(r => r.key === "protected_paths").value, '["infra/**"]');
  assert.equal(m.policyRows.find(r => r.key === "budget_usd").value, "5");
  assert.deepEqual(m.agents, [{ agent: "coder", tools: ["write_file"] },
                              { agent: "researcher", tools: [] }]);
  assert.equal(m.deniedEmpty, false);
  assert.equal(m.denied[0].outcome, "DENIED: protected path");
  assert.equal(m.reviewTarget, "main");
  assert.equal(m.taskBranch, "ada/t1");
});

test("permissionsModel: empty/absent inputs yield the empty-state model", () => {
  const m = permissionsModel(null);
  assert.deepEqual(m.policyRows, []);
  assert.deepEqual(m.agents, []);
  assert.equal(m.deniedEmpty, true);  // renders "no denied actions"
  assert.equal(m.reviewTarget, "");
});

// ---- policyFormModel / parsePolicyForm ----
test("policyFormModel round-trips through parsePolicyForm", () => {
  const policy = { budget_usd: 2.5, effort: "high", git_mode: "branch",
                   protected_paths: ["infra/**", "*.lock"] };
  const form = policyFormModel(policy);
  assert.equal(form.budget_usd, "2.5");
  assert.equal(form.protected_paths, "infra/**\n*.lock");
  const parsed = parsePolicyForm(form);
  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.policy, policy);
});

test("parsePolicyForm: blanks are omitted, protected_paths always sent", () => {
  const parsed = parsePolicyForm({ budget_usd: "", effort: "", git_mode: "", protected_paths: "" });
  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.policy, { protected_paths: [] });  // clearing is explicit
});

test("parsePolicyForm rejects bad budget and bad git_mode", () => {
  const bad = parsePolicyForm({ budget_usd: "-3", git_mode: "yolo", protected_paths: "" });
  assert.equal(bad.ok, false);
  assert.equal(bad.errors.length, 2);
  assert.ok(bad.errors[0].includes("budget_usd"));
  assert.ok(bad.errors[1].includes("git_mode"));
});

test("parsePolicyForm splits protected paths on newlines and commas", () => {
  const p = parsePolicyForm({ protected_paths: "infra/**\n *.lock, secrets/**\n\n" });
  assert.deepEqual(p.policy.protected_paths, ["infra/**", "*.lock", "secrets/**"]);
});

// ---- sparklinePoints ----
test("sparklinePoints maps values to bounded polyline geometry", () => {
  const sp = sparklinePoints([0, 50, 100], 100, 20, 0);
  assert.equal(sp.drawable, true);
  const pts = sp.points.split(" ").map(p => p.split(",").map(Number));
  assert.equal(pts.length, 3);
  assert.deepEqual(pts[0], [0, 20]);   // min value sits at the bottom
  assert.deepEqual(pts[2], [100, 0]);  // max at the top-right
  assert.equal(pts[1][1], 10);         // midpoint halfway up
  assert.equal(sp.last, 100);
  assert.equal(sp.min, 0);
  assert.equal(sp.max, 100);
});

test("sparklinePoints: flat series stays in-bounds, few points not drawable", () => {
  const flat = sparklinePoints([70, 70, 70], 100, 20, 2);
  assert.equal(flat.drawable, true);  // span guard avoids division by zero
  for (const [, y] of flat.points.split(" ").map(p => p.split(",").map(Number))) {
    assert.ok(y >= 0 && y <= 20);
  }
  assert.equal(sparklinePoints([80], 100, 20, 2).drawable, false);
  assert.equal(sparklinePoints([80], 100, 20, 2).last, 80);
  assert.equal(sparklinePoints([], 100, 20, 2).drawable, false);
  assert.equal(sparklinePoints(null, 100, 20, 2).last, null);
  // nulls/non-numbers are skipped, not plotted as zero
  assert.equal(sparklinePoints([null, 90, undefined, 60], 100, 20, 2).drawable, true);
});
