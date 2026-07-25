"""In-repo tests. These FAIL at baseline (total_pages has an off-by-one bug); the agent
must fix pagination.py — not these tests — to make them pass."""

import pytest

from pagination import page_items, total_pages


def test_total_pages_exact_multiple():
    assert total_pages(9, 3) == 3


def test_total_pages_partial_last_page():
    assert total_pages(10, 3) == 4


def test_total_pages_single_short_page():
    assert total_pages(1, 10) == 1


def test_total_pages_empty():
    assert total_pages(0, 5) == 0


def test_total_pages_rejects_bad_page_size():
    with pytest.raises(ValueError):
        total_pages(10, 0)


def test_page_items_basic():
    assert page_items(list(range(10)), 2, 3) == [3, 4, 5]


def test_page_items_out_of_range():
    assert page_items([1, 2, 3], 5, 2) == []
