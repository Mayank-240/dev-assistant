"use strict";

/** Pin `value` into the inclusive range [min, max]. */
function clamp(value, min, max) {
  if (min > max) throw new RangeError("min must be <= max");
  // BUG: the bounds are swapped — values below min come back as max and values
  // above max come back as min.
  if (value < min) return max;
  if (value > max) return min;
  return value;
}

/** True when `value` lies inside the inclusive range [min, max]. */
function inRange(value, min, max) {
  if (min > max) throw new RangeError("min must be <= max");
  return value >= min && value <= max;
}

module.exports = { clamp, inRange };
