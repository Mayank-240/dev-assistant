"""Rectangle geometry. NOTE: _validate_positive is duplicated in circle.py."""

from __future__ import annotations


def _validate_positive(name, value):
    # Duplicated verbatim in circle.py — a refactor should hoist this.
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def area(width, height):
    _validate_positive("width", width)
    _validate_positive("height", height)
    return width * height


def perimeter(width, height):
    _validate_positive("width", width)
    _validate_positive("height", height)
    return 2 * (width + height)


def describe(width, height):
    return (f"rectangle({width}x{height}): area={area(width, height):.2f}, "
            f"perimeter={perimeter(width, height):.2f}")
