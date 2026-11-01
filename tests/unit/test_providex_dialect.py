"""ProvideX generator goldens (SPEC.md §15.1): ANSI or T-SQL in, the text Sage's driver accepts out."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from sage100_mcp.capabilities import Capabilities, FunctionSupport, baseline
from sage100_mcp.sql.providex_dialect import Native, ProvideXWriter
from tests.providex_fixture import catalog, custom, parsed, profile


def write(
    sql: str, caps: Capabilities | str = "all_pass", params: list[Any] | None = None, row_cap: int = 1001
) -> Native:
    tree, values = parsed(sql, params or [])
    capabilities = profile(caps) if isinstance(caps, str) else caps
    return ProvideXWriter(capabilities, catalog(), values, row_cap=row_cap).statement(tree)


def missing(native: Native) -> list[str]:
    return [m.feature for m in native.missing]


GOLDENS = [
    pytest.param(
        "SELECT CustomerNo, CustomerName FROM AR_Customer WHERE CustomerNo = 'C0001'",
        "SELECT CustomerNo, CustomerName FROM AR_Customer WHERE CustomerNo = 'C0001' LIMIT 1001",
        id="plain-select-gets-the-row-cap",
    ),
    pytest.param(
        "SELECT [CustomerNo] FROM [dbo].[AR_Customer];",
        "SELECT CustomerNo FROM AR_Customer LIMIT 1001",
        id="brackets-schema-and-semicolon-go",
    ),
    pytest.param(
        "SELECT c.CustomerNo, i.InvoiceNo FROM AR_Customer c JOIN AR_OpenInvoice i "
        "ON i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo",
        "SELECT c.CustomerNo, i.InvoiceNo FROM {IJ AR_Customer c INNER JOIN AR_OpenInvoice i "
        "ON i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo} LIMIT 1001",
        id="inner-join-becomes-ij",
    ),
    pytest.param(
        "SELECT c.CustomerNo, i.InvoiceNo, t.TermsCodeDesc FROM AR_Customer c "
        "JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo JOIN AR_TermsCode t ON t.TermsCode = i.TermsCode",
        "SELECT c.CustomerNo, i.InvoiceNo, t.TermsCodeDesc FROM {IJ (AR_Customer c INNER JOIN AR_OpenInvoice i "
        "ON i.CustomerNo = c.CustomerNo) INNER JOIN AR_TermsCode t ON t.TermsCode = i.TermsCode} LIMIT 1001",
        id="two-inner-joins-nest",
    ),
    pytest.param(
        "SELECT c.CustomerNo, i.InvoiceNo FROM AR_Customer c LEFT JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo",
        "SELECT c.CustomerNo, i.InvoiceNo FROM {oj AR_Customer c LEFT OUTER JOIN AR_OpenInvoice i "
        "ON (i.CustomerNo = c.CustomerNo)} LIMIT 1001",
        id="left-join-becomes-oj",
    ),
    pytest.param(
        "SELECT c.CustomerNo, i.InvoiceNo, s.SalespersonName FROM AR_Customer c "
        "LEFT JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo "
        "LEFT JOIN AR_Salesperson s ON s.SalespersonNo = c.SalespersonNo",
        "SELECT c.CustomerNo, i.InvoiceNo, s.SalespersonName FROM {oj AR_Customer c LEFT OUTER JOIN AR_OpenInvoice i "
        "ON (i.CustomerNo = c.CustomerNo) LEFT OUTER JOIN AR_Salesperson s ON (s.SalespersonNo = c.SalespersonNo)} "
        "LIMIT 1001",
        id="left-joins-chain-in-one-oj",
    ),
    pytest.param(
        "SELECT a.CustomerNo FROM AR_Customer a, AR_OpenInvoice b WHERE a.CustomerNo = b.CustomerNo",
        "SELECT a.CustomerNo FROM AR_Customer a, AR_OpenInvoice b WHERE a.CustomerNo = b.CustomerNo LIMIT 1001",
        id="comma-join-stays",
    ),
    pytest.param(
        "SELECT InvoiceNo FROM AR_OpenInvoice "
        "WHERE InvoiceDate >= '2024-12-01' AND InvoiceDueDate BETWEEN '2024-01-01' AND '2024-12-31'",
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= {d '2024-12-01'} "
        "AND InvoiceDueDate BETWEEN {d '2024-01-01'} AND {d '2024-12-31'} LIMIT 1001",
        id="iso-dates-become-d-escapes",
    ),
    pytest.param(
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate = {d '2024-12-01'}",
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate = {d '2024-12-01'} LIMIT 1001",
        id="d-escapes-pass-through",
    ),
    pytest.param(
        "SELECT UPPER(CustomerName), LEN(CustomerName), LEFT(CustomerNo, 2), SUBSTRING(CustomerName, 1, 3), "
        "CHARINDEX('a', CustomerName) FROM AR_Customer",
        "SELECT {fn UCASE(CustomerName)}, {fn LENGTH(CustomerName)}, {fn LEFT(CustomerNo, 2)}, "
        "{fn SUBSTRING(CustomerName, 1, 3)}, {fn LOCATE('a', CustomerName)} FROM AR_Customer LIMIT 1001",
        id="string-functions-get-fn",
    ),
    pytest.param(
        "SELECT InvoiceNo, DATEDIFF(day, InvoiceDate, GETDATE()), YEAR(InvoiceDate), ISNULL(TermsCode, 'NA'), "
        "ROUND(Balance, 0), DATEADD(day, 30, InvoiceDate) FROM AR_OpenInvoice",
        "SELECT InvoiceNo, {fn TIMESTAMPDIFF(SQL_TSI_DAY, InvoiceDate, {fn NOW()})}, {fn YEAR(InvoiceDate)}, "
        "{fn IFNULL(TermsCode, 'NA')}, {fn ROUND(Balance, 0)}, {fn TIMESTAMPADD(SQL_TSI_DAY, 30, InvoiceDate)} "
        "FROM AR_OpenInvoice LIMIT 1001",
        id="date-and-null-functions-get-fn",
    ),
    pytest.param(
        "SELECT CustomerNo + '-' + CustomerName FROM AR_Customer",
        "SELECT {fn CONCAT({fn CONCAT(CustomerNo, '-')}, CustomerName)} FROM AR_Customer LIMIT 1001",
        id="text-plus-becomes-concat",
    ),
    pytest.param(
        "SELECT TOP 10 CustomerNo FROM AR_Customer",
        "SELECT CustomerNo FROM AR_Customer LIMIT 10",
        id="top-becomes-limit",
    ),
    pytest.param(
        "SELECT CustomerNo FROM AR_Customer LIMIT 10 OFFSET 5",
        "SELECT CustomerNo FROM AR_Customer LIMIT 10 OFFSET 5",
        id="limit-offset",
    ),
    pytest.param(
        "SELECT CustomerNo, CustomerName FROM AR_Customer ORDER BY CustomerName DESC",
        "SELECT CustomerNo, CustomerName FROM AR_Customer ORDER BY 2 DESC",
        id="order-by-uses-positions-and-no-cap",
    ),
    pytest.param(
        "SELECT CustomerNo, COUNT(*) AS n FROM AR_OpenInvoice GROUP BY CustomerNo HAVING COUNT(*) > 2 ORDER BY n DESC",
        "SELECT CustomerNo, COUNT(*) FROM AR_OpenInvoice GROUP BY CustomerNo HAVING COUNT(*) > 2 ORDER BY 2 DESC",
        id="aliases-are-never-sent",
    ),
    pytest.param(
        "SELECT VendorNo FROM AP_Vendor UNION SELECT VendorNo FROM AP_OpenInvoice ORDER BY VendorNo",
        "SELECT VendorNo FROM AP_Vendor UNION SELECT VendorNo FROM AP_OpenInvoice ORDER BY 1",
        id="union",
    ),
    pytest.param(
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = 'C1' OR CustomerNo = 'C2' OR CustomerNo IN ('C3')",
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo IN ('C1', 'C2', 'C3') LIMIT 1001",
        id="same-column-or-becomes-in",
    ),
    pytest.param(
        "SELECT CustomerNo FROM AR_Customer WHERE CustomerName LIKE 'A%' AND CustomerName NOT LIKE '%x' "
        "AND CustomerNo NOT IN ('a') AND EmailAddress IS NOT NULL AND TermsCode IS NULL",
        "SELECT CustomerNo FROM AR_Customer WHERE CustomerName LIKE 'A%' AND CustomerName NOT LIKE '%x' "
        "AND CustomerNo NOT IN ('a') AND EmailAddress IS NOT NULL AND TermsCode IS NULL LIMIT 1001",
        id="negations-survive",
    ),
    pytest.param(
        "SELECT CustomerNo, CAST(CreditLimit AS DECIMAL(12,2)) FROM AR_Customer",
        "SELECT CustomerNo, CAST(CreditLimit AS DECIMAL(12, 2)) FROM AR_Customer LIMIT 1001",
        id="cast",
    ),
    pytest.param(
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE Balance < -100",
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE Balance < -100 LIMIT 1001",
        id="negative-literal-when-accepted",
    ),
]


@pytest.mark.parametrize(("sql", "expected"), GOLDENS)
def test_goldens(sql: str, expected: str) -> None:
    native = write(sql)
    assert native.missing == []
    assert native.sql == expected
    assert ";" not in native.sql
    assert "[" not in native.sql


def test_numbers_are_never_negative_where_the_driver_says_so() -> None:
    native = write("SELECT InvoiceNo FROM AR_OpenInvoice WHERE Balance < -100", "pxplus_full")
    assert native.sql == "SELECT InvoiceNo FROM AR_OpenInvoice WHERE Balance < (0 - 100) LIMIT 1001"


def test_markers_are_sent_with_typed_values() -> None:
    native = write("SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= ?", params=["2024-12-01"])
    assert native.sql == "SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= ? LIMIT 1001"
    assert native.params == [date(2024, 12, 1)]


def test_values_follow_the_written_order() -> None:
    """CONCAT(a, b, c) is written as CONCAT(a, CONCAT(b, c)); the ? values still line up."""
    native = write("SELECT CONCAT(?, CustomerName, ?) FROM AR_Customer", params=["a", "b"])
    assert native.sql == "SELECT {fn CONCAT(?, {fn CONCAT(CustomerName, ?)})} FROM AR_Customer LIMIT 1001"
    assert native.params == ["a", "b"]


def test_values_are_inlined_as_typed_literals_without_markers() -> None:
    """§5.5 step 5: strings with doubled quotes, dates as {d}, numbers checked; never raw text."""
    native = write(
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = ? AND InvoiceDate >= ? AND Balance > ?",
        custom("calvin_graham", compound_and=True),
        ["O'Brien", "2024-01-01", -5.25],
    )
    assert native.sql == (
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = 'O''Brien' "
        "AND InvoiceDate >= {d '2024-01-01'} AND Balance > (0 - 5.25) LIMIT 1001"
    )
    assert native.params == []


def test_inlined_values_cannot_inject() -> None:
    native = write("SELECT CustomerNo FROM AR_Customer WHERE CustomerName = ?", "calvin_graham", ["x' OR '1'='1"])
    assert native.sql == "SELECT CustomerNo FROM AR_Customer WHERE CustomerName = 'x'' OR ''1''=''1' LIMIT 1001"


@pytest.mark.parametrize("caps", ["calvin_graham", "all_pass"])
def test_a_value_that_is_not_a_date_is_never_sent(caps: str) -> None:
    """bind() already refuses 2024-13-45; text that isn't date-shaped reaches the writer as text."""
    native = write("SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= ?", caps, ["next tuesday"])
    assert "date_value" in missing(native)


def test_top_without_limit_support_becomes_a_fetch_cap() -> None:
    native = write("SELECT TOP 10 CustomerNo FROM AR_Customer", custom("calvin_graham", limit=False))
    assert native.sql == "SELECT CustomerNo FROM AR_Customer"
    assert (native.fetch_limit, native.fetch_offset) == (10, 0)


def test_offset_without_support_is_skipped_while_fetching() -> None:
    native = write("SELECT CustomerNo FROM AR_Customer LIMIT 10 OFFSET 5", custom(limit_offset=False))
    assert native.sql == "SELECT CustomerNo FROM AR_Customer"
    assert (native.fetch_limit, native.fetch_offset) == (10, 5)


def test_order_by_with_a_limit_is_never_sent() -> None:
    """The grammar puts LIMIT before ORDER BY, so the driver could cut rows before sorting them."""
    for sql in (
        "SELECT TOP 5 CustomerNo FROM AR_Customer ORDER BY CustomerNo",
        "SELECT CustomerNo FROM AR_Customer ORDER BY CustomerNo OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
    ):
        assert missing(write(sql)) == ["order_by_with_limit"]


@pytest.mark.parametrize(
    ("sql", "caps", "features"),
    [
        ("SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = 'C1' OR Balance > 5", "calvin_graham", ["or"]),
        (
            "SELECT CustomerNo FROM AR_Customer WHERE ARDivisionNo = '00' AND CustomerNo = 'C1'",
            "calvin_graham",
            ["compound_and"],
        ),
        ("SELECT COUNT(DISTINCT CustomerNo) FROM AR_OpenInvoice", "pxplus_full", ["count_distinct"]),
        ("SELECT CASE WHEN Balance > 0 THEN 'D' ELSE 'C' END FROM AR_OpenInvoice", "calvin_graham", ["case"]),
        (
            "SELECT CustomerNo FROM AR_Customer WHERE CustomerNo IN (SELECT CustomerNo FROM AR_OpenInvoice)",
            "calvin_graham",
            ["subquery_in"],
        ),
        ("SELECT CustomerNo FROM AR_Customer ORDER BY CustomerNo", "calvin_graham", ["order_by_position"]),
        (
            "SELECT VendorNo FROM AP_Vendor UNION ALL SELECT VendorNo FROM AP_OpenInvoice",
            "calvin_graham",
            ["union_all"],
        ),
        (
            "SELECT c.CustomerNo FROM AR_Customer c LEFT JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo "
            "WHERE i.Balance > 0",
            "all_pass",
            ["outer_join_where"],
        ),
        (
            "SELECT c.CustomerNo FROM AR_Customer c RIGHT JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo",
            "all_pass",
            ["outer_join"],
        ),
        (
            "SELECT c.CustomerNo FROM AR_Customer c JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo "
            "LEFT JOIN AR_Salesperson s ON s.SalespersonNo = c.SalespersonNo",
            "all_pass",
            ["mixed_joins"],
        ),
        ("SELECT CustomerNo, ROW_NUMBER() OVER (ORDER BY CustomerNo) FROM AR_Customer", "all_pass", ["window"]),
        ("WITH c AS (SELECT CustomerNo FROM AR_Customer) SELECT CustomerNo FROM c", "all_pass", ["cte"]),
        ("SELECT 1", "all_pass", ["no_from"]),
        ("SELECT UPPER(CustomerName) FROM AR_Customer", "calvin_graham", ["fn:UCASE"]),
        ("SELECT CustomerNo FROM AR_Customer WHERE CustomerName LIKE 'A%'", custom(like=False), ["like"]),
        (
            "SELECT CustomerNo, SUM(CreditLimit) FROM AR_Customer GROUP BY CustomerNo",
            custom(aggregate_exact=False),
            ["aggregate_exact"],
        ),
        (
            "SELECT YEAR(InvoiceDate), COUNT(*) FROM AR_OpenInvoice GROUP BY YEAR(InvoiceDate)",
            "pxplus_full",
            ["group_by_expression"],
        ),
    ],
)
def test_what_a_profile_lacks_is_recorded(sql: str, caps: Capabilities | str, features: list[str]) -> None:
    assert missing(write(sql, caps)) == features


def test_every_missing_feature_is_listed_not_just_the_first() -> None:
    native = write(
        "SELECT CASE WHEN Balance > 0 THEN 'D' END FROM AR_OpenInvoice WHERE CustomerNo = 'A' OR Balance < 0",
        "calvin_graham",
    )
    assert missing(native) == ["case", "or"]


def test_inner_joins_fall_back_to_comma_joins() -> None:
    native = write(
        "SELECT c.CustomerNo FROM AR_Customer c JOIN AR_OpenInvoice i "
        "ON i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo WHERE i.Balance <> 0",
        baseline(),
    )
    assert native.missing == []
    assert native.sql == (
        "SELECT c.CustomerNo FROM AR_Customer c, AR_OpenInvoice i "
        "WHERE i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo AND i.Balance <> 0"
    )


def test_one_inner_join_can_use_oj_when_ij_fails() -> None:
    native = write(
        "SELECT c.CustomerNo FROM AR_Customer c JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo",
        custom(ij=False),
    )
    assert native.sql == (
        "SELECT c.CustomerNo FROM {oj AR_Customer c INNER JOIN AR_OpenInvoice i ON (i.CustomerNo = c.CustomerNo)} "
        "LIMIT 1001"
    )


def test_bare_functions_where_only_the_bare_form_works() -> None:
    caps = profile("all_pass")
    functions = {**caps.functions, "UCASE": FunctionSupport(escaped=False, bare=True)}
    native = write("SELECT UPPER(CustomerName) FROM AR_Customer", caps.model_copy(update={"functions": functions}))
    assert native.sql == "SELECT UCASE(CustomerName) FROM AR_Customer LIMIT 1001"


def test_the_writer_leaves_the_input_tree_alone() -> None:
    tree, values = parsed("SELECT c.CustomerNo FROM AR_Customer c JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo")
    before = tree.sql()
    ProvideXWriter(baseline(), catalog(), values).statement(tree)  # rewrites to a comma join on its copy
    assert tree.sql() == before


def test_decimal_values_keep_their_digits() -> None:
    native = write("SELECT InvoiceNo FROM AR_OpenInvoice WHERE Balance > ?", "calvin_graham", [1234.5])
    assert native.sql.endswith("WHERE Balance > 1234.5 LIMIT 1001")
    assert Decimal("1234.5") == Decimal(native.sql.split("> ")[1].split(" ")[0])
