"""Circle geometry. NOTE: _validate_positive is duplicated in rectangle.py."""

from __future__ import annotations

import math


def _validate_positive(name, value):
    # Duplicated verbatim in rectangle.py — a refactor should hoist this.
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def area(radius):
    _validate_positive("radius", radius)
    return math.pi * radius ** 2


def perimeter(radius):
    _validate_positive("radius", radius)
    return 2 * math.pi * radius


def describe(radius):
    return f"circle(r={radius}): area={area(radius):.2f}, perimeter={perimeter(radius):.2f}"
