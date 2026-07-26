// Unit tests for the QoL-wave pure models in web/static/util.js:
// memory curation rows + paging, workspace-GC summary/result, subtask
// rollback state, cron schedule forms/rows and the semantic palette badge.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  memoryRowModel, memoryPageModel, MEMORY_CLAMP_CHARS,
  gcSummary, gcResultModel, rollbackStateModel, reviewCardModel,
  scheduleFormModel, scheduleRowModel, CRON_HINT, paletteResultsModel,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- memoryRowModel ----
test("memoryRowModel maps a curation row and keeps short content unclamped", () => {
  const m = memoryRowModel({
    id: 7, scope: "longterm", key: "auth", content: "use bcrypt", metadata: {},
    created_at: 1_700_000_000,
  });
  assert.equal(m.id, 7);
  assert.equal(m.scope, "longterm");
  assert.equal(m.key, "auth");
  assert.equal(m.content, "use bcrypt");
  assert.equal(m.clamped, false);
  assert.equal(m.preview, "use bcrypt");
  assert.equal(m.createdAt, 1_700_000_000);
});

test("memoryRowModel clamps long content to an ellipsized preview", () => {
  const long = "x".repeat(MEMORY_CLAMP_CHARS + 50);
  const m = memoryRowModel({ id: 1, content: long });
  assert.equal(m.clamped, true);
  assert.equal(m.preview.length, MEMORY_CLAMP_CHARS + 1);   // clip + "…"
  assert.ok(m.preview.endsWith("…"));
  assert.equal(m.content, long);                            // full text kept for expand
});

test("memoryRowModel tolerates a junk row", () => {
  const m = memoryRowModel(null);
  assert.equal(m.id, null);
  assert.equal(m.scope, "");
  assert.equal(m.content, "");
  assert.equal(m.createdAt, null);
  assert.equal(memoryRowModel({ created_at: "nope" }).createdAt, null);
});

// ---- memoryPageModel ----
test("memoryPageModel drives the count line and Load-more paging", () => {
  const more = memoryPageModel(120, 50);
  assert.equal(more.countLabel, "120 memories");
  assert.equal(more.hasMore, true);
  assert.equal(more.nextOffset, 50);
  assert.equal(more.moreLabel, "Load more (70 remaining)");
  const done = memoryPageModel(50, 50);
  assert.equal(done.hasMore, false);
  assert.equal(done.moreLabel, "");
  assert.equal(memoryPageModel(1, 0).countLabel, "1 memory");
  assert.equal(memoryPageModel(null, null).countLabel, "0 memories");
});

// ---- gcSummary ----
test("gcSummary merges worktrees and branches into one labelled list", () => {
  const m = gcSummary({
    keep_days: 14,
    worktrees: [{ task_id: "t1", path: "/w/t1", status: "completed", age_days: 20.04 }],
    branches: [{ task_id: "t2", branch: "ada/t2", status: "failed", age_days: 30 }],
  });
  assert.equal(m.count, 2);
  assert.equal(m.label, "Clean up 2 items");
  assert.equal(m.keepDays, 14);
  assert.equal(m.emptyText, "");
  assert.deepEqual(m.items[0], {
    taskId: "t1", kind: "worktree", detail: "/w/t1",
    status: "completed", ageLabel: "20.0d old",
  });
  assert.deepEqual(m.items[1], {
    taskId: "t2", kind: "branch", detail: "ada/t2",
    status: "failed", ageLabel: "30.0d old",
  });
});

test("gcSummary: empty report yields the retention empty-state copy", () => {
  const m = gcSummary({ keep_days: 7, worktrees: [], branches: [] });
  assert.equal(m.count, 0);
  assert.equal(m.label, "");
  assert.equal(m.emptyText,
    "Nothing to clean — task workspaces are kept 7 days after completion.");
  const one = gcSummary({ keep_days: 1 });
  assert.ok(one.emptyText.includes("kept 1 day after"));
  assert.equal(gcSummary(null).count, 0);   // junk-tolerant
});

// ---- gcResultModel ----
test("gcResultModel summarizes removed/skipped cleanup results", () => {
  const m = gcResultModel({
    removed: { worktrees: [{ task_id: "t1" }], branches: [] },
    skipped: [{ task_id: "t9", reason: "not eligible" }],
  });
  assert.equal(m.removedCount, 1);
  assert.equal(m.text, "Removed 1 worktree · 0 branches · 1 skipped");
  assert.deepEqual(m.skipped, [{ taskId: "t9", reason: "not eligible" }]);
  const none = gcResultModel({});
  assert.equal(none.removedCount, 0);
  assert.equal(none.text, "Removed 0 worktrees · 0 branches");
});

// ---- rollbackStateModel + reviewCardModel rolled_back ----
test("rollbackStateModel: only accepted subtasks can roll back", () => {
  assert.deepEqual(rollbackStateModel({ decision: "accepted" }),
    { canRollback: true, rolledBack: false, label: "Roll back" });
  assert.deepEqual(rollbackStateModel({ decision: "rolled_back" }),
    { canRollback: false, rolledBack: true, label: "rolled back" });
  assert.equal(rollbackStateModel({ decision: "rejected" }).canRollback, false);
  assert.equal(rollbackStateModel({}).canRollback, false);
  assert.equal(rollbackStateModel(null).canRollback, false);
});

test("reviewCardModel recognizes the rolled_back decision", () => {
  const m = reviewCardModel({ id: "s1", merge_commit: "abc", decision: "rolled_back" });
  assert.equal(m.decision, "rolled_back");
  assert.equal(m.decisionLabel, "rolled back");
  assert.equal(m.canAccept, true);    // a rolled-back subtask may be re-accepted
  assert.equal(m.canReject, true);
  // existing decisions keep their labels
  assert.equal(reviewCardModel({ id: "s2", decision: "accepted" }).decisionLabel, "accepted");
  assert.equal(reviewCardModel({ id: "s3", decision: "bogus" }).decisionLabel, "");
});

// ---- scheduleFormModel cron mode ----
test("scheduleFormModel cron mode sends {cron} instead of {every_hours}", () => {
  const m = scheduleFormModel({ prompt: "nightly audit", mode: "cron", cron: "  0 9 * *   1-5 " });
  assert.equal(m.ok, true);
  assert.equal(m.mode, "cron");
  assert.deepEqual(m.body, {
    prompt: "nightly audit", title: null, cron: "0 9 * * 1-5", budget_usd: 0,
  });
  assert.ok(!("every_hours" in m.body));
});

test("scheduleFormModel cron mode rejects blank and non-5-field expressions", () => {
  const blank = scheduleFormModel({ prompt: "p", mode: "cron", cron: "  " });
  assert.equal(blank.ok, false);
  assert.ok(blank.errors[0].includes(CRON_HINT));
  const four = scheduleFormModel({ prompt: "p", mode: "cron", cron: "0 9 * *" });
  assert.equal(four.ok, false);
  assert.ok(four.errors[0].includes("5 fields"));
  // hours mode is untouched by the cron fields (regression guard)
  const hours = scheduleFormModel({ prompt: "p", every_hours: "2", cron: "junk" });
  assert.equal(hours.ok, true);
  assert.deepEqual(hours.body, { prompt: "p", title: null, every_hours: 2, budget_usd: 0 });
});

// ---- scheduleRowModel cron rows ----
test("scheduleRowModel renders cron rows with their expression", () => {
  const now = 1_000_000;
  const m = scheduleRowModel({ id: "s1", prompt: "p", enabled: 1,
                               cron: "0 9 * * 1-5", next_run_at: now + 3600 }, now);
  assert.equal(m.cron, "0 9 * * 1-5");
  assert.equal(m.everyLabel, "cron 0 9 * * 1-5");
  assert.equal(m.nextLabel, "next in 1h");
  // interval rows are unchanged
  assert.equal(scheduleRowModel({ id: "s2", prompt: "p", enabled: 1, every_hours: 24 }, now).everyLabel,
    "every 24h");
});

// ---- palette semantic badge ----
test("paletteResultsModel carries the semantic flag through to items", () => {
  const hits = [
    { kind: "kb", project: "alpha", title: "auth notes", semantic: true, ref: {} },
    { kind: "kb", project: "alpha", title: "readme", ref: {} },
  ];
  const m = paletteResultsModel("auth", hits, []);
  const kb = m.groups.find(g => g.kind === "kb").items;
  assert.equal(kb[0].semantic, true);
  assert.equal(kb[1].semantic, false);
});
