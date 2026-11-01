"""The M2 gate's sample queries (SPEC.md §18): 50 SELECTs, at least 15 of which always run hybrid.

Each sample runs through run_query, and its rows are compared with an oracle: every table the query
names is read whole through the same driver, loaded into DuckDB, and the query runs there as written.
The oracle shares no code with the ProvideX generator, the planner's pushdown or the hybrid engine,
so a wrong rewrite or a pushed filter that drops rows shows up as a difference. The queries use
only core columns of tables every Sage 100 company has, with key values taken from the data, so
the same list runs on the emulator's synthetic EMP and on the ABC sample company.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import duckdb
from sqlglot import exp

from sage100_mcp.companies import Company
from sage100_mcp.errors import SageError
from sage100_mcp.query import Cell, _cell, run_query
from sage100_mcp.runtime import Runtime
from sage100_mcp.sql import hybrid
from sage100_mcp.sql.bind import bind, number_placeholders, ordered_params
from sage100_mcp.sql.guard import check
from sage100_mcp.sql.resolve import resolve


@dataclass(frozen=True)
class Sample:
    name: str
    sql: str
    anchors: tuple[str, ...] = ()  # params, by anchor name, taken from the company's data
    ordered: bool = False  # the ORDER BY is total, so row order is compared too
    always_hybrid: bool = False  # uses SQL no Sage driver build runs, so every profile goes hybrid


C = "ARDivisionNo = i.ARDivisionNo AND c.CustomerNo = i.CustomerNo"
SAMPLES = [
    Sample(
        "customer_by_key",
        "SELECT CustomerNo, CustomerName FROM AR_Customer WHERE ARDivisionNo = ? AND CustomerNo = ?",
        ("division", "customer"),
    ),
    Sample("customers", "SELECT ARDivisionNo, CustomerNo, CustomerName, CreditLimit FROM AR_Customer"),
    Sample(
        "open_invoices",
        "SELECT ARDivisionNo, CustomerNo, InvoiceNo, InvoiceType, Balance FROM AR_OpenInvoice WHERE Balance <> 0",
    ),
    Sample(
        "open_ar_by_customer",
        "SELECT ARDivisionNo, CustomerNo, SUM(Balance) AS open_balance FROM AR_OpenInvoice "
        "WHERE Balance <> 0 GROUP BY ARDivisionNo, CustomerNo",
    ),
    Sample(
        "invoices_since", "SELECT InvoiceNo, InvoiceDate, Balance FROM AR_OpenInvoice WHERE InvoiceDate >= '2020-01-01'"
    ),
    Sample(
        "invoice_date_range",
        "SELECT InvoiceNo, InvoiceDueDate FROM AR_OpenInvoice "
        "WHERE InvoiceDueDate BETWEEN '2020-01-01' AND '2030-12-31'",
    ),
    Sample(
        "customer_invoices",
        f"SELECT c.CustomerNo, c.CustomerName, i.InvoiceNo, i.Balance FROM AR_Customer c "
        f"JOIN AR_OpenInvoice i ON c.{C} WHERE i.Balance <> 0",
    ),
    Sample(
        "customer_invoices_terms",
        f"SELECT c.CustomerNo, i.InvoiceNo, t.TermsCodeDesc FROM AR_Customer c "
        f"JOIN AR_OpenInvoice i ON c.{C} JOIN AR_TermsCode t ON t.TermsCode = i.TermsCode",
    ),
    Sample(
        "customers_left_invoices",
        f"SELECT c.CustomerNo, i.InvoiceNo FROM AR_Customer c LEFT JOIN AR_OpenInvoice i ON c.{C}",
    ),
    Sample(
        "open_ap_by_vendor",
        "SELECT APDivisionNo, VendorNo, SUM(Balance) AS open_balance FROM AP_OpenInvoice "
        "GROUP BY APDivisionNo, VendorNo",
    ),
    Sample(
        "vendor_invoices",
        "SELECT v.VendorNo, v.VendorName, i.InvoiceNo, i.InvoiceAmt FROM AP_Vendor v "
        "JOIN AP_OpenInvoice i ON i.APDivisionNo = v.APDivisionNo AND i.VendorNo = v.VendorNo",
    ),
    Sample("items_like", "SELECT ItemCode, ItemCodeDesc FROM CI_Item WHERE ItemCode LIKE ?", ("item_prefix",)),
    Sample(
        "items_by_type_reversed",
        "SELECT ItemCode, REVERSE(ItemCode) AS reversed FROM CI_Item WHERE ItemType IN ('1', '2')",
        always_hybrid=True,  # REVERSE has no ODBC equivalent; the IN list is still pushed to the driver
    ),
    Sample(
        "stock_on_hand", "SELECT ItemCode, WarehouseCode, QuantityOnHand FROM IM_ItemWarehouse WHERE QuantityOnHand > 0"
    ),
    Sample(
        "stock_by_warehouse",
        "SELECT WarehouseCode, SUM(QuantityOnHand) AS on_hand FROM IM_ItemWarehouse GROUP BY WarehouseCode",
    ),
    Sample("customer_types", "SELECT DISTINCT CustomerType FROM AR_Customer"),
    Sample(
        "repeat_customers",
        "SELECT CustomerNo, COUNT(*) AS invoices FROM AR_OpenInvoice GROUP BY CustomerNo HAVING COUNT(*) > 1",
    ),
    Sample(
        "sales_orders_not_quotes",
        "SELECT SalesOrderNo, OrderDate, OrderType FROM SO_SalesOrderHeader WHERE OrderType NOT IN ('Q', 'M', 'R')",
    ),
    Sample(
        "sales_order_lines",
        "SELECT h.SalesOrderNo, h.CustomerNo, d.ItemCode, d.QuantityOrdered FROM SO_SalesOrderHeader h "
        "JOIN SO_SalesOrderDetail d ON d.SalesOrderNo = h.SalesOrderNo",
    ),
    Sample(
        "open_purchase_orders",
        "SELECT PurchaseOrderNo, OrderStatus FROM PO_PurchaseOrderHeader WHERE OrderStatus <> 'X'",
    ),
    Sample(
        "po_line_values",
        "SELECT PurchaseOrderNo, ItemCode, QuantityOrdered * UnitCost AS line_value FROM PO_PurchaseOrderDetail",
    ),
    Sample("active_accounts", "SELECT AccountKey, Account, AccountDesc FROM GL_Account WHERE Status = 'A'"),
    Sample(
        "name_functions",
        "SELECT CustomerNo, UPPER(CustomerName) AS upper_name, LEN(CustomerName) AS name_length FROM AR_Customer",
    ),
    Sample(
        "date_parts",
        "SELECT InvoiceNo, YEAR(InvoiceDate) AS y, MONTH(InvoiceDate) AS m FROM AR_OpenInvoice "
        "WHERE InvoiceDate >= '2020-01-01'",
    ),
    Sample(
        "invoice_age",
        "SELECT InvoiceNo, DATEDIFF(day, InvoiceDate, '2030-01-01') AS age FROM AR_OpenInvoice "
        "WHERE InvoiceDate >= '2020-01-01'",
    ),
    Sample(
        "or_one_column",
        "SELECT InvoiceNo, InvoiceType FROM AR_OpenInvoice WHERE InvoiceType = 'IN' OR InvoiceType = 'CM'",
    ),
    Sample("not_like", "SELECT CustomerNo FROM AR_Customer WHERE CustomerName NOT LIKE 'A%'"),
    Sample("has_email", "SELECT CustomerNo FROM AR_Customer WHERE EmailAddress <> ''"),
    Sample("total_open_ar", "SELECT SUM(Balance) AS total FROM AR_OpenInvoice"),
    Sample(
        "customers_in_key_order",
        "SELECT ARDivisionNo, CustomerNo FROM AR_Customer ORDER BY ARDivisionNo, CustomerNo",
        ordered=True,
    ),
    Sample("vendor_numbers", "SELECT VendorNo FROM AP_Vendor UNION SELECT VendorNo FROM AP_OpenInvoice"),
    Sample("or_two_columns", "SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceType = 'CM' OR Balance < 0"),
    Sample(
        "debit_or_credit",
        "SELECT InvoiceNo, CASE WHEN Balance < 0 THEN 'credit' ELSE 'debit' END AS side FROM AR_OpenInvoice",
    ),
    Sample(
        "customers_with_big_invoices",
        "SELECT CustomerNo, CustomerName FROM AR_Customer WHERE CustomerNo IN "
        "(SELECT CustomerNo FROM AR_OpenInvoice WHERE Balance > 1000)",
    ),
    # Always hybrid: no Sage driver build runs these natively.
    Sample(
        "with_clause",
        "WITH open_ar AS (SELECT CustomerNo, Balance FROM AR_OpenInvoice WHERE Balance <> 0) "
        "SELECT CustomerNo, COUNT(*) AS n FROM open_ar GROUP BY CustomerNo",
        always_hybrid=True,
    ),
    Sample(
        "invoice_rank",
        "SELECT CustomerNo, InvoiceNo, ROW_NUMBER() OVER (PARTITION BY ARDivisionNo, CustomerNo "
        "ORDER BY InvoiceNo, InvoiceType) AS seq FROM AR_OpenInvoice",
        always_hybrid=True,
    ),
    Sample(
        "largest_balances",
        "SELECT TOP 5 ARDivisionNo, CustomerNo, InvoiceNo, InvoiceType, Balance FROM AR_OpenInvoice "
        "ORDER BY Balance DESC, ARDivisionNo, CustomerNo, InvoiceNo, InvoiceType",
        ordered=True,
        always_hybrid=True,
    ),
    Sample(
        "customers_without_invoices_except",
        "SELECT CustomerNo FROM AR_Customer EXCEPT SELECT CustomerNo FROM AR_OpenInvoice",
        always_hybrid=True,
    ),
    Sample(
        "customers_with_invoices_intersect",
        "SELECT CustomerNo FROM AR_Customer INTERSECT SELECT CustomerNo FROM AR_OpenInvoice",
        always_hybrid=True,
    ),
    Sample(
        "invoices_right_customers",
        # i is the preserved side: its ON condition decides matching and must not filter the read of i.
        f"SELECT i.InvoiceNo, c.CustomerName FROM AR_Customer c RIGHT JOIN AR_OpenInvoice i ON c.{C} AND i.Balance > 0",
        always_hybrid=True,
    ),
    Sample(
        "customers_full_invoices",
        f"SELECT c.CustomerNo, i.InvoiceNo FROM AR_Customer c FULL JOIN AR_OpenInvoice i ON c.{C}",
        always_hybrid=True,
    ),
    Sample(
        "mixed_joins",
        f"SELECT c.CustomerNo, i.InvoiceNo, s.SalespersonName FROM AR_Customer c JOIN AR_OpenInvoice i "
        f"ON c.{C} LEFT JOIN AR_Salesperson s ON s.SalespersonDivisionNo = c.SalespersonDivisionNo "
        f"AND s.SalespersonNo = c.SalespersonNo",
        always_hybrid=True,
    ),
    Sample(
        "left_join_filtered",
        f"SELECT c.CustomerNo, i.InvoiceNo FROM AR_Customer c LEFT JOIN AR_OpenInvoice i ON c.{C} WHERE i.Balance > 0",
        always_hybrid=True,
    ),
    Sample(
        "customers_without_invoices",
        f"SELECT c.CustomerNo FROM AR_Customer c LEFT JOIN AR_OpenInvoice i ON c.{C} WHERE i.InvoiceNo IS NULL",
        always_hybrid=True,
    ),
    Sample(
        "first_contact",
        "SELECT CustomerNo, COALESCE(NULLIF(EmailAddress, ''), NULLIF(TelephoneNo, ''), 'none') "
        "AS contact FROM AR_Customer",
        always_hybrid=True,
    ),
    Sample(
        "second_page",
        "SELECT ARDivisionNo, CustomerNo FROM AR_Customer ORDER BY ARDivisionNo, CustomerNo "
        "OFFSET 5 ROWS FETCH NEXT 5 ROWS ONLY",
        ordered=True,
        always_hybrid=True,
    ),
    Sample(
        "order_by_expression", "SELECT InvoiceNo, Balance FROM AR_OpenInvoice ORDER BY Balance * 2", always_hybrid=True
    ),
    Sample("iif", "SELECT InvoiceNo, IIF(Balance > 0, 'D', 'C') AS side FROM AR_OpenInvoice", always_hybrid=True),
    Sample(
        "invoice_counts",
        "SELECT c.CustomerNo, (SELECT COUNT(*) FROM AR_OpenInvoice i WHERE i.ARDivisionNo = "
        "c.ARDivisionNo AND i.CustomerNo = c.CustomerNo) AS n FROM AR_Customer c",
        always_hybrid=True,
    ),
    # Native where ORDER BY works: the resolver turns ORDER BY COUNT(*) into the select-list position.
    Sample(
        "busiest_customers",
        "SELECT CustomerNo, COUNT(*) AS n FROM AR_OpenInvoice GROUP BY CustomerNo ORDER BY COUNT(*) DESC, CustomerNo",
        ordered=True,
    ),
]
assert len(SAMPLES) == 50
assert len({s.name for s in SAMPLES}) == 50
assert sum(s.always_hybrid for s in SAMPLES) >= 16  # one spare over the gate's 15


def anchors(runtime: Runtime, company: Company) -> dict[str, Any]:
    """Key values from the company's own data, so the samples find rows wherever they run."""
    executor = runtime.executor(company)
    timeout = runtime.config.limits.query_timeout_s.providex
    customer = executor.query(company.database, "SELECT ARDivisionNo, CustomerNo FROM AR_Customer", max_rows=1,
                              timeout_s=timeout).rows  # fmt: skip
    item = executor.query(company.database, "SELECT ItemCode FROM CI_Item", max_rows=1, timeout_s=timeout).rows
    return {
        "division": str(customer[0][0]).rstrip() if customer else "00",
        "customer": str(customer[0][1]).rstrip() if customer else "none",
        "item_prefix": (str(item[0][0])[:1] if item else "A") + "%",
    }


def oracle(runtime: Runtime, company: Company, sql: str, params: Sequence[Any]) -> list[list[Cell]]:
    """Every named table read whole through the driver, and ``sql`` run on them in DuckDB."""
    checked = check(sql, dialect="tsql")
    catalog, _ = runtime.catalog(company)
    resolved = resolve(checked, catalog, runtime.require_policy())
    values = bind(list(params), checked.placeholders)
    number_placeholders(resolved.tree)
    executor = runtime.executor(company)
    connection = duckdb.connect(":memory:")
    try:
        for name in sorted({node.name for node in resolved.tree.find_all(exp.Table)}):
            table = catalog.table(name)
            if table is None:
                continue  # a WITH name
            columns = [c for c in table.columns if not c.masked]
            raw = executor.query(
                company.database,
                f"SELECT {', '.join(c.name for c in columns)} FROM {table.name}",
                max_rows=1_000_000,
                timeout_s=600,
            )
            kinds = [hybrid.duckdb_type(c) for c in columns]
            body = ", ".join(f'"{c.name}" {kind}' for c, kind in zip(columns, kinds, strict=True))
            connection.execute(f'CREATE TABLE "{table.name}" ({body})')
            if raw.rows:
                marks = ", ".join("?" for _ in columns)
                connection.executemany(
                    f'INSERT INTO "{table.name}" VALUES ({marks})',
                    [[_load(value, kind) for value, kind in zip(row, kinds, strict=True)] for row in raw.rows],
                )
        local = resolved.tree.sql(dialect="duckdb")
        rows = connection.execute(local, ordered_params(resolved.tree, values, "duckdb")).fetchall()
    finally:
        connection.close()
    return [[_cell(value, [], blank_date=False) for value in row] for row in rows]


def _load(value: Any, kind: str) -> Any:
    if isinstance(value, str):
        return value.rstrip()
    if kind == "DATE" and isinstance(value, datetime):
        return value.date()
    return value


def _normal(cell: Cell) -> tuple[str, Any]:
    """Numbers compare by value (2.50 = 2.5, 3 = 3.0), whatever type the engine returned."""
    if isinstance(cell, bool) or cell is None:
        return ("v", cell)
    if isinstance(cell, int | float):
        return ("n", Decimal(repr(round(cell, 6))).normalize() if isinstance(cell, float) else Decimal(cell))
    try:
        return ("n", Decimal(cell).normalize())
    except InvalidOperation:
        return ("s", cell)


def same_rows(got: list[list[Cell]], expected: list[list[Cell]], *, ordered: bool) -> bool:
    left = [tuple(_normal(c) for c in row) for row in got]
    right = [tuple(_normal(c) for c in row) for row in expected]
    if ordered:
        return left == right
    return sorted(left, key=repr) == sorted(right, key=repr)


@dataclass
class Outcome:
    sample: Sample
    mode: str | None
    passed: bool
    detail: str = ""


def run_samples(runtime: Runtime, company: Company) -> list[Outcome]:
    values = anchors(runtime, company)
    outcomes = []
    for sample in SAMPLES:
        params = [values[name] for name in sample.anchors]
        try:
            result = run_query(runtime, company, sample.sql, params, max_rows=10_000)
            expected = oracle(runtime, company, sample.sql, params)
        except SageError as error:
            outcomes.append(Outcome(sample, None, False, f"{error.code}: {error.message} {error.detail or ''}"))
            continue
        passed = not result.truncated and same_rows(result.rows, expected, ordered=sample.ordered)
        detail = (
            ""
            if passed
            else f"got {result.rows[:3]}... ({result.row_count}), expected {expected[:3]}... ({len(expected)})"
        )
        outcomes.append(Outcome(sample, result.mode, passed, detail))
    return outcomes
