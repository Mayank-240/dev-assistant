// Held-out tests (E3): the agent NEVER sees this file — it lives outside the fixture
// repo and is merged into the finished workspace by the ``heldout_tests_pass`` grader,
// where `npm test` (node --test) picks it up alongside the in-repo tests.
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { clamp, inRange } = require("./clamp");

test("held-out: clamp below/above/inside", () => {
  assert.equal(clamp(-100, -10, 10), -10);
  assert.equal(clamp(100, -10, 10), 10);
  assert.equal(clamp(-10, -10, 10), -10);
  assert.equal(clamp(0.5, 0, 1), 0.5);
});

test("held-out: degenerate single-point range", () => {
  assert.equal(clamp(3, 5, 5), 5);
  assert.equal(clamp(9, 5, 5), 5);
});

test("held-out: inverted range still throws", () => {
  assert.throws(() => clamp(0, 1, -1), RangeError);
});

test("held-out: inRange unbroken", () => {
  assert.equal(inRange(5, 5, 5), true);
  assert.equal(inRange(4.999, 5, 5), false);
});
