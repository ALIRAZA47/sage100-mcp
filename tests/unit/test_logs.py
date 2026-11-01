"""No password or token in any log line (SPEC.md §9.4): the redaction and the handler filter."""

from __future__ import annotations

import io
import logging

import pytest

from sage100_mcp.driver_errors import redact_secrets
from sage100_mcp.logs import RedactSecrets, failure_text, guard

TOKEN = "s1mcp_" + "Q" * 43


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DRIVER={x};UID=sa;PWD=hunter2;DATABASE=MAS_EMP", "DRIVER={x};UID=sa;PWD=***;DATABASE=MAS_EMP"),
        ("PWD={pa;ss}}word};APP=x", "PWD=***;APP=x"),
        ("password = s3cret", "password=***"),
        (f"Authorization: Bearer {TOKEN}", "Authorization: Bearer ***"),
        ("Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln", "Bearer ***"),
        (f"token {TOKEN} revoked", "token s1mcp_*** revoked"),
        ("WHERE CustomerName = 'O''Brien'", "WHERE CustomerName = 'O''Brien'"),
    ],
)
def test_secrets_are_redacted_and_literals_kept(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected


def logger_writing_to(stream: io.StringIO) -> logging.Logger:
    logger = logging.getLogger(f"sage100_mcp.test_logs.{id(stream)}")
    logger.propagate = False
    logger.handlers = [logging.StreamHandler(stream)]
    guard(logger)
    guard(logger)  # once is enough; twice adds nothing
    assert len(logger.handlers[0].filters) == 1
    return logger


def test_the_filter_redacts_messages_arguments_and_tracebacks() -> None:
    stream = io.StringIO()
    logger = logger_writing_to(stream)
    logger.warning("connecting with %s", "DRIVER={x};PWD=hunter2")
    try:
        raise RuntimeError(f"failed: PWD=hunter2; header Bearer {TOKEN}")
    except RuntimeError:
        logger.exception("call failed")
    text = stream.getvalue()
    assert "hunter2" not in text
    assert TOKEN not in text
    assert "connecting with DRIVER={x};PWD=***" in text
    assert "RuntimeError: failed: PWD=***; header Bearer ***" in text


def test_failure_text_is_a_redacted_traceback() -> None:
    try:
        raise ValueError("PASSWORD=hunter2")
    except ValueError:
        text = failure_text()
    assert text.startswith("Traceback")
    assert text.endswith("ValueError: PASSWORD=***")


def test_a_filtered_record_still_formats_its_arguments() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d%% of %s", (50, "rows"), None)
    assert RedactSecrets().filter(record)
    assert record.getMessage() == "50% of rows"
