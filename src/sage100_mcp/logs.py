"""Logging without secrets (SPEC.md §9.4): passwords and tokens are redacted from every line written."""

from __future__ import annotations

import logging
import traceback

from sage100_mcp.driver_errors import redact_secrets


class RedactSecrets(logging.Filter):
    """A handler filter that redacts each record's message and traceback before any formatter sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = redact_secrets(message)
        if clean != message:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        return True


def guard(*loggers: logging.Logger) -> None:
    """Add the filter to every handler of these loggers (the root logger when none are named)."""
    for logger in loggers or (logging.getLogger(),):
        for handler in logger.handlers:
            if not any(isinstance(f, RedactSecrets) for f in handler.filters):
                handler.addFilter(RedactSecrets())


def failure_text() -> str:
    """The exception being handled, as a traceback with passwords and tokens removed."""
    return redact_secrets(traceback.format_exc()).rstrip()
