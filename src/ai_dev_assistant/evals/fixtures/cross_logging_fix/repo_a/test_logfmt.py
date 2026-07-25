from logutil import format_log
from metrics import record_metric


def test_level_prefix_present():
    assert format_log("info", "server started") == "[INFO] server started"


def test_level_is_uppercased():
    assert format_log("warning", "disk almost full") == "[WARNING] disk almost full"


def test_metric_line_carries_info_level():
    assert record_metric("rps", 42) == "[INFO] metric rps=42"
