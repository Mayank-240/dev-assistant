"""Service A: metrics reporting built on the shared logging helper."""

from logutil import format_log


def record_metric(name, value):
    return format_log("info", f"metric {name}={value}")
