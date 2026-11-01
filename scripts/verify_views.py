"""Reconcile the canned views to the figures Sage keeps itself, on a real company (SPEC.md §12, §18), read-only.

    uv run python scripts/verify_views.py --config C:\\ProgramData\\sage100-mcp\\config.yaml --company ABC

Sage maintains some of the views' figures on its master records as it posts and ages. Each check runs a
view through the server, as its tool does, and compares it row by row, to the cent, with what Sage keeps:

- ar_aging with AR_Customer.CurrentBalance and AgingCategory1-4, as of each customer's DateLastAging;
- open_sales_orders, one row per order, with AR_Customer.OpenOrderAmt;
- ap_aging with AP_Vendor.BalanceDue;
- inventory_availability's on-hand quantities with CI_Item.TotalQuantityOnHand;
- gl_trial_balance: in balance in every period of the current fiscal year whose posting history balances.

A customer whose open invoices changed on or after its aging date, or whose stored aging no longer adds up
to its open invoices, is counted but not compared. The output holds counts and codes only, never amounts or
names. Exit status 0 means every comparison agreed. docs/decisions.md (D49) records what this found on
MAS_EMP; it works on ProvideX and Premium companies alike.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sage100_mcp.companies import Company
from sage100_mcp.config import ConfigError, load_config
from sage100_mcp.errors import SageError
from sage100_mcp.query import run_query
from sage100_mcp.runtime import Runtime
from sage100_mcp.views.api import run_named
from sage100_mcp.views.engine import ViewResult

Key = tuple[str, ...]
AGING_DATES = 10  # the most common DateLastAging values compared; the rest are counted
LISTED = 10  # disagreeing keys printed per check


class TooMany(Exception):
    """A read that would be truncated: the comparison would be incomplete."""


@dataclass
class Outcome:
    check: str
    compared: int = 0
    agreed: int = 0
    failed: list[str] = field(default_factory=list)  # the keys (codes) that disagree
    notes: list[str] = field(default_factory=list)  # what was counted but not compared, and why

    @property
    def passed(self) -> bool:
        return not self.failed

    def line(self) -> str:
        unit = "row" if self.compared == 1 else "rows"
        text = f"{self.agreed} of {self.compared} {unit} agree"
        if self.failed:
            text += f"; disagreeing: {', '.join(self.failed[:LISTED])}{' ...' if len(self.failed) > LISTED else ''}"
        if self.notes:
            text += f" (not compared: {'; '.join(self.notes)})"
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.check}: {text}"


def _amount(value: Any) -> Decimal:
    return Decimal(0) if value in (None, "") else Decimal(str(value))


def _key(*values: Any) -> Key:
    return tuple(str(value or "").strip().upper() for value in values)


def _rows(runtime: Runtime, company: Company, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    result = run_query(runtime, company, sql, params, max_rows=runtime.config.limits.max_rows_hard)
    if result.truncated:
        raise TooMany(
            f"more than {runtime.config.limits.max_rows_hard:,} rows from {sql.split(' FROM ')[1].split()[0]}"
        )
    names = [c.name for c in result.columns]
    return [dict(zip(names, row, strict=True)) for row in result.rows]


def _view(runtime: Runtime, company: Company, name: str, args: dict[str, Any]) -> ViewResult:
    result = run_named(runtime, company, name, args, max_rows=runtime.config.limits.max_rows_hard)
    if result.truncated:
        raise TooMany(f"{name} has more than {result.row_count:,} rows")
    return result


def _named(result: ViewResult) -> list[dict[str, Any]]:
    names = [c.name for c in result.columns]
    return [dict(zip(names, row, strict=True)) for row in result.rows]


def _compare(outcome: Outcome, mine: dict[Key, Decimal], kept: dict[Key, Decimal]) -> Outcome:
    for key in sorted(set(mine) | set(kept)):
        outcome.compared += 1
        if mine.get(key, Decimal(0)) == kept.get(key, Decimal(0)):
            outcome.agreed += 1
        else:
            outcome.failed.append("-".join(key))
    return outcome


def check_aging(runtime: Runtime, company: Company) -> Outcome:
    outcome = Outcome("ar_aging vs AR_Customer's stored aging")
    stored = _rows(
        runtime,
        company,
        "SELECT ARDivisionNo, CustomerNo, CurrentBalance, AgingCategory1, AgingCategory2, AgingCategory3, "
        "AgingCategory4, DateLastAging FROM AR_Customer",
    )
    by_date: dict[str, dict[Key, list[Decimal]]] = defaultdict(dict)
    for row in stored:
        buckets = [_amount(row[c]) for c in ("CurrentBalance", "AgingCategory1", "AgingCategory2",
                                             "AgingCategory3", "AgingCategory4")]  # fmt: skip
        if row["DateLastAging"]:
            by_date[str(row["DateLastAging"])][_key(row["ARDivisionNo"], row["CustomerNo"])] = buckets
    dates = sorted(by_date, key=lambda day: (-len(by_date[day]), day))
    changed = out_of_step = 0
    for day in dates[:AGING_DATES]:
        touched = {
            _key(r["ARDivisionNo"], r["CustomerNo"])
            for r in _rows(
                runtime,
                company,
                "SELECT DISTINCT ARDivisionNo, CustomerNo FROM AR_OpenInvoice "
                "WHERE DateUpdated >= ? OR DateCreated >= ? OR PostingDate >= ?",
                [day, day, day],
            )
        }
        aged = _view(runtime, company, "ar_aging", {"as_of": day, "min_balance": "0"})
        mine = {_key(row[0], row[1]): [_amount(value) for value in row[4:9]] for row in aged.rows}
        for key, kept in by_date[day].items():
            view = mine.get(key, [Decimal(0)] * 5)
            if not any(kept) and not any(view):
                continue  # nothing owed on either side
            if key in touched:
                changed += 1
            elif sum(view) != sum(kept):
                out_of_step += 1
            else:
                outcome.compared += 1
                if view == kept:
                    outcome.agreed += 1
                else:
                    outcome.failed.append("-".join(key))
    if changed:
        outcome.notes.append(f"{changed} with invoices changed on or after their aging date")
    if out_of_step:
        outcome.notes.append(f"{out_of_step} whose stored aging no longer adds up to their open invoices")
    skipped = sum(len(by_date[day]) for day in dates[AGING_DATES:])
    if skipped:
        outcome.notes.append(f"{skipped} aged on less common dates")
    return outcome


def check_open_orders(runtime: Runtime, company: Company) -> Outcome:
    orders = _view(runtime, company, "open_sales_orders", {"lines": False, "include_misc": True,
                                                           "order_types": ["S", "B"]})  # fmt: skip
    mine: dict[Key, Decimal] = defaultdict(Decimal)
    for row in _named(orders):
        mine[_key(row["ARDivisionNo"], row["CustomerNo"])] += _amount(row["order_total"])
    kept = {
        _key(r["ARDivisionNo"], r["CustomerNo"]): _amount(r["OpenOrderAmt"])
        for r in _rows(
            runtime, company, "SELECT ARDivisionNo, CustomerNo, OpenOrderAmt FROM AR_Customer WHERE OpenOrderAmt <> 0"
        )
    }
    return _compare(
        Outcome("open_sales_orders vs AR_Customer.OpenOrderAmt"), {k: v for k, v in mine.items() if v}, kept
    )


def check_vendors(runtime: Runtime, company: Company) -> Outcome:
    aging = _view(runtime, company, "ap_aging", {})
    mine = {_key(row["APDivisionNo"], row["VendorNo"]): _amount(row["total"]) for row in _named(aging)}
    kept = {
        _key(r["APDivisionNo"], r["VendorNo"]): _amount(r["BalanceDue"])
        for r in _rows(
            runtime, company, "SELECT APDivisionNo, VendorNo, BalanceDue FROM AP_Vendor WHERE BalanceDue <> 0"
        )
    }
    return _compare(Outcome("ap_aging vs AP_Vendor.BalanceDue"), {k: v for k, v in mine.items() if v}, kept)


def check_items(runtime: Runtime, company: Company) -> Outcome:
    outcome = Outcome("inventory_availability vs CI_Item.TotalQuantityOnHand")
    mine: dict[Key, Decimal] = defaultdict(Decimal)
    warehouses = _rows(runtime, company, "SELECT DISTINCT WarehouseCode FROM IM_ItemWarehouse")
    for warehouse in sorted({str(r["WarehouseCode"] or "").strip() for r in warehouses} - {""}):
        for row in _named(_view(runtime, company, "inventory_availability", {"warehouse": warehouse})):
            mine[_key(row["ItemCode"])] += _amount(row["on_hand"])
    kept = {
        _key(r["ItemCode"]): _amount(r["TotalQuantityOnHand"])
        for r in _rows(
            runtime, company, "SELECT ItemCode, TotalQuantityOnHand FROM CI_Item WHERE TotalQuantityOnHand <> 0"
        )
    }
    return _compare(outcome, {k: v for k, v in mine.items() if v}, kept)


def check_trial_balance(runtime: Runtime, company: Company) -> Outcome:
    outcome = Outcome("gl_trial_balance in balance, this fiscal year")
    _, profile = runtime.catalog(company)
    current = profile.current_periods.get("GL")
    if not current:
        outcome.notes.append("GL_Options names no current period")
        return outcome
    year, period = current.split("-")
    history = _rows(
        runtime,
        company,
        "SELECT FiscalPeriod, BeginningBalance, DebitAmount, CreditAmount FROM GL_PeriodPostingHistory "
        "WHERE FiscalYear = ?",
        [year],
    )
    net: dict[int, Decimal] = defaultdict(Decimal)
    for row in history:
        amount = _amount(row["BeginningBalance"]) + _amount(row["DebitAmount"]) - _amount(row["CreditAmount"])
        net[int(str(row["FiscalPeriod"]))] += amount
    unbalanced = []
    for number in range(1, int(period) + 1):
        if sum((value for p, value in net.items() if p <= number), Decimal(0)):
            unbalanced.append(f"{number:02d}")  # Sage's own history does not balance through this period
            continue
        result = _view(runtime, company, "gl_trial_balance", {"fiscal_year": year, "period": number})
        outcome.compared += 1
        if result.totals.get("debit") == result.totals.get("credit"):
            outcome.agreed += 1
        else:
            outcome.failed.append(f"{year}-{number:02d}")
    if unbalanced:
        outcome.notes.append(f"periods {', '.join(unbalanced)}, whose posting history is itself out of balance")
    return outcome


CHECKS = [
    ("AR", check_aging),
    ("SO", check_open_orders),
    ("AP", check_vendors),
    ("IM", check_items),
    ("GL", check_trial_balance),
]


def reconcile(runtime: Runtime, company: Company) -> list[Outcome]:
    """Every check the company's modules allow; a check that can't run fails with the reason."""
    _, profile = runtime.catalog(company)
    outcomes = []
    for module, check in CHECKS:
        if module not in profile.modules:
            continue
        try:
            outcomes.append(check(runtime, company))
        except (SageError, TooMany) as error:
            reason = f"{error.code}: {error.message}" if isinstance(error, SageError) else str(error)
            outcomes.append(Outcome(check.__name__.removeprefix("check_"), failed=[f"could not run ({reason})"]))
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config", type=Path, help="config file (default: the usual location)")
    parser.add_argument("--company", help="company code (default: the configured default)")
    args = parser.parse_args(argv)
    try:
        loaded = load_config(args.config, must_exist=args.config is not None)
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 1
    runtime = Runtime(loaded)
    runtime.discover()
    try:
        company = runtime.resolve_company(args.company)
        outcomes = reconcile(runtime, company)
    except SageError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1
    finally:
        runtime.close()
    for outcome in outcomes:
        print(outcome.line())
    failures = sum(not o.passed for o in outcomes)
    print("every check agreed" if not failures else f"{failures} check(s) disagreed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
