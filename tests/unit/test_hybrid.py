"""Hybrid execution on DuckDB (SPEC.md §5.5 step 4, §13.5): exact decimals, caps, timeouts, errors."""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from sage100_mcp.backends.base import RawResult, ResultColumn
from sage100_mcp.catalog.model import Column
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.sql import hybrid
from sage100_mcp.sql.planner import Plan, Pull, plan
from tests.providex_fixture import catalog, parsed, profile

Rows = dict[str, list[dict[str, Any]]]


def reader(tables: Rows, *, truncated: bool = False, coerced: bool = False, delay: float = 0.0) -> hybrid.Reader:
    """Serves whole tables, ignoring the pushed filters: DuckDB applies every condition again anyway."""

    def read(pull: Pull, cap: int, seconds_left: float) -> RawResult:
        time.sleep(delay)
        rows = [tuple(row.get(c.name) for c in pull.columns) for row in tables.get(pull.table.name, [])]
        columns = tuple(ResultColumn(c.name, c.type) for c in pull.columns)
        return RawResult(columns, rows[:cap], truncated, 1, null_dates_coerced=coerced)

    return read


def run(sql: str, tables: Rows, params: list[Any] | None = None, *, row_cap: int = 100, **options: Any) -> Any:
    tree, values = parsed(sql, params or [])
    planned = plan(tree, catalog(), profile("calvin_graham"), values, mode="hybrid", row_cap=row_cap + 1)
    read = options.pop("read", None) or reader(tables)
    defaults: dict[str, Any] = {"max_pushdown_rows": 1000, "row_cap": row_cap, "timeout_s": 10.0}
    return hybrid.run(planned, read, **{**defaults, **options})


INVOICES = [
    {"ARDivisionNo": "00", "CustomerNo": f"C{i % 3}", "InvoiceNo": f"{i:07d}", "Balance": Decimal("0.10")}
    for i in range(1000)
]


def test_sums_stay_exact() -> None:
    result = run("SELECT SUM(Balance) FROM AR_OpenInvoice", {"AR_OpenInvoice": INVOICES})
    assert result.raw.rows == [(Decimal("100.00"),)]
    assert result.pulled == {"AR_OpenInvoice": 1000}


def test_grouping_and_params_run_locally() -> None:
    result = run(
        "SELECT CustomerNo, COUNT(*) AS n, SUM(Balance) AS total FROM AR_OpenInvoice "
        "WHERE CustomerNo <> ? GROUP BY CustomerNo ORDER BY CustomerNo",
        {"AR_OpenInvoice": INVOICES},
        ["C2"],
    )
    assert result.raw.rows == [("C0", 334, Decimal("33.40")), ("C1", 333, Decimal("33.30"))]
    assert [c.name for c in result.raw.columns] == ["CustomerNo", "n", "total"]


def test_trailing_spaces_do_not_break_joins() -> None:
    tables: Rows = {
        "AR_Customer": [{"ARDivisionNo": "00", "CustomerNo": "C0   ", "CustomerName": "Blue Supply   "}],
        "AR_OpenInvoice": INVOICES[:3],
    }
    result = run(
        "SELECT c.CustomerName, i.InvoiceNo FROM AR_Customer c JOIN AR_OpenInvoice i "
        "ON i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo",
        tables,
    )
    assert result.raw.rows == [("Blue Supply", "0000000")]


def test_dates_load_as_dates() -> None:
    tables: Rows = {
        "AR_OpenInvoice": [
            {"InvoiceNo": "1", "InvoiceDate": datetime(2024, 12, 1)},
            {"InvoiceNo": "2", "InvoiceDate": date(2024, 11, 1)},
            {"InvoiceNo": "3", "InvoiceDate": None},
        ]
    }
    result = run(
        "SELECT InvoiceNo, DATEDIFF(day, InvoiceDate, '2024-12-31') FROM AR_OpenInvoice "
        "WHERE InvoiceDate >= '2024-11-15' ORDER BY InvoiceNo",
        tables,
    )
    assert result.raw.rows == [("1", 30)]


def test_the_row_cap_marks_truncation() -> None:
    result = run("SELECT InvoiceNo FROM AR_OpenInvoice", {"AR_OpenInvoice": INVOICES}, row_cap=10)
    assert len(result.raw.rows) == 10
    assert result.raw.truncated


def test_a_pull_that_reaches_the_cap_fails_with_the_filter_to_add() -> None:
    with pytest.raises(SageError) as caught:
        run(
            "SELECT COUNT(DISTINCT CustomerNo) FROM AR_OpenInvoice",
            {},
            read=reader({"AR_OpenInvoice": INVOICES}, truncated=True),
            max_pushdown_rows=500,
        )
    assert caught.value.code == ErrorCode.PUSHDOWN_LIMIT
    assert "AR_OpenInvoice" in caught.value.message
    assert "500" in caught.value.message
    assert "ARDivisionNo, CustomerNo, InvoiceNo, InvoiceType" in (caught.value.hint or "")


def test_the_reader_is_asked_for_the_pushdown_cap() -> None:
    asked: list[int] = []

    def read(pull: Pull, cap: int, seconds_left: float) -> RawResult:
        asked.append(cap)
        return RawResult(tuple(ResultColumn(c.name, c.type) for c in pull.columns), [], False, 0)

    run("SELECT COUNT(*) FROM AR_OpenInvoice", {}, read=read, max_pushdown_rows=1234)
    assert asked == [1234]


def test_coerced_dates_are_reported() -> None:
    result = run("SELECT COUNT(*) FROM AR_OpenInvoice", {}, read=reader({"AR_OpenInvoice": INVOICES}, coerced=True))
    assert result.null_dates_coerced


def test_slow_pulls_use_up_the_timeout() -> None:
    tables: Rows = {"AR_Customer": [{"CustomerNo": "C0"}], "AR_OpenInvoice": INVOICES}
    with pytest.raises(SageError) as caught:
        run(
            "SELECT c.CustomerNo FROM AR_Customer c JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo",
            {},
            read=reader(tables, delay=0.3),
            timeout_s=0.2,
        )
    assert caught.value.code == ErrorCode.QUERY_TIMEOUT


def test_a_slow_local_query_is_interrupted() -> None:
    local = sqlglot.parse_one("SELECT SUM(a.range * b.range) FROM range(1000000) a, range(1000000) b", read="duckdb")
    assert isinstance(local, exp.Query)
    started = time.monotonic()
    with pytest.raises(SageError) as caught:
        hybrid.run(Plan("hybrid", local=local), reader({}), max_pushdown_rows=10, row_cap=10, timeout_s=0.5)
    assert caught.value.code == ErrorCode.QUERY_TIMEOUT
    assert time.monotonic() - started < 5


def test_engine_failures_are_internal_errors_with_detail() -> None:
    local = sqlglot.parse_one("SELECT x FROM no_such_table", read="duckdb")
    assert isinstance(local, exp.Query)
    with pytest.raises(SageError) as caught:
        hybrid.run(Plan("hybrid", local=local), reader({}), max_pushdown_rows=10, row_cap=10, timeout_s=5)
    assert caught.value.code == ErrorCode.INTERNAL
    assert "no_such_table" in (caught.value.detail or "")


@pytest.mark.parametrize(
    ("sql_type", "duck"),
    [
        ("decimal(13,2)", "DECIMAL(13,2)"),
        ("decimal(40,4)", "DECIMAL(38,4)"),
        ("varchar(30)", "VARCHAR"),
        ("date", "DATE"),
        ("datetime", "TIMESTAMP"),
        ("int", "BIGINT"),
        ("float", "DOUBLE"),
        ("bit", "BOOLEAN"),
        ("something", "VARCHAR"),
    ],
)
def test_duckdb_types(sql_type: str, duck: str) -> None:
    assert hybrid.duckdb_type(Column("x", sql_type, True, "text")) == duck
