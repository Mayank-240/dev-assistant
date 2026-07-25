// Unit tests for the pure browser helpers in web/static/util.js.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  escapeHtml, escapeAttr, fmtTok, fmtSize, fmtCost, fmtDuration, classifyDiffLine,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- escapeHtml: the XSS boundary for every interpolation in app.js ----
test("escapeHtml neutralizes script tags", () => {
  assert.equal(
    escapeHtml('<script>alert("xss")</script>'),
    "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;",
  );
});

test("escapeHtml escapes all five special characters", () => {
  assert.equal(escapeHtml(`&<>"'`), "&amp;&lt;&gt;&quot;&#39;");
});

test("escapeHtml neutralizes attribute-breakout payloads", () => {
  const out = escapeHtml(`" onmouseover="alert(1)`);
  assert.ok(!out.includes('"'));
  assert.equal(out, "&quot; onmouseover=&quot;alert(1)");
});

test("escapeHtml double-escapes already-escaped input (no unescape round-trip)", () => {
  assert.equal(escapeHtml("&amp;"), "&amp;amp;");
});

test("escapeHtml coerces non-strings", () => {
  assert.equal(escapeHtml(42), "42");
  assert.equal(escapeHtml(null), "null");
  assert.equal(escapeHtml(undefined), "undefined");
});

test("escapeHtml leaves plain text unchanged", () => {
  assert.equal(escapeHtml("plain text 123"), "plain text 123");
});

// ---- escapeAttr: used for value="" interpolations ----
test("escapeAttr escapes quotes, ampersands and angle brackets", () => {
  assert.equal(escapeAttr(`a"b&c<d`), "a&quot;b&amp;c&lt;d");
  assert.equal(escapeAttr(123), "123");
});

// ---- fmtTok ----
test("fmtTok formats token counts", () => {
  assert.equal(fmtTok(0), "0");
  assert.equal(fmtTok(999), "999");
  assert.equal(fmtTok(1000), "1.0k");
  assert.equal(fmtTok(1550), "1.6k");
  assert.equal(fmtTok(undefined), "0");
  assert.equal(fmtTok(null), "0");
});

// ---- fmtSize ----
test("fmtSize formats byte counts", () => {
  assert.equal(fmtSize(0), "0 B");
  assert.equal(fmtSize(1023), "1023 B");
  assert.equal(fmtSize(1024), "1.0 KB");
  assert.equal(fmtSize(2560), "2.5 KB");
});

// ---- fmtCost ----
test("fmtCost formats USD with four decimals", () => {
  assert.equal(fmtCost(1.23456), "$1.2346");
  assert.equal(fmtCost(0), "$0.0000");
  assert.equal(fmtCost(null), "$0.0000");
  assert.equal(fmtCost(undefined), "$0.0000");
});

// ---- fmtDuration ----
test("fmtDuration formats seconds and minutes", () => {
  assert.equal(fmtDuration(0), "0.0s");
  assert.equal(fmtDuration(5.25), "5.3s");
  assert.equal(fmtDuration(59.94), "59.9s");
  assert.equal(fmtDuration(60), "1m 0s");
  assert.equal(fmtDuration(125.7), "2m 5s");
});

test("fmtDuration handles missing/invalid values", () => {
  assert.equal(fmtDuration(null), "—");
  assert.equal(fmtDuration(undefined), "—");
  assert.equal(fmtDuration(NaN), "—");
  assert.equal(fmtDuration(Infinity), "—");
  assert.equal(fmtDuration(-1), "—");
});

// ---- classifyDiffLine ----
test("classifyDiffLine recognizes file headers before +/-", () => {
  assert.equal(classifyDiffLine("diff --git a/x.py b/x.py"), "file");
  assert.equal(classifyDiffLine("--- a/x.py"), "file");   // not "del"
  assert.equal(classifyDiffLine("+++ b/x.py"), "file");   // not "add"
});

test("classifyDiffLine recognizes hunks, additions, deletions and context", () => {
  assert.equal(classifyDiffLine("@@ -1,4 +1,6 @@"), "hunk");
  assert.equal(classifyDiffLine("+new line"), "add");
  assert.equal(classifyDiffLine("-old line"), "del");
  assert.equal(classifyDiffLine(" unchanged"), "ctx");
  assert.equal(classifyDiffLine(""), "ctx");
  assert.equal(classifyDiffLine("plain"), "ctx");
});
