"""Service B: alerting built on the shared logging helper."""

from logutil import format_log


def alert(msg):
    return format_log("error", msg)
