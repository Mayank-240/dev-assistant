"""Tiny pagination helpers used by the golden-task eval suite."""

from __future__ import annotations


def total_pages(total_items, page_size):
    """Number of pages needed to show ``total_items`` at ``page_size`` per page."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if total_items <= 0:
        return 0
    # BUG: off-by-one — floor division drops the final partial page, so e.g.
    # total_pages(10, 3) returns 3 instead of 4.
    return total_items // page_size


def page_items(items, page, page_size):
    """The 1-based ``page`` of ``items`` (empty list when the page is out of range)."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if page < 1:
        raise ValueError("page numbers start at 1")
    start = (page - 1) * page_size
    return list(items[start:start + page_size])
