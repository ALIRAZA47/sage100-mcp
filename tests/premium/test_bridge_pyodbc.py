"""The 32-bit helper's script, with the real pyodbc and a real driver (docs/decisions.md D67).

Sage's ProvideX driver needs Windows, but Microsoft's SQL Server driver runs on the test bed. So these
tests show that what pyodbc really returns crosses the helper unchanged: its value types, descriptions,
catalog rows (with pyodbc's lower-case names), errors, cancel and query timeout. Each runs the same calls
on pyodbc directly and through helper.py in an isolated child Python, then compares the two.

SAGE100_MCP_TEST_HELPER_PYTHON names another Python with pyodbc to run the helper under, such as a 32-bit
one on Windows, or the oldest supported, 3.9.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from sage100_mcp.backends.base import description_type
from sage100_mcp.bridge import SCRIPT
from sage100_mcp.bridge.client import Bridge
from tests import premium_testbed as testbed
from tests.bridge_fixture import helper_process

pyodbc = pytest.importorskip("pyodbc")
pytestmark = [pytest.mark.premium]

PYTHON = os.environ.get("SAGE100_MCP_TEST_HELPER_PYTHON") or sys.executable
VALUES = (
    "SELECT CAST(? AS decimal(13,2)) AS amount, CAST('2026-10-31' AS date) AS day, "
    "CAST('2026-10-31T12:34:56.123' AS datetime) AS stamp, CAST('23:59:58.5' AS time(1)) AS clock, "
    "CAST(1 AS bit) AS flag, N'Grüße, 東京' AS words, CAST(NULL AS varchar(10)) AS nothing, "
    "CAST(0x00FF AS varbinary(2)) AS raw, CAST(? AS int) AS whole, CAST(1.5 AS float) AS ratio, "
    "CAST(? AS bigint) AS big, CAST('12345678-1234-5678-1234-567812345678' AS uniqueidentifier) AS id, "
    "CAST(? AS varchar(20)) AS code"
)


@pytest.fixture
def bridge(server: str) -> Iterator[Bridge]:
    bridge = Bridge((PYTHON, "-I", str(SCRIPT)))
    yield bridge
    bridge.shutdown()


def text(server: str) -> str:
    return testbed.connection_string(server, "MAS_EMP", testbed.READER, testbed.READER_PASSWORD)


def both(server: str, bridge: Bridge) -> list[tuple[str, Any]]:
    """The same connection, opened directly and through the helper."""
    return [(route, odbc.connect(text(server), autocommit=True, timeout=15)) for route, odbc in
            (("direct", pyodbc), ("helper", bridge))]  # fmt: skip


def test_values_and_descriptions_match_pyodbc(server: str, bridge: Bridge) -> None:
    found = {}
    for route, connection in both(server, bridge):
        cursor = connection.cursor()
        cursor.execute(VALUES, Decimal("12.34"), 7, 2**40, "C0001")
        row = cursor.fetchone()
        found[route] = (tuple(row), [tuple(column) for column in cursor.description], row.amount, row.code)
        connection.close()
    assert found["helper"] == found["direct"]
    values, description, amount, code = found["helper"]
    assert [type(value).__name__ for value in values] == [
        "Decimal", "date", "datetime", "time", "bool", "str", "NoneType", "bytes", "int", "float", "int", "str", "str",
    ]  # fmt: skip
    assert [description_type(column) for column in description][:5] == [
        "decimal(13,2)", "date", "datetime", "time", "bit",
    ]  # fmt: skip
    assert (str(amount), code) == ("12.34", "C0001")


def test_catalog_rows_match_pyodbc(server: str, bridge: Bridge) -> None:
    found = {}
    for route, connection in both(server, bridge):
        cursor = connection.cursor()
        tables = [(row.table_name, row.table_type) for row in cursor.tables(table="AR_%").fetchall()]
        columns = [(row.column_name, row.type_name, row.column_size, row.ordinal_position)
                   for row in cursor.columns(table="AR_Customer").fetchall()]  # fmt: skip
        keys = [(row.column_name, row.key_seq) for row in cursor.primaryKeys("AR_Customer").fetchall()]
        found[route] = (tables, columns, keys, connection.getinfo(pyodbc.SQL_DRIVER_VER))
        connection.close()
    assert found["helper"] == found["direct"]
    tables, columns, keys, _ = found["helper"]
    assert ("AR_Customer", "TABLE") in tables
    assert columns[0][0] == "ARDivisionNo"
    assert keys == [("ARDivisionNo", 1), ("CustomerNo", 2)]


def test_errors_match_pyodbc(server: str, bridge: Bridge) -> None:
    found = {}
    for route, connection in both(server, bridge):
        try:
            connection.cursor().execute("SELECT Nope FROM AR_NoSuchTable")
        except Exception as exc:
            found[route] = (type(exc).__name__, exc.args)
        connection.close()
    assert found["helper"] == found["direct"]
    assert found["helper"][0] == "ProgrammingError"
    assert found["helper"][1][0] == "42S02"


def test_a_cancel_stops_a_running_statement(server: str, bridge: Bridge) -> None:
    connection = bridge.connect(text(server), autocommit=True, timeout=15)
    cursor = connection.cursor()
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            cursor.execute("WAITFOR DELAY '00:00:20'")
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    time.sleep(0.5)
    cursor.cancel()
    worker.join(10)
    assert time.monotonic() - started < 5
    assert outcome["error"].args[0] == "HY008"
    cursor.execute("SELECT 1 AS one")
    row = cursor.fetchone()
    assert row is not None
    assert row.one == 1
    connection.close()


def test_the_query_timeout_applies_to_new_cursors(server: str, bridge: Bridge) -> None:
    connection = bridge.connect(text(server), autocommit=True, timeout=15)
    connection.timeout = 1
    started = time.monotonic()
    with pytest.raises(bridge.OperationalError) as timed_out:
        connection.cursor().execute("WAITFOR DELAY '00:00:10'")
    assert timed_out.value.args[0] == "HYT00"
    assert time.monotonic() - started < 5
    connection.close()


def test_the_helper_says_what_it_runs_and_exits_with_its_input(server: str, bridge: Bridge) -> None:
    bridge.connect(text(server), autocommit=True, timeout=15).close()
    assert sys.executable != PYTHON or f"pyodbc {pyodbc.version}" in bridge.describe()
    process = helper_process(bridge)
    bridge.shutdown()
    assert process.wait(10) == 0
