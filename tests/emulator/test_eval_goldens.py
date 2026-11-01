"""The eval set's golden answers (SPEC.md §15.6) agree three ways on the test bed: the value in
evals/tasks.yaml, a plain-Python reading of the synthetic rows (tests/premium_testbed.py), and the
server's own tools on an emulated install. So a model that answers through the tools can match every
golden, and a golden can't drift from the data. SAGE100_MCP_UPDATE_GOLDENS=1 rewrites the test bed
goldens in place from the plain-Python answers.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
import yaml

from sage100_mcp.query import run_query
from sage100_mcp.runtime import Runtime
from sage100_mcp.views.api import run_named
from tests import emulator
from tests import premium_testbed as testbed
from tests.emulator.conftest import install_at, providex_runtime
from tests.paths import REPO

TASKS = REPO / "evals" / "tasks.yaml"
UPDATE_ENV = "SAGE100_MCP_UPDATE_GOLDENS"
AS_OF = date(2024, 12, 31)
Rows = dict[str, list[dict[str, Any]]]


# --- normal forms, as scripts/run_evals.py compares them -------------------------------------------------


def money(value: Decimal | int) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def number(value: Decimal | int) -> str:
    return format(Decimal(value).normalize(), "f")


def codes(values: Iterable[str]) -> str:
    return ", ".join(sorted({value.strip() for value in values}))


def day(value: Any) -> date | None:
    if value is None or value == testbed.EMPTY:
        return None
    return value.date() if isinstance(value, datetime) else value


def age(invoice: dict[str, Any]) -> int:
    """Days past due on AS_OF; without a due date from the invoice date, without either the oldest (D42)."""
    found = day(invoice.get("InvoiceDueDate")) or day(invoice.get("InvoiceDate"))
    return (AS_OF - found).days if found else 10**6


def biggest(totals: dict[str, Decimal]) -> str:
    return max(totals.items(), key=lambda item: (item[1], item[0]))[0]


# --- the rows, read the way each question means them -----------------------------------------------------


def ar_open(rows: Rows) -> list[dict[str, Any]]:
    return [invoice for invoice in rows["AR_OpenInvoice"] if invoice["Balance"] != 0]


def ar_by_customer(rows: Rows) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for invoice in ar_open(rows):
        totals[invoice["CustomerNo"]] += invoice["Balance"]
    return dict(totals)


def ap_open(rows: Rows) -> list[dict[str, Any]]:
    return [invoice for invoice in rows["AP_OpenInvoice"] if invoice["Balance"] != 0]


def ap_by_vendor(rows: Rows) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for invoice in ap_open(rows):
        totals[invoice["VendorNo"]] += invoice["Balance"]
    return dict(totals)


def so_open_lines(rows: Rows) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Item lines with quantity left to ship on standard and back orders (open_sales_orders' defaults)."""
    orders = {o["SalesOrderNo"]: o for o in rows["SO_SalesOrderHeader"] if o["OrderType"] in ("S", "B")}
    return [
        (orders[line["SalesOrderNo"]], line)
        for line in rows["SO_SalesOrderDetail"]
        if line["SalesOrderNo"] in orders
        and line["ItemType"] not in ("3", "4", "5")
        and line["QuantityOrdered"] - line["QuantityShipped"] > 0
    ]


def po_open_lines(rows: Rows) -> list[tuple[dict[str, Any], dict[str, Any], Decimal]]:
    """Lines with quantity left to receive on new, open, back-ordered and changing POs, with their value."""
    headers = rows["PO_PurchaseOrderHeader"]
    orders = {o["PurchaseOrderNo"]: o for o in headers if o["OrderStatus"] in ("N", "O", "B", "C")}
    found = []
    for line in rows["PO_PurchaseOrderDetail"]:
        left = line["QuantityOrdered"] - line["QuantityReceived"]
        if line["PurchaseOrderNo"] in orders and left > 0:
            found.append((orders[line["PurchaseOrderNo"]], line, (left * line["UnitCost"]).quantize(Decimal("0.01"))))
    return found


def on_hand_by_item(rows: Rows) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for stock in rows["IM_ItemWarehouse"]:
        totals[stock["ItemCode"]] += stock["QuantityOnHand"]
    return dict(totals)


def short_items(rows: Rows) -> set[str]:
    """Sage's available (IM_Options counts purchase orders in: P) below zero, or projected below reorder."""
    found = set()
    for s in rows["IM_ItemWarehouse"]:
        value = {name: s.get(column) or Decimal(0) for name, column in (
            ("onhand", "QuantityOnHand"), ("so", "QuantityOnSalesOrder"), ("bo", "QuantityOnBackOrder"),
            ("po", "QuantityOnPurchaseOrder"), ("wo", "QuantityOnWorkOrder"), ("req", "QuantityRequiredForWO"),
            ("reorder", "ReorderPointQty"))}  # fmt: skip
        available = value["onhand"] + value["po"] - value["so"] - value["bo"] - value["req"]
        projected = value["onhand"] + value["po"] + value["wo"] - value["so"] - value["bo"] - value["req"]
        if available < 0 or projected < value["reorder"]:
            found.add(s["ItemCode"])
    return found


def period_rows(rows: Rows, year: str, period: int) -> list[dict[str, Any]]:
    return [r for r in rows["GL_PeriodPostingHistory"] if r["FiscalYear"] == year and int(r["FiscalPeriod"]) == period]


def ending_balances(rows: Rows, year: str, period: int) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows["GL_PeriodPostingHistory"]:
        if r["FiscalYear"] == year and int(r["FiscalPeriod"]) <= period:
            totals[r["AccountKey"]] += (r.get("BeginningBalance") or 0) + r["DebitAmount"] - r["CreditAmount"]
    return dict(totals)


def sales_by_month(rows: Rows, year: int) -> dict[str, Decimal]:
    headers = {
        (h["InvoiceNo"], h["HeaderSeqNo"]): h
        for h in rows["AR_InvoiceHistoryHeader"]
        if h["InvoiceType"] in ("IN", "CM", "DM") and day(h["InvoiceDate"]) and day(h["InvoiceDate"]).year == year  # type: ignore[union-attr]
    }
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for line in rows["AR_InvoiceHistoryDetail"]:
        header = headers.get((line["InvoiceNo"], line["HeaderSeqNo"]))
        if header is not None:
            totals[f"{day(header['InvoiceDate']):%Y-%m}"] += line["ExtensionAmt"] or 0
    return dict(totals)


def total(values: Iterable[Decimal]) -> Decimal:
    return sum(values, Decimal(0))


def over_credit_limit(emp: Rows) -> int:
    owed = ar_by_customer(emp)
    return sum(1 for c in emp["AR_Customer"] if owed.get(c["CustomerNo"], 0) > c["CreditLimit"])


def largest_order(emp: Rows) -> str:
    return biggest(_sum_by(so_open_lines(emp), lambda order, line: (order["SalesOrderNo"], line["ExtensionAmt"])))


def due_by(lines: Iterable[dict[str, Any]], column: str, last: date) -> int:
    return sum(1 for line in lines if (day(line[column]) or date.max) <= last)


def largest_po_vendor(emp: Rows) -> str:
    return biggest(_sum_by([(o, value) for o, _, value in po_open_lines(emp)], lambda o, value: (o["VendorNo"], value)))


Oracle = Callable[[Rows, Rows], str]
ORACLES: dict[str, Oracle] = {
    "ar-open-total": lambda emp, _: money(total(i["Balance"] for i in ar_open(emp))),
    "ar-open-documents": lambda emp, _: str(len(ar_open(emp))),
    "ar-top-customer": lambda emp, _: biggest(ar_by_customer(emp)),
    "ar-over-60": lambda emp, _: money(total(i["Balance"] for i in ar_open(emp) if age(i) >= 61)),
    "ar-not-yet-due": lambda emp, _: money(total(i["Balance"] for i in ar_open(emp) if age(i) <= 0)),
    "ar-over-credit-limit": lambda emp, _: str(over_credit_limit(emp)),
    "ar-credit-hold": lambda emp, _: codes(c["CustomerNo"] for c in emp["AR_Customer"] if c["CreditHold"] == "Y"),
    "ar-customer-count": lambda emp, _: str(len(emp["AR_Customer"])),
    "ar-inactive": lambda emp, _: str(sum(1 for c in emp["AR_Customer"] if c["CustomerStatus"] == "I")),
    "ar-largest-invoice": lambda emp, _: max(ar_open(emp), key=lambda i: i["Balance"])["InvoiceNo"],
    "ar-customer-balance": lambda emp, _: money(ar_by_customer(emp).get("C0014", Decimal(0))),
    "ar-customers-owing": lambda emp, _: str(sum(1 for owed in ar_by_customer(emp).values() if owed > 0)),
    "ap-open-total": lambda emp, _: money(total(i["Balance"] for i in ap_open(emp))),
    "ap-vendor-count": lambda emp, _: str(len(emp["AP_Vendor"])),
    "ap-top-vendor": lambda emp, _: biggest(ap_by_vendor(emp)),
    "ap-60-plus": lambda emp, _: money(total(i["Balance"] for i in ap_open(emp) if age(i) >= 60)),
    "ap-on-hold": lambda emp, _: money(total(i["Balance"] for i in ap_open(emp) if i["HoldPayment"] == "Y")),
    "ap-1099": lambda emp, _: str(sum(1 for v in emp["AP_Vendor"] if v["Form1099"] == "Y")),
    "pcc-ap-total": lambda _, pcc: money(total(i["Balance"] for i in ap_open(pcc))),
    "pcc-vendor-count": lambda _, pcc: str(len(pcc["AP_Vendor"])),
    "so-open-orders": lambda emp, _: str(len({o["SalesOrderNo"] for o, _ in so_open_lines(emp)})),
    "so-open-value": lambda emp, _: money(total(line["ExtensionAmt"] for _, line in so_open_lines(emp))),
    "so-quotes": lambda emp, _: str(sum(1 for o in emp["SO_SalesOrderHeader"] if o["OrderType"] == "Q")),
    "so-largest-order": lambda emp, _: largest_order(emp),
    "so-promised-by": lambda emp, _: str(
        due_by((line for _, line in so_open_lines(emp)), "PromiseDate", date(2025, 1, 15))
    ),
    "so-customer-orders": lambda emp, _: str(
        len({o["SalesOrderNo"] for o, _ in so_open_lines(emp) if o["CustomerNo"] == "C0003"})
    ),
    "po-open-orders": lambda emp, _: str(len({o["PurchaseOrderNo"] for o, _, _ in po_open_lines(emp)})),
    "po-open-value": lambda emp, _: money(total(value for _, _, value in po_open_lines(emp))),
    "po-top-vendor": lambda emp, _: largest_po_vendor(emp),
    "po-required-by": lambda emp, _: str(
        due_by((line for _, line, _ in po_open_lines(emp)), "RequiredDate", date(2025, 1, 31))
    ),
    "im-item-count": lambda emp, _: str(len(emp["CI_Item"])),
    "im-item-on-hand": lambda emp, _: number(on_hand_by_item(emp).get("ITEM-003", Decimal(0))),
    "im-most-on-hand": lambda emp, _: biggest(on_hand_by_item(emp)),
    "im-short": lambda emp, _: codes(short_items(emp)),
    "gl-period-6-balances": lambda emp, _: "yes" if total(ending_balances(emp, "2024", 6).values()) == 0 else "no",
    "gl-period-6-debits": lambda emp, _: money(total(r["DebitAmount"] for r in period_rows(emp, "2024", 6))),
    "gl-account-count": lambda emp, _: str(len(emp["GL_Account"])),
    "sales-2024": lambda emp, _: money(total(sales_by_month(emp, 2024).values())),
    "sales-best-month": lambda emp, _: biggest(sales_by_month(emp, 2024)),
    "write-refused": lambda emp, _: "REFUSED",
}


def _sum_by(items: Iterable[tuple[Any, Any]], pick: Callable[[Any, Any], tuple[str, Decimal]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for first, second in items:
        key, value = pick(first, second)
        totals[key] += value
    return dict(totals)


# --- the server's own answers -------------------------------------------------------------------------------


class Tools:
    """The server's tools on an emulated install, called as the model would call them."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def view(self, name: str, company: str = "EMP", **args: Any) -> Any:
        return run_named(self.runtime, self.runtime.resolve_company(company), name, args, max_rows=5000)

    def rows(self, sql: str, company: str = "EMP") -> list[list[Any]]:
        found: list[list[Any]] = run_query(self.runtime, self.runtime.resolve_company(company), sql).rows
        return found

    def scalar(self, sql: str, company: str = "EMP") -> Any:
        return self.rows(sql, company)[0][0]

    def column(self, result: Any, name: str) -> list[Any]:
        position = [c.name for c in result.columns].index(name)
        return [row[position] for row in result.rows]

    def pairs(self, result: Any, key: str, value: str) -> dict[str, Decimal]:
        return dict(zip(self.column(result, key), map(Decimal, self.column(result, value)), strict=True))


def server_answers(tools: Tools) -> Iterator[tuple[str, str]]:
    aging = tools.view("ar_aging", as_of="2024-12-31", min_balance="-1000000000")
    customers = tools.pairs(aging, "CustomerNo", "total")
    yield "ar-open-total", money(Decimal(tools.scalar("SELECT SUM(Balance) FROM AR_OpenInvoice")))
    yield "ar-open-documents", str(tools.scalar("SELECT COUNT(*) FROM AR_OpenInvoice WHERE Balance <> 0"))
    yield "ar-top-customer", biggest(customers)
    yield "ar-over-60", money(Decimal(aging.totals["days_61_plus"]))
    yield "ar-not-yet-due", money(Decimal(aging.totals["current"]))
    limits = tools.rows("SELECT CustomerNo, CreditLimit FROM AR_Customer")
    yield "ar-over-credit-limit", str(sum(1 for code, limit in limits if customers.get(code, 0) > Decimal(limit)))
    yield (
        "ar-credit-hold",
        codes(row[0] for row in tools.rows("SELECT CustomerNo FROM AR_Customer WHERE CreditHold = 'Y'")),
    )
    yield "ar-customer-count", str(tools.scalar("SELECT COUNT(*) FROM AR_Customer"))
    yield "ar-inactive", str(tools.scalar("SELECT COUNT(*) FROM AR_Customer WHERE CustomerStatus = 'I'"))
    invoices = tools.view("ar_aging", as_of="2024-12-31", detail="invoice", min_balance="-1000000000")
    yield "ar-largest-invoice", biggest(tools.pairs(invoices, "InvoiceNo", "Balance"))
    yield "ar-customer-balance", money(customers.get("C0014", Decimal(0)))
    yield "ar-customers-owing", str(sum(1 for total in customers.values() if total > 0))
    ap = tools.view("ap_aging", as_of="2024-12-31")
    vendors = tools.pairs(ap, "VendorNo", "total")
    yield "ap-open-total", money(Decimal(ap.totals["total"]))
    yield "ap-vendor-count", str(tools.scalar("SELECT COUNT(*) FROM AP_Vendor"))
    yield "ap-top-vendor", biggest(vendors)
    yield "ap-60-plus", money(sum((Decimal(ap.totals[c]) for c in ("days_60_89", "days_90_119", "days_120_plus")),
                                  Decimal(0)))  # fmt: skip
    yield "ap-on-hold", money(Decimal(ap.totals["on_hold"]))
    yield "ap-1099", str(tools.scalar("SELECT COUNT(*) FROM AP_Vendor WHERE Form1099 = 'Y'"))
    yield "pcc-ap-total", money(Decimal(tools.view("ap_aging", company="PCC", as_of="2024-12-31").totals["total"]))
    yield "pcc-vendor-count", str(tools.scalar("SELECT COUNT(*) FROM AP_Vendor", company="PCC"))
    orders = tools.view("open_sales_orders", lines=False)
    values = tools.pairs(orders, "SalesOrderNo", "open_value")
    yield "so-open-orders", str(orders.row_count)
    yield "so-open-value", money(Decimal(orders.totals["open_value"]))
    yield "so-quotes", str(tools.scalar("SELECT COUNT(*) FROM SO_SalesOrderHeader WHERE OrderType = 'Q'"))
    yield "so-largest-order", biggest(values)
    yield "so-promised-by", str(tools.view("open_sales_orders", promise_before="2025-01-15").row_count)
    yield "so-customer-orders", str(tools.view("open_sales_orders", customer="C0003", lines=False).row_count)
    pos = tools.view("open_purchase_orders", lines=False)
    by_vendor: dict[str, Decimal] = defaultdict(Decimal)
    for vendor, value in zip(tools.column(pos, "VendorNo"), tools.column(pos, "open_value"), strict=True):
        by_vendor[vendor] += Decimal(value)
    yield "po-open-orders", str(pos.row_count)
    yield "po-open-value", money(Decimal(pos.totals["open_value"]))
    yield "po-top-vendor", biggest(dict(by_vendor))
    yield "po-required-by", str(tools.view("open_purchase_orders", required_before="2025-01-31").row_count)
    yield "im-item-count", str(tools.scalar("SELECT COUNT(*) FROM CI_Item"))
    stock = tools.view("inventory_availability")
    on_hand: dict[str, Decimal] = defaultdict(Decimal)
    for item, quantity in zip(tools.column(stock, "ItemCode"), tools.column(stock, "on_hand"), strict=True):
        on_hand[item] += Decimal(quantity)
    yield "im-item-on-hand", number(on_hand.get("ITEM-003", Decimal(0)))
    yield "im-most-on-hand", biggest(dict(on_hand))
    yield "im-short", codes(tools.column(tools.view("inventory_availability", only_short=True), "ItemCode"))
    balance = tools.view("gl_trial_balance", fiscal_year="2024", period="6")
    yield "gl-period-6-balances", "yes" if balance.totals["debit"] == balance.totals["credit"] else "no"
    yield "gl-period-6-debits", money(Decimal(balance.totals["period_debits"]))
    yield "gl-account-count", str(tools.scalar("SELECT COUNT(*) FROM GL_Account"))
    sales = tools.view("sales_history", from_date="2024-01-01", to_date="2024-12-31", group_by="month")
    months = tools.pairs(sales, "group_key", "revenue")
    yield "sales-2024", money(Decimal(sales.totals["revenue"]))
    yield "sales-best-month", biggest(months)
    yield "write-refused", "REFUSED"


# --- the checks -------------------------------------------------------------------------------------------


def load_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = yaml.safe_load(TASKS.read_text(encoding="utf-8"))["tasks"]
    return tasks


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Tools]:
    tmp = tmp_path_factory.mktemp("evals")
    install = install_at(tmp, "pxplus_full")
    emulator.INSTALLS[install.directory.lower()] = install
    runtime = providex_runtime(tmp / "home", install)
    yield Tools(runtime)
    runtime.close()


def test_the_set_has_forty_distinct_tasks_with_oracles() -> None:
    tasks = load_tasks()
    ids = [task["id"] for task in tasks]
    assert len(ids) == len(set(ids)) == 40
    assert set(ids) == set(ORACLES)
    kinds = {"money", "number", "count", "code", "codes", "month", "yesno", "refused"}
    assert {task["answer"] for task in tasks} <= kinds


def test_goldens_agree_with_the_rows_and_the_server(tools: Tools) -> None:
    emp, pcc = testbed.emp_rows(), testbed.pcc_rows()
    expected = {name: oracle(emp, pcc) for name, oracle in ORACLES.items()}
    served = dict(server_answers(tools))
    assert served == expected, {k: (expected[k], served.get(k)) for k in expected if served.get(k) != expected[k]}
    if os.environ.get(UPDATE_ENV):
        update_goldens(expected)
    stored = {task["id"]: str(task["golden"]["testbed"]) for task in load_tasks()}
    assert stored == expected, "evals/tasks.yaml is out of date; rerun with " + UPDATE_ENV + "=1"


def update_goldens(answers: dict[str, str]) -> None:
    """Rewrite each task's golden.testbed in place, keeping the file's comments and layout."""
    text = TASKS.read_text(encoding="utf-8")
    pattern = re.compile(r"(  - id: (?P<id>[\w-]+)\n(?:    .*\n)*?    golden: \{testbed: )(?P<old>'[^']*'|[^,]*)(,)")

    def put(match: re.Match[str]) -> str:
        value = answers[match["id"]].replace("'", "''")
        return f"{match.group(1)}'{value}'{match.group(4)}"

    TASKS.write_text(pattern.sub(put, text), encoding="utf-8")
