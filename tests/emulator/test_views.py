"""The canned views end to end on an emulated ProvideX install (SPEC.md §12, §15.2).

Every expected figure is worked out here in plain Python from the synthetic test-bed rows the emulator
serves (tests/premium_testbed.py), by the rules docs/decisions.md D37-D45 settle, so the views' SQL is
checked against an independent reading of the same data. tests/premium/test_view_parity.py then holds
the Premium plans to these same rows.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import jsonschema
import pytest
from mcp import Client

from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.runtime import Runtime
from sage100_mcp.server import build_server
from sage100_mcp.views.api import compare_companies, run_named
from sage100_mcp.views.engine import ViewResult
from sage100_mcp.views.lookups import clear_cache
from tests import emulator
from tests import premium_testbed as testbed
from tests.emulator.conftest import install_at, providex_runtime

ROWS = testbed.emp_rows()
AS_OF = testbed.AS_OF
CENT = Decimal("0.01")


@pytest.fixture(scope="module", params=["pxplus_full", "calvin_graham"])
def runtime(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """A full-featured driver, and one that sends most of the work to the hybrid planner."""
    tmp = tmp_path_factory.mktemp(request.param)
    install = install_at(tmp, request.param)
    rt = providex_runtime(tmp / "home", install)
    rt.install = install  # type: ignore[attr-defined]
    yield rt
    rt.close()


@pytest.fixture(autouse=True)
def _registered(runtime: Runtime) -> None:
    """The module-scoped install outlives the per-test INSTALLS reset in conftest.py."""
    install = runtime.install  # type: ignore[attr-defined]
    emulator.INSTALLS[install.directory.lower()] = install
    clear_cache()


def view(runtime: Runtime, name: str, company: str = "EMP", **args: Any) -> ViewResult:
    return run_named(runtime, runtime.resolve_company(company), name, args)


def table(result: ViewResult) -> list[dict[str, Any]]:
    names = [c.name for c in result.columns]
    return [dict(zip(names, row, strict=True)) for row in result.rows]


def money(value: Decimal) -> str:
    rounded = Decimal(value).quantize(CENT, ROUND_HALF_UP)
    return format(rounded if rounded else abs(rounded), "f")  # never -0.00


def quantity(value: Decimal) -> str:
    return format(value.normalize() if value else Decimal(0), "f")


def day(value: Any) -> date | None:
    """The test bed's blank date (1753-01-01) and None both mean no date."""
    if value is None or value == testbed.EMPTY:
        return None
    return value.date() if isinstance(value, datetime) else value


def text(value: Any) -> str:
    return "" if value is None else str(value).rstrip()


# --- AR aging ------------------------------------------------------------------------------------


def aged(row: dict[str, Any], basis: str, as_of: date = AS_OF) -> int | None:
    due, invoiced = day(row.get("InvoiceDueDate")), day(row.get("InvoiceDate"))
    start = (due or invoiced) if basis == "due" else invoiced
    return None if start is None else (as_of - start).days


def bucket(age: int | None, starts: tuple[int, ...]) -> int:
    if age is None:
        return 4  # no date at all: the oldest bucket
    return next((i for i, start in enumerate(starts) if age < start), 4)


def expected_aging(
    invoices: list[dict[str, Any]], key: tuple[str, str], starts: tuple[int, ...], basis: str
) -> dict[tuple[str, str], list[Decimal]]:
    totals: dict[tuple[str, str], list[Decimal]] = defaultdict(lambda: [Decimal(0)] * 6)
    for row in invoices:
        if not row.get("Balance"):
            continue
        sums = totals[(row[key[0]], row[key[1]])]
        sums[bucket(aged(row, basis), starts)] += row["Balance"]
        sums[5] += row["Balance"]
    return dict(totals)


def test_ar_aging_summary(runtime: Runtime) -> None:
    result = view(runtime, "ar_aging", as_of="2024-12-31")
    labels = ["current", "days_1_15", "days_16_30", "days_31_60", "days_61_plus"]
    assert [c.name for c in result.columns] == ["ARDivisionNo", "CustomerNo", "CustomerName", "TelephoneNo",
                                                *labels, "total"]  # fmt: skip
    expected = expected_aging(ROWS["AR_OpenInvoice"], ("ARDivisionNo", "CustomerNo"), testbed.AR_AGING, "due")
    kept = {k: v for k, v in expected.items() if abs(v[5]) >= CENT}
    customers = {(c["ARDivisionNo"], c["CustomerNo"]): c for c in ROWS["AR_Customer"]}
    assert [(r["ARDivisionNo"], r["CustomerNo"]) for r in table(result)] == sorted(kept)
    for row in table(result):
        sums = kept[(row["ARDivisionNo"], row["CustomerNo"])]
        assert [row[label] for label in labels] + [row["total"]] == [money(v) for v in sums]
        customer = customers[(row["ARDivisionNo"], row["CustomerNo"])]
        assert row["CustomerName"] == text(customer.get("CustomerName"))
    grand = sum(r["Balance"] for r in ROWS["AR_OpenInvoice"] if r.get("Balance"))
    assert result.totals["total"] == money(grand)  # §12.1: the grand total is SUM(Balance) WHERE Balance <> 0
    assert sum(Decimal(str(result.totals[label])) for label in labels) == Decimal(str(result.totals["total"]))
    assert result.warnings == []


def test_ar_aging_bucket_edges(runtime: Runtime) -> None:
    """The first day of each category opens it (days 1, 16, 31, 61); blank dates age as §12.1 says."""
    result = view(runtime, "ar_aging", as_of="2024-12-31", customer="C0004", detail="invoice")
    rows = {r["InvoiceNo"]: r for r in table(result)}
    edges = {"E000000": (-5, "current"), "E000001": (0, "current"), "E000002": (1, "days_1_15"),
             "E000003": (15, "days_1_15"), "E000004": (16, "days_16_30"), "E000005": (30, "days_16_30"),
             "E000006": (31, "days_31_60"), "E000007": (60, "days_31_60"), "E000008": (61, "days_61_plus"),
             "E000009": (200, "days_61_plus")}  # fmt: skip
    for invoice, (days, label) in edges.items():
        assert (rows[invoice]["days"], rows[invoice]["bucket"]) == (days, label), invoice
    assert (rows["E000003"]["InvoiceType"], rows["E000003"]["Balance"]) == ("CM", "-103.00")  # stays negative
    assert (rows["E000006"]["InvoiceType"], rows["E000006"]["Balance"]) == ("PP", "-106.00")
    assert (rows["E000098"]["InvoiceDueDate"], rows["E000098"]["days"], rows["E000098"]["bucket"]) == (
        None,
        20,
        "days_16_30",
    )  # no due date: aged from the invoice date
    blank = rows["E000099"]
    assert (blank["InvoiceDate"], blank["InvoiceDueDate"], blank["days"], blank["bucket"]) == (
        None,
        None,
        None,
        "days_61_plus",
    )
    total = sum(r["Balance"] for r in ROWS["AR_OpenInvoice"] if r["CustomerNo"] == "C0004" and r.get("Balance"))
    assert result.totals == {"Balance": money(total)}


def test_ar_aging_by_invoice_date(runtime: Runtime) -> None:
    by_due = table(view(runtime, "ar_aging", as_of="2024-12-31", customer="C0004", detail="invoice"))
    by_invoice = table(
        view(runtime, "ar_aging", as_of="2024-12-31", customer="00-C0004", detail="invoice", age_by="invoice")
    )
    ages = {r["InvoiceNo"]: r["days"] for r in by_invoice}
    for row in by_due:
        if row["InvoiceNo"].startswith("E0000") and row["InvoiceNo"] not in ("E000098", "E000099"):
            assert ages[row["InvoiceNo"]] == row["days"] + 30  # the test bed's invoices fall due in 30 days
    assert ages["E000098"] == 20
    assert ages["E000099"] is None


def test_ar_aging_filters_and_division_warning(runtime: Runtime) -> None:
    everyone = table(view(runtime, "ar_aging", as_of="2024-12-31"))
    big = table(view(runtime, "ar_aging", as_of="2024-12-31", min_balance="5000"))
    assert big == [r for r in everyone if abs(Decimal(r["total"])) >= 5000]
    assert 0 < len(big) < len(everyone)
    divided = view(runtime, "ar_aging", as_of="2024-12-31", division="00")
    assert table(divided) == everyone
    assert divided.warnings[0].startswith("DIVISION_DISABLED: ")  # AR_Options.Divisions is N on EMP


def test_ar_aging_as_of_moves_the_aging_date_only(runtime: Runtime) -> None:
    early = view(runtime, "ar_aging", as_of="2024-07-01")
    late = view(runtime, "ar_aging", as_of="2024-12-31")
    assert early.totals["total"] == late.totals["total"]  # balances are always current (§12.1)
    assert early.totals["days_61_plus"] != late.totals["days_61_plus"]


# --- AP aging ------------------------------------------------------------------------------------


def test_ap_aging(runtime: Runtime) -> None:
    result = view(runtime, "ap_aging", as_of="2024-12-31")
    labels = ["current", "days_30_59", "days_60_89", "days_90_119", "days_120_plus"]
    assert [c.name for c in result.columns][4:] == [*labels, "total", "on_hold"]
    expected = expected_aging(ROWS["AP_OpenInvoice"], ("APDivisionNo", "VendorNo"), testbed.AP_AGING, "due")
    rows = table(result)
    assert [(r["APDivisionNo"], r["VendorNo"]) for r in rows] == sorted(expected)
    held: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for invoice in ROWS["AP_OpenInvoice"]:
        if invoice.get("Balance") and invoice.get("HoldPayment") == "Y":
            held[(invoice["APDivisionNo"], invoice["VendorNo"])] += invoice["Balance"]
    for row in rows:
        key = (row["APDivisionNo"], row["VendorNo"])
        assert [row[label] for label in labels] + [row["total"]] == [money(v) for v in expected[key]]
        assert row["on_hold"] == money(held.get(key, Decimal(0)))
    assert result.warnings == []


def test_ap_aging_edges_and_hold(runtime: Runtime) -> None:
    rows = {r["InvoiceNo"]: r for r in table(view(runtime, "ap_aging", as_of="2024-12-31", detail="invoice"))}
    edges = {"EDGE-00": "current", "EDGE-01": "current", "EDGE-02": "days_30_59", "EDGE-03": "days_30_59",
             "EDGE-04": "days_60_89", "EDGE-05": "days_60_89", "EDGE-06": "days_90_119", "EDGE-07": "days_90_119",
             "EDGE-08": "days_120_plus", "EDGE-09": "days_120_plus"}  # fmt: skip
    assert {n: rows[n]["bucket"] for n in edges} == edges
    assert rows["EDGE-04"]["on_hold"] is True
    assert rows["EDGE-05"]["on_hold"] is False
    released = table(view(runtime, "ap_aging", as_of="2024-12-31", detail="invoice", include_on_hold=False))
    assert {r["InvoiceNo"] for r in released} == {n for n, r in rows.items() if not r["on_hold"]}
    by_invoice = {r["InvoiceNo"]: r for r in table(view(runtime, "ap_aging", as_of="2024-12-31", detail="invoice",
                                                         age_by="invoice"))}  # fmt: skip
    assert by_invoice["EDGE-02"]["days"] == rows["EDGE-02"]["days"] + 30


def test_ap_aging_by_month(runtime: Runtime) -> None:
    """PCC ages AP in calendar months (AgingCategoriesToUseForInv M, categories 1-4)."""
    result = view(runtime, "ap_aging", company="PCC", as_of="2024-12-31", detail="invoice")
    assert [c.name for c in result.columns][7] == "months"
    months = {r["InvoiceNo"]: (r["months"], r["bucket"]) for r in table(result)}
    assert months == {
        "PCC-00": (-1, "current"),  # due next month
        "PCC-01": (0, "current"),
        "PCC-02": (0, "current"),
        "PCC-03": (1, "months_1"),  # due 30 Nov: one calendar month back
        "PCC-04": (1, "months_1"),
        "PCC-05": (2, "months_2"),
        "PCC-06": (3, "months_3"),
        "PCC-07": (4, "months_4_plus"),
        "PCC-08": (23, "months_4_plus"),
    }
    summary = view(runtime, "ap_aging", company="PCC", as_of="2024-12-31")
    assert list(summary.totals)[:5] == ["current", "months_1", "months_2", "months_3", "months_4_plus"]


# --- open sales orders ---------------------------------------------------------------------------


def open_lines(types: tuple[str, ...] = ("S", "B"), *, misc: bool = False) -> list[dict[str, Any]]:
    headers = {h["SalesOrderNo"]: h for h in ROWS["SO_SalesOrderHeader"]}
    found = []
    for line in ROWS["SO_SalesOrderDetail"]:
        header = headers[line["SalesOrderNo"]]
        if header["OrderType"] not in types:
            continue
        remaining = line["QuantityOrdered"] - line["QuantityShipped"]
        kind = line.get("ItemType")
        if kind in ("3", "4", "5") and not misc:
            continue
        if remaining > 0 or (misc and kind in ("3", "4", "5") and not line["QuantityOrdered"]):
            found.append({**header, **line, "open_qty": remaining})
    return sorted(found, key=lambda r: (r["SalesOrderNo"], r["LineKey"]))


def test_open_sales_order_lines(runtime: Runtime) -> None:
    result = view(runtime, "open_sales_orders")
    rows = table(result)
    expected = open_lines()
    assert [(r["SalesOrderNo"], r["LineKey"]) for r in rows] == [(e["SalesOrderNo"], e["LineKey"]) for e in expected]
    for row, line in zip(rows, expected, strict=True):
        assert row["open_qty"] == quantity(line["QuantityOrdered"] - line["QuantityShipped"])
        assert row["QuantityBackordered"] == quantity(line.get("QuantityBackordered") or Decimal(0))
        assert row["open_value"] == money(line["ExtensionAmt"])  # the stored, discounted extension (D38)
    assert result.totals == {"open_value": money(sum((e["ExtensionAmt"] for e in expected), Decimal(0)))}
    assert {r["OrderType"] for r in rows} == {"S", "B"}  # quotes, master and repeating orders stay out
    partial = [r for r in rows if r["QuantityShipped"] != "0"]
    assert partial
    assert all(r["OrderType"] == "B" for r in partial)
    discounted = [e for e in expected if e.get("LineDiscountPercent")]
    assert discounted
    assert all(e["ExtensionAmt"] < (e["open_qty"] * e["UnitPrice"]).quantize(CENT) for e in discounted)


def test_open_sales_orders_options(runtime: Runtime) -> None:
    with_misc = table(view(runtime, "open_sales_orders", include_misc=True))
    assert {r["ItemCode"] for r in with_misc} - {r["ItemCode"] for r in table(view(runtime, "open_sales_orders"))} == {
        "/C",
        "/SETUP",
    }
    comment = next(r for r in with_misc if r["ItemCode"] == "/C")
    assert (comment["open_qty"], comment["open_value"]) == ("0", "0.00")
    every = table(view(runtime, "open_sales_orders", order_types=["S", "B", "Q", "M", "R"]))
    assert {r["OrderType"] for r in every} == {"S", "B", "Q", "M", "R"}
    promised = table(view(runtime, "open_sales_orders", promise_before="2024-11-15"))
    assert promised
    assert all(r["PromiseDate"] is not None and r["PromiseDate"] <= "2024-11-15" for r in promised)
    one = table(view(runtime, "open_sales_orders", customer=open_lines()[0]["CustomerNo"]))
    assert one
    assert {r["CustomerNo"] for r in one} == {open_lines()[0]["CustomerNo"]}


def test_open_sales_orders_by_order(runtime: Runtime) -> None:
    lines = view(runtime, "open_sales_orders")
    orders = view(runtime, "open_sales_orders", lines=False)
    per_order: dict[str, list[Decimal]] = defaultdict(list)
    for row in table(lines):
        per_order[row["SalesOrderNo"]].append(Decimal(row["open_value"]))
    assert {r["SalesOrderNo"]: (r["open_lines"], r["open_value"]) for r in table(orders)} == {
        number: (len(values), money(sum(values, Decimal(0)))) for number, values in per_order.items()
    }
    assert orders.totals["open_value"] == lines.totals["open_value"]
    headers = {h["SalesOrderNo"]: h for h in ROWS["SO_SalesOrderHeader"]}
    for row in table(orders):
        h = headers[row["SalesOrderNo"]]
        merchandise = h["TaxableAmt"] + h["NonTaxableAmt"]
        assert [row["merchandise"], row["freight"], row["sales_tax"], row["discount"], row["order_total"]] == [
            money(merchandise),
            money(h["FreightAmt"]),
            money(h["SalesTaxAmt"]),
            money(h["DiscountAmt"]),
            money(merchandise + h["FreightAmt"] + h["SalesTaxAmt"] - h["DiscountAmt"]),
        ]


def test_order_totals_add_up_to_the_customers_open_order_amount(runtime: Runtime) -> None:
    """D49: with charge and comment lines kept, each customer's order totals sum to AR_Customer.OpenOrderAmt."""
    orders = table(view(runtime, "open_sales_orders", lines=False, include_misc=True))
    per_customer: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for row in orders:
        per_customer[(row["ARDivisionNo"], row["CustomerNo"])] += Decimal(row["order_total"])
    stored = {(c["ARDivisionNo"], c["CustomerNo"]): c["OpenOrderAmt"] for c in ROWS["AR_Customer"] if c["OpenOrderAmt"]}
    assert dict(per_customer) == stored
    merchandise = sum((Decimal(r["merchandise"]) for r in orders), Decimal(0))
    assert merchandise == sum((Decimal(r["open_value"]) for r in orders), Decimal(0))  # the header is its lines


def test_committed_stock_matches_the_open_orders(runtime: Runtime) -> None:
    """D38: IM_ItemWarehouse's sales and back order quantities are the open quantities of those orders."""
    committed: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for row in table(view(runtime, "open_sales_orders", order_types=["S"])):
        if row["ItemType"] == "1":
            committed[(row["ItemCode"], row["WarehouseCode"])] += Decimal(row["open_qty"])
    stock = {(r["ItemCode"], r["WarehouseCode"]): r for r in table(view(runtime, "inventory_availability"))}
    for key, row in stock.items():
        assert Decimal(row["on_sales_order"]) == committed.get(key, Decimal(0)), key


# --- open purchase orders ------------------------------------------------------------------------


def test_open_purchase_orders(runtime: Runtime) -> None:
    result = view(runtime, "open_purchase_orders")
    statuses = {h["PurchaseOrderNo"]: h["OrderStatus"] for h in ROWS["PO_PurchaseOrderHeader"]}
    expected = sorted(
        (line for line in ROWS["PO_PurchaseOrderDetail"]
         if statuses[line["PurchaseOrderNo"]] in ("N", "O", "B", "C")
         and line["QuantityOrdered"] - line["QuantityReceived"] > 0),
        key=lambda line: (line["PurchaseOrderNo"], line["LineKey"]),
    )  # fmt: skip
    rows = table(result)
    assert [(r["PurchaseOrderNo"], r["LineKey"]) for r in rows] == [
        (e["PurchaseOrderNo"], e["LineKey"]) for e in expected
    ]
    values = [((e["QuantityOrdered"] - e["QuantityReceived"]) * e["UnitCost"]).quantize(CENT, ROUND_HALF_UP)
              for e in expected]  # fmt: skip
    assert [r["open_value"] for r in rows] == [money(v) for v in values]
    assert {r["OrderStatus"] for r in rows} == {"N", "O", "B", "C"}  # never R (received) or X (completed)
    assert result.totals == {"open_value": money(sum(values, Decimal(0)))}
    by_po = view(runtime, "open_purchase_orders", lines=False)
    assert by_po.totals == result.totals
    assert [r["open_lines"] for r in table(by_po)] == [
        sum(1 for r in rows if r["PurchaseOrderNo"] == po["PurchaseOrderNo"]) for po in table(by_po)
    ]


# --- inventory -----------------------------------------------------------------------------------


def test_inventory_availability_follows_im_options(runtime: Runtime) -> None:
    """IM_Options.QuantityAvailableToInclude is P on EMP: purchase orders count as available supply."""
    rows = table(view(runtime, "inventory_availability"))
    stock = {(s["ItemCode"], s["WarehouseCode"]): s for s in ROWS["IM_ItemWarehouse"]}
    assert len(rows) == len(stock)
    for row in rows:
        s = stock[(row["ItemCode"], row["WarehouseCode"])]

        def q(name: str, s: dict[str, Any] = s) -> Decimal:
            return Decimal(s.get(name) or 0)

        available = q("QuantityOnHand") + q("QuantityOnPurchaseOrder") - q("QuantityOnSalesOrder")
        available -= q("QuantityOnBackOrder") + q("QuantityRequiredForWO")
        projected = available + q("QuantityOnWorkOrder")
        assert row["available"] == quantity(available)  # QuantityInShipping is never subtracted (D40)
        assert row["projected"] == quantity(projected)
        assert row["short"] is (available < 0 or projected < q("ReorderPointQty"))
    short = table(view(runtime, "inventory_availability", only_short=True))
    assert short == [r for r in rows if r["short"]]


def test_inventory_item_prefix_is_literal(runtime: Runtime) -> None:
    codes = sorted({s["ItemCode"] for s in ROWS["IM_ItemWarehouse"]})
    prefix = codes[0][:-1]
    rows = table(view(runtime, "inventory_availability", item=prefix))
    assert {r["ItemCode"] for r in rows} == {c for c in codes if c.startswith(prefix)}
    wild = prefix[:-1] + "_"  # LIKE would read _ as any character; the view must not
    assert table(view(runtime, "inventory_availability", item=wild)) == [
        r for r in rows if r["ItemCode"].startswith(wild)
    ]


# --- trial balance -------------------------------------------------------------------------------


def expected_balances(year: str, period: str) -> dict[str, list[Decimal]]:
    """Account -> [beginning, period debits, period credits, ending], by D37's reading of the history."""
    accounts = {a["AccountKey"]: a["Account"] for a in ROWS["GL_Account"]}
    found: dict[str, list[Decimal]] = {a: [Decimal(0)] * 4 for a in accounts.values()}
    for h in ROWS["GL_PeriodPostingHistory"]:
        if h["FiscalYear"] != year or h["FiscalPeriod"] > period:
            continue
        sums = found[accounts[h["AccountKey"]]]
        net = h["BeginningBalance"] + h["DebitAmount"] - h["CreditAmount"]
        if h["FiscalPeriod"] < period:
            sums[0] += net
        else:
            sums[0] += h["BeginningBalance"]
            sums[1] += h["DebitAmount"]
            sums[2] += h["CreditAmount"]
        sums[3] += net
    return found


@pytest.mark.parametrize(("year", "period"), [("2024", "12"), ("2024", "01"), ("2023", "06")])
def test_trial_balance(runtime: Runtime, year: str, period: str) -> None:
    result = view(runtime, "gl_trial_balance", fiscal_year=year, period=period)
    expected = {a: v for a, v in expected_balances(year, period).items() if any(v)}
    rows = table(result)
    assert [r["Account"] for r in rows] == sorted(expected)
    for row in rows:
        begin, debits, credits, ending = expected[row["Account"]]
        assert [row["beginning_balance"], row["period_debits"], row["period_credits"], row["ending_balance"]] == [
            money(begin),
            money(debits),
            money(credits),
            money(ending),
        ]
        assert (row["debit"], row["credit"]) == (money(max(ending, Decimal(0))), money(max(-ending, Decimal(0))))
    assert result.totals["debit"] == result.totals["credit"]  # the test bed's ledger balances
    assert result.totals["period_debits"] == result.totals["period_credits"]
    assert not any(w.startswith("TB_OUT_OF_BALANCE") for w in result.warnings)


def test_trial_balance_carries_balances_into_the_next_year(runtime: Runtime) -> None:
    """D37: period 01's BeginningBalance is the prior year's closing balance for balance-sheet accounts,
    with net income closed into retained earnings; income and expense accounts start the year at zero."""
    closing = {r["Account"]: Decimal(r["ending_balance"]) for r in table(
        view(runtime, "gl_trial_balance", fiscal_year="2023", period="12", include_zero=True))}  # fmt: skip
    opening = {r["Account"]: Decimal(r["beginning_balance"]) for r in table(
        view(runtime, "gl_trial_balance", fiscal_year="2024", period="01", include_zero=True))}  # fmt: skip
    kinds = {f"{main}-00-00": kind for main, kind in testbed.GL_KINDS.items()}
    income = sum(closing[a] for a, kind in kinds.items() if kind == "I")
    for account, kind in kinds.items():
        if kind == "I":
            assert opening[account] == 0, account
        elif account == "3000-00-00":
            assert opening[account] == closing[account] + income
        else:
            assert opening[account] == closing[account], account


def test_trial_balance_zero_rows_and_period_check(runtime: Runtime) -> None:
    some = view(runtime, "gl_trial_balance", fiscal_year="2024", period="12")
    every = view(runtime, "gl_trial_balance", fiscal_year="2024", period="12", include_zero=True)
    assert every.row_count == len(ROWS["GL_Account"]) >= some.row_count
    empty = view(runtime, "gl_trial_balance", fiscal_year="2030", period="1", include_zero=False)
    assert empty.rows == []
    assert empty.totals["debit"] == "0.00"


# --- sales history -------------------------------------------------------------------------------


def sold(start: date, end: date) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    headers = {(h["InvoiceNo"], h["HeaderSeqNo"]): h for h in ROWS["AR_InvoiceHistoryHeader"]}
    found = []
    for line in ROWS["AR_InvoiceHistoryDetail"]:
        header = headers[(line["InvoiceNo"], line["HeaderSeqNo"])]
        if header["InvoiceType"] in ("IN", "CM", "DM") and start <= day(header["InvoiceDate"]) <= end:  # type: ignore[operator]
            found.append((header, line))
    return found


@pytest.mark.parametrize("group_by", ["customer", "item", "salesperson", "product_line", "month"])
def test_sales_history(runtime: Runtime, group_by: str) -> None:
    result = view(runtime, "sales_history", from_date="2024-01-01", to_date="2024-12-31", group_by=group_by)
    pairs = sold(date(2024, 1, 1), date(2024, 12, 31))
    revenue = sum(line["ExtensionAmt"] for _, line in pairs)
    cost = sum((line["QuantityShipped"] * line["UnitCost"]).quantize(CENT, ROUND_HALF_UP) for _, line in pairs)
    assert result.totals == {"revenue": money(revenue), "cost": money(cost), "margin": money(revenue - cost)}
    invoices = len({(h["InvoiceNo"], h["HeaderSeqNo"]) for h, _ in pairs})
    counted = sum(r["invoices"] for r in table(result))
    if group_by in ("customer", "salesperson", "month"):  # each invoice falls in exactly one group
        assert counted == invoices
    else:  # an invoice with several items or product lines counts once in each
        assert counted >= invoices
    keys = [r["group_key"] for r in table(result)]
    assert keys == sorted(keys)
    if group_by == "month":
        assert all(len(k) == 7 and k[4] == "-" for k in keys)
    if group_by == "customer":
        assert all("-" in k for k in keys)


def test_credit_memos_reduce_sales_once(runtime: Runtime) -> None:
    """D41: credit memo lines are stored negative, so they count as they stand."""
    pairs = sold(date(2024, 1, 1), date(2024, 12, 31))
    memos = [line for header, line in pairs if header["InvoiceType"] == "CM"]
    assert memos
    assert all(line["ExtensionAmt"] < 0 for line in memos)
    by_customer = table(view(runtime, "sales_history", from_date="2024-01-01", to_date="2024-12-31"))
    assert sum(Decimal(r["revenue"]) for r in by_customer) == sum(line["ExtensionAmt"] for _, line in pairs)


def test_sales_history_range_is_inclusive(runtime: Runtime) -> None:
    dates = sorted(
        d for h in ROWS["AR_InvoiceHistoryHeader"] if h["InvoiceType"] == "IN" and (d := day(h["InvoiceDate"]))
    )
    first = dates[0]
    assert first is not None
    one_day = view(runtime, "sales_history", from_date=first.isoformat(), to_date=first.isoformat())
    assert Decimal(str(one_day.totals["revenue"])) == sum(line["ExtensionAmt"] for _, line in sold(first, first))
    with pytest.raises(SageError) as raised:
        view(runtime, "sales_history", from_date="2024-12-31", to_date="2024-01-01")
    assert raised.value.code == ErrorCode.PARSE_ERROR


# --- lookups -------------------------------------------------------------------------------------


def test_customer_lookup(runtime: Runtime) -> None:
    result = view(runtime, "customer_lookup", query="cafe UNICODE")
    rows = table(result)
    assert [r["CustomerNo"] for r in rows] == ["C0013"]
    balance = sum(r["Balance"] for r in ROWS["AR_OpenInvoice"] if r["CustomerNo"] == "C0013" and r.get("Balance"))
    assert rows[0]["open_balance"] == money(balance)
    assert rows[0]["CustomerName"] == "Café Ünicode Gmbh"
    exact = table(view(runtime, "customer_lookup", query="c0004"))
    assert exact[0]["CustomerNo"] == "C0004"
    dates = [day(r["InvoiceDate"]) for r in ROWS["AR_OpenInvoice"] if r["CustomerNo"] == "C0004"]
    assert exact[0]["last_invoice_date"] == max(d for d in dates if d).isoformat()


def test_lookups_cap_matches(runtime: Runtime) -> None:
    result = view(runtime, "customer_lookup", query="C00")
    assert result.row_count == 20
    assert result.truncated
    assert result.warnings[0].startswith("RESULT_TRUNCATED: ")
    with pytest.raises(SageError) as raised:
        view(runtime, "customer_lookup", query="  ")
    assert raised.value.code == ErrorCode.PARSE_ERROR


def test_vendor_and_item_lookup(runtime: Runtime) -> None:
    vendor = table(view(runtime, "vendor_lookup", query="V000002"))[0]
    owed = sum(r["Balance"] for r in ROWS["AP_OpenInvoice"] if r["VendorNo"] == "V000002" and r.get("Balance"))
    assert vendor["open_balance"] == money(owed)
    code = ROWS["IM_ItemWarehouse"][0]["ItemCode"]
    item = table(view(runtime, "item_lookup", query=code))[0]
    stock = [s for s in ROWS["IM_ItemWarehouse"] if s["ItemCode"] == code]
    assert item["warehouses"] == len(stock)
    assert item["on_hand"] == quantity(sum((Decimal(s.get("QuantityOnHand") or 0) for s in stock), Decimal(0)))
    availability = table(view(runtime, "inventory_availability", item=code))
    assert item["available"] == quantity(
        sum((Decimal(r["available"]) for r in availability if r["ItemCode"] == code), Decimal(0))
    )


# --- across companies, errors and limits ---------------------------------------------------------


def test_compare_companies(runtime: Runtime) -> None:
    both = compare_companies(runtime, "ap_aging", {"as_of": "2024-12-31"}, ["emp", "PCC"])
    assert both.company == "EMP,PCC"
    assert both.columns[0].name == "company"
    assert both.columns[5].name == "column_4"
    assert any(w.startswith("COLUMNS_DIFFER: EMP columns: ") for w in both.warnings)
    assert {row[0] for row in both.rows} == {"EMP", "PCC"}
    assert both.totals == {}
    gl = compare_companies(runtime, "gl_trial_balance", {"fiscal_year": "2024", "period": "12"}, ["EMP", "PCC"])
    emp = view(runtime, "gl_trial_balance", fiscal_year="2024", period="12")
    assert gl.totals["debit"] == emp.totals["debit"]  # PCC's ledger has no history
    with pytest.raises(SageError) as raised:
        compare_companies(runtime, "ar_aging", {}, ["EMP"])
    assert raised.value.code == ErrorCode.PARSE_ERROR
    with pytest.raises(SageError) as raised:
        compare_companies(runtime, "ar_aging", {}, ["EMP", "ZZZ"])
    assert raised.value.code == ErrorCode.COMPANY_NOT_FOUND


def test_missing_module(runtime: Runtime) -> None:
    with pytest.raises(SageError) as raised:
        view(runtime, "ar_aging", company="PCC")
    assert raised.value.code == ErrorCode.MODULE_NOT_INSTALLED
    assert raised.value.choices == ["EMP"]  # ABC is hidden


def test_rows_shown_stop_at_max_rows_but_totals_do_not(runtime: Runtime) -> None:
    every = view(runtime, "ar_aging", as_of="2024-12-31", detail="invoice")
    few = run_named(runtime, runtime.resolve_company("EMP"), "ar_aging", {"as_of": "2024-12-31", "detail": "invoice"},
                    max_rows=5)  # fmt: skip
    assert (few.row_count, few.truncated, few.rows) == (5, True, every.rows[:5])
    assert few.totals == every.totals
    assert any(w.startswith("RESULT_TRUNCATED: Only the first 5 of") for w in few.warnings)


def test_pulls_reach_the_driver_filtered(runtime: Runtime) -> None:
    """The customer filter goes into the AR_OpenInvoice read, and order lines are read by order key."""
    one = view(runtime, "ar_aging", as_of="2024-12-31", customer="C0004")
    assert any("FROM AR_OpenInvoice WHERE CustomerNo = " in sql for sql in one.sent_sql), one.sent_sql
    orders = view(runtime, "open_sales_orders")
    assert any("FROM SO_SalesOrderDetail WHERE SalesOrderNo IN (" in sql for sql in orders.sent_sql), orders.sent_sql


@pytest.mark.anyio
async def test_view_tools_over_mcp(runtime: Runtime) -> None:
    """Each view tool's structuredContent validates against its outputSchema, on real rows."""
    calls: dict[str, dict[str, Any]] = {
        "ar_aging": {"as_of": "2024-12-31", "company": "EMP"},
        "ap_aging": {"detail": "invoice", "company": "EMP"},
        "open_sales_orders": {"order_types": ["S"], "company": "EMP"},
        "open_purchase_orders": {"lines": False, "company": "EMP"},
        "inventory_availability": {"only_short": True, "company": "EMP"},
        "gl_trial_balance": {"fiscal_year": 2024, "period": 12, "company": "EMP"},
        "sales_history": {"from_date": "2024-01-01", "to_date": "2024-06-30", "group_by": "month", "company": "EMP"},
        "customer_lookup": {"query": "o'brien", "company": "EMP"},
        "vendor_lookup": {"query": "V0", "company": "EMP"},
        "item_lookup": {"query": "a", "company": "EMP"},
        "compare_companies": {"view": "vendor_lookup", "args": {"query": "vendor"}, "companies": ["EMP", "PCC"]},
    }
    async with Client(build_server(runtime.loaded, runtime)) as client:
        schemas = {t.name: t.output_schema for t in (await client.list_tools()).tools}
        for tool, arguments in calls.items():
            outcome = await client.call_tool(tool, arguments)
            assert outcome.is_error is False, (tool, outcome.content)
            assert outcome.structured_content is not None
            schema = schemas[tool]
            assert schema is not None
            jsonschema.validate(outcome.structured_content, schema)
        bad = await client.call_tool("ar_aging", {"as_of": "yesterday", "company": "EMP"})
        assert bad.is_error is True
        assert bad.structured_content is not None
        assert bad.structured_content["code"] == "PARSE_ERROR"
