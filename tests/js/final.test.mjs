// Unit tests for the closing-wave pure models in web/static/util.js:
// the Benchmarks panel (benchModel), knowledge-graph distillation
// (distillReportModel / distillResultModel) and named-user identity
// (userChipModel / usersListModel / userCreateModel).
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  benchModel, distillReportModel, distillResultModel,
  userChipModel, usersListModel, userCreateModel,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- benchModel ----
test("benchModel: no latest entry -> unavailable with the exact empty-state copy", () => {
  const expected = "No benchmark history — run an eval with --record-history.";
  for (const payload of [null, {}, { latest: null, delta: null, series: [], history: [] }]) {
    const m = benchModel(payload);
    assert.equal(m.available, false);
    assert.equal(m.emptyText, expected);
    assert.equal(m.latest, null);
    assert.deepEqual(m.bars, []);
    assert.deepEqual(m.history, []);
  }
});

test("benchModel: latest scorecard labels, sha chip and delta arrows", () => {
  const m = benchModel({
    latest: { ts: 1750000000, git_sha: "abcdef0123456789", suite: "core",
              pass_rate: 0.875, quality_mean: 82, quality_min: 64,
              cost_usd: 3.456, duration_s: 125, runs: 8 },
    delta: { pass_rate: 0.125, quality_mean: -3, quality_min: 0, cost_usd: 0.5 },
    series: [], history: [],
  });
  assert.equal(m.available, true);
  const l = m.latest;
  assert.equal(l.passLabel, "88%");            // 0.875 -> rounded percent
  assert.equal(l.qualityLabel, "82");
  assert.equal(l.qualityMinLabel, "64");
  assert.equal(l.costLabel, "$3.46");
  assert.equal(l.sha, "abcdef0");              // 7-char chip
  assert.equal(l.suite, "core");
  assert.equal(l.runsLabel, "8 runs");
  assert.equal(l.durationLabel, "2m 5s");
  assert.equal(l.deltas.pass.dir, "up");       // +0.125 -> +13 percentage points
  assert.equal(l.deltas.pass.label, "▲ +13");
  assert.equal(l.deltas.quality.dir, "down");
  assert.equal(l.deltas.qualityMin.dir, "flat");  // zero delta -> flat
  assert.equal(l.deltas.cost.dir, "up");
});

test("benchModel: null delta and missing fields degrade to em dashes / flat arrows", () => {
  const m = benchModel({ latest: { git_sha: "a1b2c3d4" }, delta: null });
  assert.equal(m.latest.passLabel, "—");
  assert.equal(m.latest.qualityCell, "—");
  assert.equal(m.latest.costLabel, "—");
  assert.equal(m.latest.deltas.pass.dir, "flat");
  assert.equal(m.latest.deltas.cost.dir, "flat");
});

test("benchModel: series bars clamp heights to 0-100 with a visibility floor", () => {
  const m = benchModel({
    latest: { pass_rate: 1 },
    series: [
      { sha: "aaaaaaa1111", ts: 1, pass_rate: 1.2, quality_mean: 250 },   // over -> clamp 100
      { sha: "bbbbbbb", ts: 2, pass_rate: 0, quality_mean: 0 },           // zero stays 0
      { sha: "ccccccc", ts: 3, pass_rate: 0.01, quality_mean: 2 },        // tiny -> 4% floor
      { sha: "ddddddd", ts: 4, pass_rate: 0.5, quality_mean: 72, cost_usd: 1.25 },
    ],
  });
  assert.deepEqual(m.bars.map(b => b.passH), [100, 0, 4, 50]);
  assert.deepEqual(m.bars.map(b => b.qualityH), [100, 0, 4, 72]);
  assert.equal(m.bars[0].sha, "aaaaaaa");                    // sha label is short
  assert.equal(m.bars[3].label, "ddddddd · 50% pass · quality 72 · $1.25");
});

test("benchModel: history rows read newest-first with numeric ts and labels", () => {
  const m = benchModel({
    latest: { pass_rate: 1 },
    history: [   // server order: newest-last -> the model reverses for the table
      { suite: "" },   // sparse legacy entry (oldest)
      { ts: 1750000000, git_sha: "abcdef9876", suite: "core",
        pass_rate: 0.5, quality_mean: 70, quality_min: 55, cost_usd: 2 },
    ],
  });
  assert.equal(m.history.length, 2);
  assert.equal(m.history[0].ts, 1750000000);
  assert.equal(m.history[0].sha, "abcdef9");
  assert.equal(m.history[0].passLabel, "50%");
  assert.equal(m.history[0].qualityCell, "70 / 55");
  assert.equal(m.history[0].costLabel, "$2.00");
  assert.equal(m.history[1].ts, null);
  assert.equal(m.history[1].passLabel, "—");
});

// ---- distillReportModel / distillResultModel ----
test("distillReportModel: merges/prunes/orphans map with counts and summary", () => {
  const m = distillReportModel({
    merges: [{ keep: "auth module", drop: "the auth module", reason: "near-duplicate label" }],
    prunes: [{ src: "t1", dst: "old.py", relation: "produced_file", weight: 1, age_days: 45.6 }],
    orphans: ["ghost-node", "island"],
  });
  assert.equal(m.empty, false);
  assert.equal(m.canApply, true);
  assert.equal(m.summary, "1 merge · 1 prune · 2 orphans");
  assert.equal(m.merges[0].label, "auth module ← the auth module");
  assert.equal(m.merges[0].reason, "near-duplicate label");
  assert.equal(m.prunes[0].edge, "t1 → old.py (produced_file)");
  assert.equal(m.prunes[0].detail, "w1 · 46d old");
  assert.deepEqual(m.orphans, ["ghost-node", "island"]);
});

test("distillReportModel: empty report -> 'Nothing to distill.'; small-graph reason wins", () => {
  const empty = distillReportModel({ merges: [], prunes: [], orphans: [] });
  assert.equal(empty.empty, true);
  assert.equal(empty.canApply, false);
  assert.equal(empty.emptyText, "Nothing to distill.");
  assert.equal(empty.summary, "");
  const small = distillReportModel({ reason: "graph too small to distill (12 nodes < 25)" });
  assert.equal(small.empty, true);
  assert.equal(small.reason, "graph too small to distill (12 nodes < 25)");
  assert.equal(small.emptyText, "graph too small to distill (12 nodes < 25)");
});

test("distillResultModel: applied counts fold into one result line", () => {
  const m = distillResultModel({ merged: 3, pruned: 2, orphans_removed: 1 });
  assert.equal(m.total, 6);
  assert.equal(m.text, "Distilled — merged 3 · pruned 2 · removed 1 orphan");
  const none = distillResultModel({});
  assert.equal(none.total, 0);
  assert.equal(none.text, "Distilled — merged 0 · pruned 0 · removed 0 orphans");
});

// ---- userChipModel ----
test("userChipModel: named user on an auth-gated server shows; 'local' and auth-off hide", () => {
  const named = userChipModel({ auth_required: true, authorized: true, user: "mayank" });
  assert.equal(named.visible, true);
  assert.equal(named.label, "◆ mayank");
  assert.equal(named.byLabel, "by mayank");
  assert.equal(userChipModel({ auth_required: true, user: "local" }).visible, false);
  assert.equal(userChipModel({ auth_required: false, user: "mayank" }).visible, false);
  assert.equal(userChipModel({ auth_required: true, user: "" }).visible, false);
  assert.equal(userChipModel(null).visible, false);
  // byLabel ignores auth_required — historical run payloads carry only `user`
  assert.equal(userChipModel({ user: "sam" }).byLabel, "by sam");
  assert.equal(userChipModel({ user: "local" }).byLabel, "");
});

// ---- usersListModel / userCreateModel ----
test("usersListModel: tolerates array, {users} wrapper and string entries", () => {
  const wrapped = usersListModel({ users: [{ name: "ana", created_at: 1750000000 }, "bob"] });
  assert.equal(wrapped.empty, false);
  assert.equal(wrapped.countLabel, "2 users");
  assert.deepEqual(wrapped.rows[0], { name: "ana", createdAt: 1750000000 });
  assert.deepEqual(wrapped.rows[1], { name: "bob", createdAt: null });
  const bare = usersListModel([{ name: "cy" }, { name: "" }]);   // nameless rows drop
  assert.equal(bare.rows.length, 1);
  assert.equal(bare.countLabel, "1 user");
  const none = usersListModel(null);
  assert.equal(none.empty, true);
  assert.deepEqual(none.rows, []);
});

test("userCreateModel: one-time token with shown-once warning; no token -> not ok", () => {
  const m = userCreateModel({ name: "ana", token: "tok_9f8e7d" });
  assert.equal(m.ok, true);
  assert.equal(m.name, "ana");
  assert.equal(m.token, "tok_9f8e7d");
  assert.equal(m.warning, "This token is shown once — copy it now.");
  const bad = userCreateModel({ name: "ana" });
  assert.equal(bad.ok, false);
  assert.equal(bad.token, "");
  assert.equal(bad.warning, "");
});
