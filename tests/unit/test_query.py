"""run_query on Premium (SPEC.md §5.5, §11.2) against the fake backend: the SQL sent and the result shape."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.query import QueryResult, result_text, run_query
from sage100_mcp.runtime import Runtime
from sage100_mcp.sql.bind import bind, bind_value
from tests.fake_backend import FakeExecutor, fake_runtime, result


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> tuple[Runtime, FakeExecutor]:
    return fake_runtime(tmp_path_factory.mktemp("home"))


@pytest.fixture
def emp(shared: tuple[Runtime, FakeExecutor]) -> tuple[Runtime, FakeExecutor]:
    shared[1].sent.clear()
    shared[1].results.clear()
    return shared


def query(setup: tuple[Runtime, FakeExecutor], sql: str, params: list[Any] | None = None, **kw: Any) -> QueryResult:
    runtime, _ = setup
    return run_query(runtime, runtime.resolve_company("EMP"), sql, params, **kw)


def sent(setup: tuple[Runtime, FakeExecutor]) -> str:
    return setup[1].sent[-1][0]


# --- the SQL that is sent ------------------------------------------------------------------------


def test_sent_sql_is_bracketed_capped_and_in_sage_casing(emp: tuple[Runtime, FakeExecutor]) -> None:
    out = query(emp, "select customerno, creditlimit from ar_customer where customerstatus = 'A'")
    assert sent(emp) == (
        "SELECT TOP 1001 [AR_Customer].[CustomerNo] AS [CustomerNo], [AR_Customer].[CreditLimit] AS [CreditLimit] "
        "FROM [AR_Customer] AS [AR_Customer] WHERE [AR_Customer].[CustomerStatus] = 'A'"
    )
    assert out.sent_sql == [sent(emp)]
    assert (out.company, out.backend, out.mode) == ("EMP", "sqlserver", "native")


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("SELECT TOP 5 CustomerNo FROM AR_Customer", "SELECT TOP 5 "),  # the caller's TOP stays
        ("SELECT CustomerNo FROM AR_Customer LIMIT 7", "SELECT TOP 7 "),
        ("SELECT CustomerNo FROM AR_Customer ORDER BY CustomerNo OFFSET 10 ROWS", "OFFSET 10 ROWS"),
        ("SELECT DISTINCT State FROM AR_Customer", "SELECT DISTINCT TOP 1001 "),
        ("WITH c AS (SELECT CustomerNo FROM AR_Customer) SELECT CustomerNo FROM c", "SELECT TOP 1001 [c]"),
    ],
)
def test_top_is_added_only_where_safe(emp: tuple[Runtime, FakeExecutor], sql: str, fragment: str) -> None:
    query(emp, sql)
    assert fragment in sent(emp)


def test_set_operations_are_capped_by_the_fetch_not_top(emp: tuple[Runtime, FakeExecutor]) -> None:
    query(emp, "SELECT CustomerNo FROM AR_Customer UNION SELECT CustomerNo FROM AR_OpenInvoice ORDER BY 1")
    assert "TOP" not in sent(emp)


def test_tsql_functions_survive(emp: tuple[Runtime, FakeExecutor]) -> None:
    """The ANSI reader would turn DATEDIFF(day, a, b) into DATEDIFF(GETDATE, a, day) (decisions D17)."""
    query(emp, "SELECT DATEDIFF(day, InvoiceDate, GETDATE()) AS age FROM AR_OpenInvoice", dialect="ansi")
    assert "DATEDIFF(DAY, CAST([AR_OpenInvoice].[InvoiceDate] AS DATETIME2), CAST(GETDATE() AS DATETIME2))" in sent(emp)


def test_odbc_date_escapes_become_casts(emp: tuple[Runtime, FakeExecutor]) -> None:
    query(emp, "SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= {d '2024-12-01'}")
    assert "[AR_OpenInvoice].[InvoiceDate] >= CAST('2024-12-01' AS DATE)" in sent(emp)


def test_computed_columns_are_named_after_their_text(emp: tuple[Runtime, FakeExecutor]) -> None:
    fake = emp[1]
    fake.results.append(result([("", "int"), ("", "decimal(38,2)")], [(3, Decimal("10.50"))]))
    out = query(emp, "SELECT COUNT(*), SUM(balance) FROM AR_OpenInvoice")  # named as written
    assert "COUNT(*) AS [COUNT(*)], SUM([AR_OpenInvoice].[Balance]) AS [SUM(balance)]" in sent(emp)
    assert [c.name for c in out.columns] == ["COUNT(*)", "SUM(balance)"]
    assert [c.semantic for c in out.columns] == ["number", "money"]
    assert out.rows == [[3, "10.50"]]


def test_masked_columns_are_never_fetched(emp: tuple[Runtime, FakeExecutor]) -> None:
    fake = emp[1]
    fake.results.append(
        result([("VendorNo", "varchar(7)"), ("TaxpayerIDSocialSecurityNo", "varchar(3)")], [("V1", "***")])
    )
    out = query(emp, "SELECT DISTINCT VendorNo, TaxpayerIDSocialSecurityNo FROM AP_Vendor ORDER BY 2")
    assert "[AP_Vendor].[TaxpayerIDSocialSecurityNo]" not in sent(emp)
    assert "'***' AS [TaxpayerIDSocialSecurityNo]" in sent(emp)
    column = out.columns[1]
    assert (column.masked, column.source, column.type) == (True, "AP_Vendor.TaxpayerIDSocialSecurityNo", "varchar(10)")
    assert out.rows == [["V1", "***"]]
    assert any(w.startswith("COLUMNS_MASKED: AP_Vendor.TaxpayerIDSocialSecurityNo") for w in out.warnings)


def test_masking_covers_every_union_branch(emp: tuple[Runtime, FakeExecutor]) -> None:
    query(emp, "SELECT VendorName FROM AP_Vendor UNION ALL SELECT TaxpayerIDSocialSecurityNo FROM AP_Vendor")
    assert sent(emp).count("'***' AS") == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT VendorNo FROM AP_Vendor WHERE TaxpayerIDSocialSecurityNo LIKE '9%'",
        "SELECT VendorNo FROM AP_Vendor ORDER BY TaxpayerIDSocialSecurityNo",
        "SELECT LEFT(TaxpayerIDSocialSecurityNo, 3) FROM AP_Vendor",
        "SELECT COUNT(DISTINCT TaxpayerIDSocialSecurityNo) FROM AP_Vendor",
        "SELECT TaxpayerIDSocialSecurityNo, COUNT(*) FROM AP_Vendor GROUP BY TaxpayerIDSocialSecurityNo",
        "SELECT v.VendorNo FROM AP_Vendor v JOIN AP_Vendor w ON w.Form1099 = v.Form1099 "
        "AND w.TaxpayerIDSocialSecurityNo = v.TaxpayerIDSocialSecurityNo",
        "SELECT x FROM (SELECT TaxpayerIDSocialSecurityNo AS x FROM AP_Vendor) AS s",
        "SELECT * FROM (SELECT * FROM AP_Vendor) AS s",
    ],
)
def test_masked_columns_cannot_be_used_beyond_selecting(emp: tuple[Runtime, FakeExecutor], sql: str) -> None:
    with pytest.raises(SageError) as raised:
        query(emp, sql)
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert emp[1].sent == []


def test_select_star_shows_core_columns(emp: tuple[Runtime, FakeExecutor]) -> None:
    runtime, _ = emp
    catalog, _ = runtime.catalog(runtime.resolve_company("EMP"))
    header = catalog.table("SO_SalesOrderHeader")
    assert header is not None
    query(emp, "SELECT * FROM SO_SalesOrderHeader")
    projections = sent(emp).split(" FROM ")[0]
    assert projections.count(" AS ") == len(header.default_columns()) < len(header.columns)
    assert "UDF_" not in projections
    out = query(emp, "SELECT h.* FROM SO_SalesOrderHeader h JOIN AR_Customer c ON c.CustomerNo = h.CustomerNo")
    assert out.warnings == [
        f"COLUMNS_PRUNED: SELECT * shows the {len(header.default_columns())} core columns of SO_SalesOrderHeader "
        f"(of {len(header.columns)}); name other columns to get them."
    ]


# --- values ---------------------------------------------------------------------------------------


def test_values_become_json(emp: tuple[Runtime, FakeExecutor]) -> None:
    fake = emp[1]
    columns = [
        ("CustomerNo", "varchar(20)"),
        ("CustomerName", "varchar(50)"),
        ("CreditLimit", "decimal(13,2)"),
        ("DateLastPayment", "datetime"),
        ("DateEstablished", "datetime"),
    ]
    rows = [
        ("C0001", "Trailing Spaces Ltd   ", Decimal("1500.00"), datetime(1753, 1, 1), datetime(2020, 5, 1)),
        ("C0002", None, Decimal("0E-2"), datetime(2024, 1, 2, 13, 5), datetime(2020, 5, 2)),
    ]
    fake.results.append(result(columns, rows))
    sql = "SELECT CustomerNo, CustomerName, CreditLimit, DateLastPayment, DateEstablished FROM AR_Customer"
    out = query(emp, sql)
    assert out.rows == [
        ["C0001", "Trailing Spaces Ltd", "1500.00", None, "2020-05-01"],
        ["C0002", None, "0.00", "2024-01-02T13:05:00", "2020-05-02"],
    ]
    assert out.null_dates_coerced is True
    assert [c.semantic for c in out.columns] == ["code", "text", "money", "date", "date"]
    assert any(w.startswith("NULL_DATES_COERCED: 1 date of 1753-01-01 shows as null") for w in out.warnings)
    assert (out.row_count, out.truncated) == (2, False)


def test_truncation(emp: tuple[Runtime, FakeExecutor]) -> None:
    fake = emp[1]
    fake.results.append(result([("CustomerNo", "varchar(20)")], [(f"C{i}",) for i in range(6)]))
    out = query(emp, "SELECT CustomerNo FROM AR_Customer", max_rows=5)
    assert "SELECT TOP 6 " in sent(emp)
    assert (out.row_count, out.truncated) == (5, True)
    assert any("RESULT_TRUNCATED: Only the first 5 rows" in w and "up to 50000" in w for w in out.warnings)
    assert "5 rows (truncated)" in result_text(out)


# --- arguments and limits -----------------------------------------------------------------------------


def test_params_are_bound_not_inlined(emp: tuple[Runtime, FakeExecutor]) -> None:
    query(emp, "SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = ? AND InvoiceDate >= ? AND Balance > ?",
          ["O'Brien", "2024-12-01", 0.1])  # fmt: skip
    sql, params = emp[1].sent[-1]
    assert "O'Brien" not in sql
    assert params == ("O'Brien", date(2024, 12, 1), Decimal("0.1"))


def test_limits_are_capped(tmp_path: Path) -> None:
    runtime, fake = fake_runtime(tmp_path, limits={"max_rows": 100, "max_rows_hard": 200})
    out = run_query(runtime, runtime.resolve_company("EMP"), "SELECT CustomerNo FROM AR_Customer", max_rows=500,
                    timeout_s=999, mode="hybrid")  # fmt: skip
    assert "SELECT TOP 201 " in fake.sent[-1][0]
    codes = [w.split(":")[0] for w in out.warnings]
    assert codes == ["LIMIT_CAPPED", "LIMIT_CAPPED", "MODE_IGNORED"]


@pytest.mark.parametrize(
    ("sql", "params", "kw", "code"),
    [
        ("DELETE FROM AR_Customer", None, {}, ErrorCode.WRITE_BLOCKED),
        ("SELECT 1 FROM AR_Customer; SELECT 2 FROM AR_Customer", None, {}, ErrorCode.MULTI_STATEMENT),
        ("SELECT * FROM MAS_PCC.dbo.AR_Customer", None, {}, ErrorCode.TABLE_DENIED),
        ("SELECT * FROM AR_CustomerCreditCard", None, {}, ErrorCode.TABLE_DENIED),
        ("SELECT * FROM CI_ItemWarehouse", None, {}, ErrorCode.UNKNOWN_TABLE),
        ("SELECT CustName FROM AR_Customer", None, {}, ErrorCode.UNKNOWN_COLUMN),
        ("SELECT CustomerNo FROM AR_Customer WHERE CustomerNo = ?", None, {}, ErrorCode.PARSE_ERROR),
        ("SELECT CustomerNo FROM AR_Customer", ["x"], {}, ErrorCode.PARSE_ERROR),
        ("SELECT CustomerNo FROM AR_Customer WHERE DateEstablished > ?", ["2024-02-30"], {}, ErrorCode.PARSE_ERROR),
        ("SELECT CustomerNo FROM AR_Customer", None, {"dialect": "providex"}, ErrorCode.DIALECT_UNSUPPORTED),
    ],
)
def test_rejections(
    emp: tuple[Runtime, FakeExecutor], sql: str, params: list[Any] | None, kw: dict[str, Any], code: ErrorCode
) -> None:
    with pytest.raises(SageError) as raised:
        query(emp, sql, params, **kw)
    assert raised.value.code == code
    assert emp[1].sent == []  # nothing reached the database


def test_suggestions(emp: tuple[Runtime, FakeExecutor]) -> None:
    with pytest.raises(SageError) as table:
        query(emp, "SELECT ItemCode FROM CI_ItemWarehouse")
    assert table.value.choices is not None
    assert table.value.choices[0] == "IM_ItemWarehouse"
    with pytest.raises(SageError) as column:
        query(emp, "SELECT CustomerNmae FROM AR_Customer")
    assert column.value.choices is not None
    assert column.value.choices[0] == "CustomerName"
    assert "CustomerNmae" in column.value.message


def test_driver_errors_pass_through(emp: tuple[Runtime, FakeExecutor]) -> None:
    emp[1].results.append(SageError(ErrorCode.LOCK_TIMEOUT, "Lock request time out period exceeded."))
    with pytest.raises(SageError) as raised:
        query(emp, "SELECT CustomerNo FROM AR_Customer")
    assert raised.value.envelope().retryable is True


# --- binding ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "bound"),
    [
        (None, None),
        (True, True),
        (7, 7),
        (0.1, Decimal("0.1")),
        (1e20, Decimal("1e+20")),
        ("2024-12-31", date(2024, 12, 31)),
        ("2024-12-31T10:30", datetime(2024, 12, 31, 10, 30)),
        ("2024-12-31 10:30:05", datetime(2024, 12, 31, 10, 30, 5)),
        ("C0001", "C0001"),
        ("12/31/2024", "12/31/2024"),
    ],
)
def test_bind_value(value: object, bound: object) -> None:
    assert bind_value(value, 0) == bound


@pytest.mark.parametrize("value", [float("nan"), float("inf"), [1], {"a": 1}, "2024-13-01"])
def test_bind_value_rejects(value: object) -> None:
    with pytest.raises(SageError) as raised:
        bind_value(value, 2)
    assert raised.value.code == ErrorCode.PARSE_ERROR
    assert "params[2]" in raised.value.message


def test_bind_counts() -> None:
    assert bind(None, 0) == []
    with pytest.raises(SageError, match="1 \\? placeholder but params has 2 values"):
        bind(["a", "b"], 1)
