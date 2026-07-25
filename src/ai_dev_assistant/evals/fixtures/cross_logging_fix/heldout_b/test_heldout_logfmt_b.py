"""Held-out tests for repo_b — never visible to the agent during the run (E3)."""

from alerts import alert
from logutil import format_log


def test_error_prefix():
    assert format_log("error", "boom") == "[ERROR] boom"


def test_alert_includes_level_prefix():
    assert alert("db connection lost") == "[ERROR] db connection lost"


def test_empty_message_keeps_prefix():
    assert format_log("info", "") == "[INFO] "
