"""Held-out tests (E3): the agent NEVER sees this file — it lives outside the fixture
repo and is copied into the finished workspace by the ``heldout_tests_pass`` grader."""

import pytest

from calculator import add, divide, multiply, subtract


def test_subtract_positive():
    assert subtract(10, 4) == 6


def test_subtract_negative_result():
    assert subtract(3, 8) == -5


def test_subtract_with_negatives():
    assert subtract(-2, -7) == 5


def test_subtract_zero_identity():
    assert subtract(9, 0) == 9


def test_add_and_multiply_unbroken():
    assert add(-1, 1) == 0
    assert multiply(-3, 3) == -9


def test_divide_unbroken():
    assert divide(9, 3) == 3
    with pytest.raises(ValueError):
        divide(5, 0)
