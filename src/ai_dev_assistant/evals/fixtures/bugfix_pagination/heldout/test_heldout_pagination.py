"""Held-out tests (E3): the agent NEVER sees this file — it lives outside the fixture
repo and is copied into the finished workspace by the ``heldout_tests_pass`` grader."""

import pytest

from pagination import page_items, total_pages


def test_total_pages_partial_pages():
    assert total_pages(10, 3) == 4
    assert total_pages(11, 5) == 3
    assert total_pages(7, 4) == 2


def test_total_pages_exact_and_tiny():
    assert total_pages(12, 4) == 3
    assert total_pages(1, 1) == 1
    assert total_pages(3, 100) == 1


def test_total_pages_zero_and_negative_items():
    assert total_pages(0, 5) == 0
    assert total_pages(-2, 5) == 0


def test_total_pages_invalid_page_size():
    with pytest.raises(ValueError):
        total_pages(5, 0)
    with pytest.raises(ValueError):
        total_pages(5, -1)


def test_page_items_unbroken():
    assert page_items(list(range(10)), 1, 4) == [0, 1, 2, 3]
    assert page_items(list(range(10)), 3, 4) == [8, 9]
    assert page_items([], 1, 3) == []
    with pytest.raises(ValueError):
        page_items([1], 0, 3)
