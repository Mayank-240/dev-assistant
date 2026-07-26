// Unit tests for the mega-wave final-UI models in web/static/util.js:
// urlBase64ToUint8Array, pushStatusModel, pushTestResultLine, pushHintModel,
// maintenancePolicyModel, maintenancePayload, backupsModel, fmtBytes, plus
// spend_alert rendering in notifCenterModel and the review-card objective note.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  urlBase64ToUint8Array, pushStatusModel, pushTestResultLine, pushHintModel,
  MAINTENANCE_TASKS, maintenancePolicyModel, maintenancePayload,
  fmtBytes, backupsModel, notifCenterModel, reviewCardModel,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- urlBase64ToUint8Array ----

test("urlBase64ToUint8Array decodes a known vector incl. url-safe chars", () => {
  assert.deepEqual([...urlBase64ToUint8Array("AQID")], [1, 2, 3]);       // unpadded 3 bytes
  assert.deepEqual([...urlBase64ToUint8Array("-_8")], [251, 255]);       // '-'/'_' url-safe alphabet
  assert.equal(urlBase64ToUint8Array("").length, 0);
  const key = urlBase64ToUint8Array("BM_v0aLpsQ7C3Yr9nZ5t-w");           // vapid-ish, unpadded
  assert.ok(key instanceof Uint8Array && key.length === 16);
});

// ---- pushStatusModel ----

test("pushStatusModel: unavailable surfaces the server reason", () => {
  const m = pushStatusModel({ available: false, subscriptions: 0, reason: "pywebpush not installed" });
  assert.equal(m.available, false);
  assert.equal(m.reason, "pywebpush not installed");
  assert.equal(m.canTest, false);
});

test("pushStatusModel: available vs subscribed-on-this-device variants", () => {
  const avail = pushStatusModel({ available: true, subscriptions: 2, public_key: "PK" }, false);
  assert.deepEqual(
    [avail.available, avail.subscribed, avail.countLabel, avail.canTest, avail.buttonLabel, avail.publicKey],
    [true, false, "2 subscribed devices", true, "Enable on this device", "PK"]);
  const sub = pushStatusModel({ available: true, subscriptions: 1, public_key: "PK" }, true);
  assert.deepEqual([sub.subscribed, sub.countLabel, sub.buttonLabel],
    [true, "1 subscribed device", "Disable on this device"]);
  // available but nobody subscribed anywhere: test button is pointless
  assert.equal(pushStatusModel({ available: true, subscriptions: 0 }).canTest, false);
  assert.equal(pushTestResultLine({ sent: 2, failed: 1, gone: 0 }), "sent 2 · failed 1 · gone 0");
});

// ---- pushHintModel (home attention-card hint visibility) ----

test("pushHintModel shows only for undecided+available+unsubscribed+undismissed", () => {
  const base = { permission: "default", available: true, subscribed: false, dismissed: false };
  assert.equal(pushHintModel(base).show, true);
  assert.match(pushHintModel(base).text, /enable push in Settings/);
  assert.equal(pushHintModel({ ...base, permission: "granted" }).show, false);
  assert.equal(pushHintModel({ ...base, permission: "denied" }).show, false);
  assert.equal(pushHintModel({ ...base, available: false }).show, false);
  assert.equal(pushHintModel({ ...base, subscribed: true }).show, false);
  assert.equal(pushHintModel({ ...base, dismissed: true }).show, false);
  assert.equal(pushHintModel().show, false);
});

// ---- maintenancePolicyModel ----

test("maintenancePolicyModel maps the server policy onto the form", () => {
  const m = maintenancePolicyModel({
    enabled: true, cadence: "0 6 * * 1", budget_usd: 2.5,
    tasks: ["security-audit", "dead-code"], last_run_at: 1000,
  }, 4600);
  assert.equal(m.enabled, true);
  assert.equal(m.cadence, "0 6 * * 1");
  assert.equal(m.budgetValue, "2.5");
  assert.equal(m.tasks.length, MAINTENANCE_TASKS.length);
  assert.deepEqual(m.tasks.filter(t => t.checked).map(t => t.id), ["security-audit", "dead-code"]);
  assert.ok(m.tasks.every(t => t.label && t.desc));       // one-line descriptions present
  assert.match(m.lastRunLabel, /^Last maintenance run started 1h ago/);
  const dflt = maintenancePolicyModel({});
  assert.deepEqual([dflt.enabled, dflt.cadence, dflt.budgetValue, dflt.lastRunLabel],
    [false, "", "", "No maintenance run yet."]);
});

test("maintenancePolicyModel numeric cadence renders as its hours string", () => {
  assert.equal(maintenancePolicyModel({ cadence: 36 }).cadence, "36");
});

// ---- maintenancePayload (cron-vs-hours parsing + round trip) ----

test("maintenancePayload round-trips both cadence spellings", () => {
  const cron = maintenancePayload({
    enabled: true, cadence: maintenancePolicyModel({ cadence: "0 6 * * 1" }).cadence,
    budget: "2.5", tasks: ["dead-code"],
  });
  assert.equal(cron.ok, true);
  assert.deepEqual(cron.payload,
    { enabled: true, cadence: "0 6 * * 1", budget_usd: 2.5, tasks: ["dead-code"] });
  const hours = maintenancePayload({ enabled: true, cadence: "36", budget: "", tasks: ["doc-drift"] });
  assert.equal(hours.ok, true);
  assert.equal(hours.payload.cadence, 36);                // number, not string
  assert.equal(hours.payload.budget_usd, 0);
  const spaced = maintenancePayload({ cadence: "  0  6 * * 1 " });
  assert.equal(spaced.payload.cadence, "0 6 * * 1");      // whitespace-normalized
});

test("maintenancePayload rejects bad forms with inline messages", () => {
  const noCadence = maintenancePayload({ enabled: true, cadence: "", tasks: ["dead-code"] });
  assert.equal(noCadence.ok, false);
  assert.match(noCadence.errors[0], /needs a cadence/);
  const badCron = maintenancePayload({ cadence: "0 6 * *" });        // 4 fields
  assert.match(badCron.errors[0], /cadence must be/);
  const zeroHours = maintenancePayload({ cadence: "0" });
  assert.match(zeroHours.errors[0], /hours must be > 0/);
  const noTasks = maintenancePayload({ enabled: true, cadence: "24", tasks: [] });
  assert.match(noTasks.errors[0], /at least one/);
  const badBudget = maintenancePayload({ cadence: "24", budget: "lots" });
  assert.match(badBudget.errors[0], /budget must be a number/);
  // disabled + blank cadence is a valid "turned off" save
  assert.equal(maintenancePayload({ enabled: false, cadence: "", tasks: [] }).ok, true);
});

// ---- backupsModel ----

test("fmtBytes humanizes across B/KB/MB/GB", () => {
  assert.equal(fmtBytes(512), "512 B");
  assert.equal(fmtBytes(2048), "2.0 KB");
  assert.equal(fmtBytes(5 * 1024 * 1024), "5.0 MB");
  assert.equal(fmtBytes(3 * 1024 * 1024 * 1024), "3.00 GB");
});

test("backupsModel: newest first, humanized sizes, download hrefs", () => {
  const m = backupsModel([
    { path: "/d/backups/ada-backup-20260101T000000Z.tar.gz", size: 1024, created: "2026-01-01T00:00:00+00:00" },
    { path: "/d/backups/ada-backup-20260715T060000Z.tar.gz", size: 3 * 1024 * 1024, created: "2026-07-15T06:00:00+00:00" },
  ]);
  assert.equal(m.empty, false);
  assert.deepEqual(m.rows.map(r => r.name),
    ["ada-backup-20260715T060000Z.tar.gz", "ada-backup-20260101T000000Z.tar.gz"]);
  assert.deepEqual(m.rows.map(r => r.sizeLabel), ["3.0 MB", "1.0 KB"]);
  assert.equal(m.rows[0].dateLabel, "2026-07-15 06:00 UTC");
  assert.equal(m.rows[0].href,
    "/api/backup/download?path=" + encodeURIComponent("/d/backups/ada-backup-20260715T060000Z.tar.gz"));
  assert.equal(backupsModel([]).empty, true);
  assert.equal(backupsModel(null).empty, true);
});

// ---- notifCenterModel: spend_alert kind ----

test("notifCenterModel renders spend_alert with a warning prefix", () => {
  const m = notifCenterModel([
    { id: "n1", ts: 10, kind: "spend_alert", text: "80% of monthly cap", read: false },
  ], 100);
  assert.equal(m.rows[0].icon, "⚠");
  assert.equal(m.rows[0].cls, "nf-spend_alert");
  assert.equal(m.badge, "1");
});

// ---- reviewCardModel: objective gate note ----

test("reviewCardModel surfaces the verdict's static-check objective note", () => {
  const m = reviewCardModel({
    id: "s1",
    verdict: { passed: false, score: 40, objective_note: "static checks: ruff +3 vs baseline" },
  });
  assert.equal(m.objectiveNote, "static checks: ruff +3 vs baseline");
  assert.equal(reviewCardModel({ id: "s2", verdict: {} }).objectiveNote, "");
});
