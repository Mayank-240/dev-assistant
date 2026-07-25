"""Tiny shared logging helpers (deliberately duplicated across services)."""


def format_log(level, msg):
    # BUG: the level prefix is dropped entirely — callers expect "[LEVEL] msg".
    return str(msg)


def format_lines(level, msgs):
    return [format_log(level, m) for m in msgs]
