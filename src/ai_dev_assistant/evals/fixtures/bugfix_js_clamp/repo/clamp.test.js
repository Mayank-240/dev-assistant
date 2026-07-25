// In-repo tests (node --test). These FAIL at baseline (clamp has swapped bounds);
// the agent must fix clamp.js — not these tests — to make `npm test` pass.
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { clamp, inRange } = require("./clamp");

test("clamp pins values below the range to min", () => {
  assert.equal(clamp(-5, 0, 10), 0);
});

test("clamp pins values above the range to max", () => {
  assert.equal(clamp(99, 0, 10), 10);
});

test("clamp leaves in-range values alone", () => {
  assert.equal(clamp(7, 0, 10), 7);
  assert.equal(clamp(0, 0, 10), 0);
  assert.equal(clamp(10, 0, 10), 10);
});

test("clamp rejects an inverted range", () => {
  assert.throws(() => clamp(1, 5, 2), RangeError);
});

test("inRange basics", () => {
  assert.equal(inRange(3, 1, 5), true);
  assert.equal(inRange(9, 1, 5), false);
});
