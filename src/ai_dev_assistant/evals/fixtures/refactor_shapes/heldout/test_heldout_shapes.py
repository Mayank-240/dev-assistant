"""Held-out tests (E3): the agent never sees this file. Checks the refactor target
(shapes/common.py with validate_positive) exists AND public behavior is unchanged."""

import math

import pytest


def test_common_module_exposes_validate_positive():
    from shapes.common import validate_positive

    assert validate_positive("side", 2) == 2
    with pytest.raises(ValueError) as exc:
        validate_positive("radius", -3)
    assert "radius" in str(exc.value)


def test_circle_behavior_preserved():
    from shapes import circle

    assert circle.area(3) == pytest.approx(9 * math.pi)
    assert circle.perimeter(2) == pytest.approx(4 * math.pi)
    with pytest.raises(ValueError):
        circle.perimeter(0)
    assert circle.describe(1) == f"circle(r=1): area={math.pi:.2f}, perimeter={2 * math.pi:.2f}"


def test_rectangle_behavior_preserved():
    from shapes import rectangle

    assert rectangle.area(5, 6) == 30
    assert rectangle.perimeter(5, 6) == 22
    with pytest.raises(ValueError):
        rectangle.area(-1, 2)
    assert rectangle.describe(2, 3) == "rectangle(2x3): area=6.00, perimeter=10.00"
