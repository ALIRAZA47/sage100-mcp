"""Run the M1 exit gates against a real Premium company (SPEC.md §18), read-only.

    uv run python scripts/verify_premium.py --config C:\\ProgramData\\sage100-mcp\\config.yaml --company EMP

Gate 2: the company's catalog builds from the database in under five minutes (the cache is bypassed).
Gate 3: run_query returns exactly what SQL Server returns for the same question. Each check runs a
plain T-SQL statement directly and the equivalent question through the server's pipeline (guard,
catalog, policy, generated T-SQL), and compares the rows. The questions return counts and codes
only, so the output holds no amounts, names or other company data. Two more checks: a masked column
comes back as *** without its values being fetched, and a write is refused before reaching the
database. Exit status 0 means every check passed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from sage100_mcp.config import ConfigError, load_config
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.query import run_query
from sage100_mcp.runtime import Runtime

# (name, the question as the model would write it, the same question in plain T-SQL)
CHECKS = [
    ("open AR count", "SELECT COUNT(*) AS n FROM AR_OpenInvoice WHERE Balance <> 0", None),
    ("AR by invoice type", "SELECT InvoiceType, COUNT(*) AS n FROM AR_OpenInvoice GROUP BY InvoiceType", None),
    (
        "join and group",
        "SELECT c.CustomerStatus, COUNT(*) AS n FROM AR_Customer c JOIN AR_OpenInvoice i "
        "ON i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo WHERE i.Balance <> 0 "
        "GROUP BY c.CustomerStatus",
        None,
    ),
    (
        "backlog order types",
        "SELECT OrderType, COUNT(*) AS n FROM SO_SalesOrderHeader WHERE OrderType NOT IN ('Q', 'M', 'R') "
        "GROUP BY OrderType",
        None,
    ),
    ("backordered lines", "SELECT COUNT(*) AS n FROM SO_SalesOrderDetail WHERE QuantityBackordered > 0", None),
    ("PO status", "SELECT OrderStatus, COUNT(*) AS n FROM PO_PurchaseOrderHeader GROUP BY OrderStatus", None),
    (
        "ODBC date escape",
        "SELECT COUNT(*) AS n FROM AR_OpenInvoice WHERE InvoiceDate >= {d '2024-01-01'}",
        "SELECT COUNT(*) AS n FROM AR_OpenInvoice WHERE InvoiceDate >= CAST('2024-01-01' AS date)",
    ),
    ("blank dates", "SELECT COUNT(*) AS n FROM AR_Customer WHERE DateLastPayment = '1753-01-01'", None),
    (
        "derived table",
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT ItemCode FROM IM_ItemWarehouse WHERE QuantityOnHand > 0) AS x",
        None,
    ),
    (
        "CTE",
        "WITH open_ar AS (SELECT ARDivisionNo, CustomerNo FROM AR_OpenInvoice WHERE Balance <> 0) "
        "SELECT COUNT(DISTINCT CustomerNo) AS n FROM open_ar",
        None,
    ),
    (
        "T-SQL DATEDIFF",
        "SELECT COUNT(*) AS n FROM AR_OpenInvoice WHERE DATEDIFF(day, InvoiceDate, InvoiceDueDate) > 30",
        None,
    ),
    (
        "UNION",
        "SELECT COUNT(*) AS n FROM (SELECT CustomerNo FROM AR_Customer UNION SELECT CustomerNo FROM AR_OpenInvoice) u",
        None,
    ),
    ("lower-case names", "select count(*) as n from ar_customer where customerstatus = 'A'", None),
    (
        "LIMIT",
        "SELECT COUNT(*) AS n FROM (SELECT ItemCode FROM CI_Item ORDER BY ItemCode LIMIT 5) AS t",
        "SELECT COUNT(*) AS n FROM (SELECT ItemCode FROM CI_Item ORDER BY ItemCode OFFSET 0 ROWS "
        "FETCH NEXT 5 ROWS ONLY) AS t",
    ),
]


def _plain(rows: list[tuple[Any, ...]]) -> list[tuple[str, ...]]:
    return sorted(tuple("" if value is None else str(value).rstrip() for value in row) for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="config file (default: the usual location)")
    parser.add_argument("--company", default="EMP")
    args = parser.parse_args(argv)
    try:
        loaded = load_config(args.config, must_exist=args.config is not None)
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 1
    runtime = Runtime(loaded)
    runtime.discover()
    for name, backend in runtime.backends.items():
        if backend.error is not None:
            print(f"backend {name}: {backend.error.code} {backend.error.message}")
    try:
        company = runtime.resolve_company(args.company)
    except SageError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1
    failures = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {label}{': ' + detail if detail else ''}")

    started = time.perf_counter()
    state = runtime.build(company.code, force=True)
    elapsed = time.perf_counter() - started
    tables = len(state.catalog.tables) if state.catalog else 0
    report(state.catalog is not None and elapsed < 300, "gate 2: catalog build", f"{tables} tables in {elapsed:.1f} s")

    executor = runtime.executor(company)
    timeout = loaded.config.limits.query_timeout_s.sqlserver
    for name, question, plain in CHECKS:
        try:
            direct = executor.query(company.database, plain or question, max_rows=10_000, timeout_s=timeout)
            served = run_query(runtime, company, question, max_rows=10_000)
        except SageError as error:
            report(False, f"gate 3: {name}", f"{error.code} {error.message}")
            continue
        same = _plain(direct.rows) == _plain([tuple(row) for row in served.rows])
        report(same, f"gate 3: {name}", f"{len(served.rows)} row(s), identical" if same else "results differ")

    try:
        masked = run_query(runtime, company, "SELECT VendorNo, TaxpayerIDSocialSecurityNo FROM AP_Vendor")
        values = {row[1] for row in masked.rows}
        fetched = "[AP_Vendor].[TaxpayerIDSocialSecurityNo]" in masked.sent_sql[0]
        report(values <= {"***"} and not fetched, "masking", f"{len(masked.rows)} rows, values never fetched")
    except SageError as error:
        report(error.code == ErrorCode.UNKNOWN_TABLE, "masking", f"{error.code} {error.message}")

    try:
        run_query(runtime, company, "UPDATE AR_Customer SET CreditLimit = 0 WHERE 1 = 0")
        report(False, "writes refused", "the guard let an UPDATE through")
    except SageError as error:
        report(error.code == ErrorCode.WRITE_BLOCKED, "writes refused", error.code)
    runtime.close()
    print("all checks passed" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
