from alerts import alert
from logutil import format_log


def test_level_prefix_present():
    assert format_log("debug", "cache warmed") == "[DEBUG] cache warmed"


def test_alert_uses_error_level():
    assert alert("queue backlog") == "[ERROR] queue backlog"
