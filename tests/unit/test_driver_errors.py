"""Driver errors to stable codes (SPEC.md §14.3): one real message per rule, and redaction."""

from __future__ import annotations

from typing import Any

import pytest

from sage100_mcp.config import parse_yaml
from sage100_mcp.driver_errors import map_driver_error, redact, rules
from sage100_mcp.errors import ErrorCode
from tests.paths import REPO

BACKENDS = ("sqlserver", "providex")
FIXTURES: dict[str, list[dict[str, Any]]] = {
    backend: parse_yaml((REPO / f"tests/fixtures/errors/{backend}.yaml").read_text(encoding="utf-8"))
    for backend in BACKENDS
}
CASES = [(backend, case) for backend in BACKENDS for case in FIXTURES[backend]]


class DriverError(Exception):
    """Shaped like pyodbc.Error: args are (sqlstate, message)."""


@pytest.mark.parametrize(("backend", "case"), CASES, ids=[f"{b}-{c['code']}-{c['sqlstate']}" for b, c in CASES])
def test_each_fixture_maps_to_its_code(backend: str, case: dict[str, Any]) -> None:
    error = map_driver_error(
        DriverError(case["sqlstate"], case["message"]), backend=backend, phase=case["phase"], company="EMP"
    )
    assert error.code == ErrorCode(case["code"])
    assert error.company == "EMP"
    assert error.detail
    assert not error.message.startswith("[")  # the vendor prefixes are trimmed from the message


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_rule_has_a_fixture(backend: str) -> None:
    covered = {case["code"] for case in FIXTURES[backend]}
    assert {rule.code.value for rule in rules(backend)} <= covered


def test_providex_fixtures_cite_a_source() -> None:
    assert all(case.get("source") for case in FIXTURES["providex"])


def test_providex_parser_errors_keep_the_token() -> None:
    """The token the driver choked on is the model's best clue, so it stays in the message."""
    error = map_driver_error(
        DriverError("37000", "[37000] [ProvideX][ODBC Driver]Unexpected extra token: INNER (1017) (SQLExecDirect)"),
        backend="providex",
        phase="execute",
    )
    assert error.code == ErrorCode.DIALECT_UNSUPPORTED
    assert "INNER" in (error.detail or "")


def test_timeouts_depend_on_the_phase() -> None:
    timeout = DriverError("HYT00", "[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Query timeout expired (0)")
    assert map_driver_error(timeout, backend="sqlserver", phase="execute").code == ErrorCode.QUERY_TIMEOUT
    assert map_driver_error(timeout, backend="sqlserver", phase="connect").code == ErrorCode.BACKEND_UNREACHABLE


def test_unmapped_errors_are_internal_and_redacted() -> None:
    message = (
        "[22018] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Conversion failed when converting the "
        "varchar value 'C0001' to data type int. (245) (SQLExecDirectW)"
    )
    error = map_driver_error(DriverError("22018", message), backend="sqlserver", phase="execute")
    assert error.code == ErrorCode.INTERNAL
    assert error.detail is not None
    assert "C0001" not in error.detail  # a data value, so it is redacted
    assert error.detail.startswith("[22018]")


def test_object_names_survive_redaction() -> None:
    case = next(c for c in FIXTURES["sqlserver"] if c["code"] == "UNKNOWN_TABLE")
    error = map_driver_error(DriverError(case["sqlstate"], case["message"]), backend="sqlserver", phase="execute")
    assert error.message == "Invalid object name 'AR_Nope'. (208) (SQLExecDirectW)"
    assert "refresh_catalog" in (error.hint or "")


def test_login_names_and_passwords_are_redacted() -> None:
    case = next(c for c in FIXTURES["sqlserver"] if c["code"] == "AUTH_FAILED")
    error = map_driver_error(DriverError(case["sqlstate"], case["message"]), backend="sqlserver", phase="connect")
    assert "sage100_mcp_test" not in (error.detail or "")
    text = "Connect failed: DRIVER={x};PWD={se;cr}}et};UID=u and Password=hunter2; then hunter2 again"
    redacted = redact(text, ("hunter2",))
    assert "se;cr" not in redacted
    assert "hunter2" not in redacted
    assert "PWD=***" in redacted


def test_non_driver_exceptions() -> None:
    error = map_driver_error(RuntimeError("boom"), backend="sqlserver", phase="execute")
    assert (error.code, error.detail) == (ErrorCode.INTERNAL, "boom")
