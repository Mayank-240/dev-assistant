"""A tiny calculator module used by the golden-task eval suite."""

from __future__ import annotations


def add(a, b):
    return a + b


def subtract(a, b):
    # BUG: operands are reversed — subtract(5, 3) returns -2 instead of 2.
    return b - a


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
