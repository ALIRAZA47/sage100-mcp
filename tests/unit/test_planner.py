"""Native or hybrid (SPEC.md §5.5 step 4, §15.1): the plan for each query under the three driver profiles."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from sage100_mcp.capabilities import Capabilities
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.query import _mask
from sage100_mcp.sql.planner import Mode, Plan, Pull, plan
from tests.providex_fixture import PROFILE_NAMES, catalog, custom, parsed, profile


def planned(
    sql: str, caps: Capabilities | str = "all_pass", params: list[Any] | None = None, *, mode: Mode = "auto"
) -> Plan:
    tree, values = parsed(sql, params or [])
    capabilities = profile(caps) if isinstance(caps, str) else caps
    return plan(tree, catalog(), capabilities, values, mode=mode, row_cap=1001)


def pull(result: Plan, alias: str) -> Pull:
    return next(p for p in result.pulls if p.alias == alias)


KEY_JOIN = (
    "SELECT c.CustomerNo, i.InvoiceNo FROM AR_Customer c JOIN AR_OpenInvoice i "
    "ON i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo"
)
MODES = [
    # sql, then the mode under all_pass, pxplus_full and calvin_graham
    ("SELECT CustomerNo, CustomerName FROM AR_Customer WHERE CustomerNo = 'C0001'", "nnn"),
    (KEY_JOIN, "nnh"),
    ("SELECT CASE WHEN Balance > 0 THEN 'D' ELSE 'C' END FROM AR_OpenInvoice", "nnh"),
    ("SELECT COUNT(DISTINCT CustomerNo) FROM AR_OpenInvoice", "nhh"),
    ("SELECT CustomerNo FROM AR_Customer WHERE CustomerNo IN (SELECT CustomerNo FROM AR_OpenInvoice)", "nnh"),
    ("SELECT d.CustomerNo FROM (SELECT CustomerNo FROM AR_Customer) d", "nhh"),
    ("SELECT VendorNo FROM AP_Vendor UNION SELECT VendorNo FROM AP_OpenInvoice", "nnh"),
    ("SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = 'C1' OR Balance > 5", "nnh"),
    ("SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = 'C1' OR CustomerNo = 'C2'", "nnn"),
    ("SELECT UPPER(CustomerName) FROM AR_Customer", "nnh"),
    ("SELECT CustomerNo FROM AR_Customer ORDER BY CustomerNo", "nnh"),
    ("SELECT TOP 5 CustomerNo FROM AR_Customer", "nnn"),
    ("SELECT TOP 5 CustomerNo FROM AR_Customer ORDER BY CustomerNo", "hhh"),
    ("WITH c AS (SELECT CustomerNo FROM AR_Customer) SELECT CustomerNo FROM c", "hhh"),
    ("SELECT CustomerNo, ROW_NUMBER() OVER (ORDER BY CustomerNo) FROM AR_Customer", "hhh"),
    ("SELECT VendorNo FROM AP_Vendor EXCEPT SELECT VendorNo FROM AP_OpenInvoice", "hhh"),
    ("SELECT c.CustomerNo FROM AR_Customer c LEFT JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo "
     "WHERE i.Balance > 0", "hhh"),
]  # fmt: skip


@pytest.mark.parametrize(("sql", "modes"), MODES)
@pytest.mark.parametrize("position", range(3), ids=PROFILE_NAMES)
def test_each_profile_gets_the_expected_mode(sql: str, modes: str, position: int) -> None:
    result = planned(sql, PROFILE_NAMES[position])
    assert result.mode == {"n": "native", "h": "hybrid"}[modes[position]]
    if result.mode == "native":
        assert result.native is not None
        assert result.pulls == []
        assert result.sent_sql == [result.native.sql]
    else:
        assert result.reasons  # explain_query says why
        assert result.native is None
        assert len(result.sent_sql) == len(result.pulls) >= 1


def test_native_mode_refuses_what_the_driver_lacks() -> None:
    with pytest.raises(SageError) as caught:
        planned("SELECT COUNT(DISTINCT CustomerNo) FROM AR_OpenInvoice", "pxplus_full", mode="native")
    assert caught.value.code == ErrorCode.DIALECT_UNSUPPORTED
    assert "COUNT(DISTINCT x)" in caught.value.message
    assert "hybrid" in (caught.value.hint or "")


def test_hybrid_mode_is_honoured_for_a_query_the_driver_could_run() -> None:
    result = planned("SELECT CustomerNo FROM AR_Customer WHERE CustomerNo = 'C1'", mode="hybrid")
    assert result.mode == "hybrid"
    assert result.reasons == []
    assert result.sent_sql == ["SELECT CustomerNo FROM AR_Customer WHERE CustomerNo = 'C1'"]


def test_a_value_that_is_not_a_date_is_a_parse_error() -> None:
    with pytest.raises(SageError) as caught:
        planned("SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= ?", params=["last week"])
    assert caught.value.code == ErrorCode.PARSE_ERROR


# --- pushdown ------------------------------------------------------------------------------------------


def test_every_supported_condition_is_pushed() -> None:
    result = planned(
        "SELECT InvoiceNo, Balance FROM AR_OpenInvoice "
        "WHERE CustomerNo = 'C1' AND Balance <> 0 AND InvoiceType IN ('IN', 'CM') AND InvoiceDate >= '2024-01-01'",
        mode="hybrid",
    )
    (only,) = result.pulls
    assert only.pushed == 4
    assert only.native.sql == (
        "SELECT CustomerNo, InvoiceNo, InvoiceType, InvoiceDate, Balance FROM AR_OpenInvoice "
        "WHERE CustomerNo = 'C1' AND Balance <> 0 AND InvoiceType IN ('IN', 'CM') AND InvoiceDate >= {d '2024-01-01'}"
    )


def test_without_compound_and_only_the_first_condition_is_pushed() -> None:
    result = planned("SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = 'C1' AND Balance <> 0", "calvin_graham")
    (only,) = result.pulls
    assert only.pushed == 1
    assert only.native.sql.endswith("FROM AR_OpenInvoice WHERE CustomerNo = 'C1'")


def test_conditions_the_driver_cannot_take_stay_local() -> None:
    result = planned(
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE UPPER(InvoiceType) = 'IN' AND CustomerNo LIKE 'C%'",
        custom(fn_UCASE=False),
        mode="hybrid",
    )
    (only,) = result.pulls
    assert only.pushed == 1
    assert only.native.sql.endswith("WHERE CustomerNo LIKE 'C%'")


def test_is_null_is_never_pushed() -> None:
    """IS NULL is not null-rejecting: under an outer join it must see the padding rows, so it stays local."""
    result = planned(
        "SELECT CustomerNo FROM AR_Customer WHERE TermsCode IS NULL AND EmailAddress IS NOT NULL", mode="hybrid"
    )
    (only,) = result.pulls
    assert only.pushed == 1
    assert only.native.sql.endswith("WHERE EmailAddress IS NOT NULL")


def test_or_on_one_column_is_pushed_as_in() -> None:
    result = planned(
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = 'C1' OR CustomerNo = 'C2'",
        "calvin_graham",
        mode="hybrid",
    )
    assert result.pulls[0].native.sql.endswith("WHERE CustomerNo IN ('C1', 'C2')")


def test_or_across_columns_is_not_pushed() -> None:
    result = planned("SELECT InvoiceNo FROM AR_OpenInvoice WHERE CustomerNo = 'C1' OR Balance > 5", "calvin_graham")
    assert result.pulls[0].pushed == 0


def test_left_join_pushes_only_what_cannot_change_the_padding() -> None:
    result = planned(
        "SELECT c.CustomerNo, i.InvoiceNo FROM AR_Customer c LEFT JOIN AR_OpenInvoice i "
        "ON i.CustomerNo = c.CustomerNo AND i.Balance <> 0 AND c.CustomerType = 'R' "
        "WHERE c.ARDivisionNo = '00'",
        mode="hybrid",
    )
    customers, invoices = pull(result, "c"), pull(result, "i")
    # An ON condition on the preserved side decides matching, not which customers appear: not pushed.
    assert customers.native.sql.endswith("FROM AR_Customer WHERE ARDivisionNo = '00'")
    assert invoices.native.sql.endswith("FROM AR_OpenInvoice WHERE Balance <> 0")


def test_right_join_pushes_only_onto_the_nullable_side() -> None:
    """In a RIGHT JOIN the joined table is the preserved one; its ON conditions only decide matching."""
    result = planned(
        "SELECT i.InvoiceNo, c.CustomerName FROM AR_Customer c RIGHT JOIN AR_OpenInvoice i "
        "ON i.CustomerNo = c.CustomerNo AND i.Balance > 0 AND c.CustomerType = 'R'",
        mode="hybrid",
    )
    assert pull(result, "i").pushed == 0
    assert pull(result, "c").native.sql.endswith("FROM AR_Customer WHERE CustomerType = 'R'")


def test_full_join_pushes_no_on_condition() -> None:
    result = planned(
        "SELECT c.CustomerNo, i.InvoiceNo FROM AR_Customer c FULL JOIN AR_OpenInvoice i "
        "ON i.CustomerNo = c.CustomerNo AND i.Balance > 0 AND c.CustomerType = 'R'",
        mode="hybrid",
    )
    assert [p.pushed for p in result.pulls] == [0, 0]


def test_pulls_read_only_the_columns_the_query_names() -> None:
    result = planned("SELECT COUNT(*) FROM AR_Customer WHERE CustomerType = 'R'", "calvin_graham", mode="hybrid")
    assert [c.name for c in result.pulls[0].columns] == ["CustomerType"]
    result = planned("SELECT COUNT(*) FROM AR_Customer", mode="hybrid")
    assert [c.name for c in result.pulls[0].columns] == ["ARDivisionNo"]  # the key's first column


def test_masked_columns_are_never_pulled() -> None:
    tree, values = parsed("SELECT VendorNo, TaxpayerIDSocialSecurityNo FROM AP_Vendor")
    _mask(tree, [1])  # what run_query does before planning: the masked output becomes '***'
    result = plan(tree, catalog(), profile("all_pass"), values, mode="hybrid", row_cap=1001)
    assert [c.name for c in result.pulls[0].columns] == ["VendorNo"]
    assert "Taxpayer" not in result.pulls[0].native.sql


def test_a_table_read_twice_is_pulled_twice() -> None:
    result = planned(
        "SELECT a.CustomerNo FROM AR_Customer a JOIN AR_Customer b ON b.CustomerNo = a.CustomerNo "
        "WHERE b.CustomerType = 'R'",
        mode="hybrid",
    )
    assert [p.alias for p in result.pulls] == ["a", "b"]
    assert pull(result, "b").native.sql.endswith("WHERE CustomerType = 'R'")
    assert pull(result, "a").pushed == 0


def test_tables_inside_a_with_are_pulled_with_their_own_filters() -> None:
    result = planned(
        "WITH big AS (SELECT CustomerNo, Balance FROM AR_OpenInvoice WHERE Balance > ?) "
        "SELECT CustomerNo, COUNT(*) FROM big WHERE CustomerNo <> ? GROUP BY CustomerNo",
        params=[100, "C0002"],
    )
    (only,) = result.pulls
    assert only.native.sql == "SELECT CustomerNo, Balance FROM AR_OpenInvoice WHERE Balance > ?"
    assert only.native.params == [100]
    assert result.local_params == [100, "C0002"]  # DuckDB's text writes the WITH first


def test_pulls_never_limit_or_sort() -> None:
    result = planned("SELECT TOP 5 CustomerNo FROM AR_Customer ORDER BY CustomerName")
    assert result.mode == "hybrid"
    assert "LIMIT" not in result.pulls[0].native.sql
    assert "ORDER" not in result.pulls[0].native.sql


def test_pushed_values_keep_their_types() -> None:
    result = planned(
        "SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= ? AND Balance > ?",
        "all_pass",
        ["2024-06-01", 12.5],
        mode="hybrid",
    )
    assert result.pulls[0].native.params[1] == Decimal("12.5")
    assert str(result.pulls[0].native.params[0]) == "2024-06-01"
