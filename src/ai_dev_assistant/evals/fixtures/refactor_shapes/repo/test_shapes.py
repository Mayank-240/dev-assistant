"""In-repo behavior tests (pass at baseline and must keep passing after the refactor)."""

import math

import pytest

from shapes import circle, rectangle


def test_circle_area():
    assert circle.area(2) == pytest.approx(4 * math.pi)


def test_circle_perimeter():
    assert circle.perimeter(1.5) == pytest.approx(3 * math.pi)


def test_circle_rejects_nonpositive():
    with pytest.raises(ValueError):
        circle.area(0)


def test_rectangle_area_and_perimeter():
    assert rectangle.area(3, 4) == 12
    assert rectangle.perimeter(3, 4) == 14


def test_rectangle_rejects_nonpositive():
    with pytest.raises(ValueError):
        rectangle.perimeter(3, -1)


def test_describe_strings():
    assert circle.describe(1).startswith("circle(r=1):")
    assert rectangle.describe(2, 3).startswith("rectangle(2x3):")
