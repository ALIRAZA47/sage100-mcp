"""customer_lookup, vendor_lookup and item_lookup (SPEC.md §12.8).

The master table's key, name and contact columns are read once per company and kept for 10 minutes
(§13.4). Matching happens here, not in SQL: case- and accent-insensitive, across number, name, email
and phone, so it never depends on ProvideX LIKE rules or OR. Balances (or quantities) are then read
for the matches only, with one IN list. Both backends run the same two statements, so their rows
are the same.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sage100_mcp.backends.base import RawResult
from sage100_mcp.companies import Company
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.query import ColumnInfo, execute
from sage100_mcp.runtime import Runtime
from sage100_mcp.views.engine import ViewResult, require_modules
from sage100_mcp.views.model import OutputColumn
from sage100_mcp.views.rules import available_formula, available_sql

CACHE_SECONDS = 600
MATCHES = 20
_cache: dict[tuple[int, str, str], tuple[float, RawResult]] = {}
_lock = threading.Lock()


def fold(text: Any) -> str:
    """Lower case without accents: 'Café' and 'CAFE' match."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


def _digits(text: Any) -> str:
    return re.sub(r"\D", "", str(text or ""))


@dataclass(frozen=True)
class Lookup:
    name: str
    module: str
    table: str
    key: tuple[str, ...]  # the key columns, division first where there is one
    columns: tuple[str, ...]  # read from the master table: key, then name, then the rest
    types: tuple[str, ...]  # their output types, fixed so that both backends describe them alike
    searched: tuple[str, ...]  # matched against the query
    phone: str | None
    figures: Callable[[Runtime, Company, list[tuple[Any, ...]]], dict[tuple[str, ...], dict[str, Any]]]
    figure_columns: tuple[OutputColumn, ...]


def _seconds(runtime: Runtime, company: Company) -> int:
    timeouts = runtime.config.limits.query_timeout_s
    return timeouts.providex if runtime.backends[company.backend].kind == "providex" else timeouts.sqlserver


def _read(runtime: Runtime, company: Company, sql: str, values: list[Any]) -> RawResult:
    limits = runtime.config.limits
    done = execute(runtime, company, sql, values, row_cap=limits.max_pushdown_rows, timeout=_seconds(runtime, company))
    if done.truncated:
        raise SageError(
            ErrorCode.PUSHDOWN_LIMIT,
            f"The lookup read more than {limits.max_pushdown_rows:,} rows.",
            hint="Raise limits.max_pushdown_rows, or use run_query with a filter.",
            company=company.code,
        )
    return done.raw


def _master(runtime: Runtime, company: Company, lookup: Lookup) -> RawResult:
    key = (id(runtime), company.code, lookup.name)
    with _lock:
        cached = _cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    raw = _read(runtime, company, f"SELECT {', '.join(lookup.columns)} FROM {lookup.table}", [])
    with _lock:
        _cache[key] = (time.monotonic(), raw)
    return raw


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _score(row: dict[str, Any], words: list[str], whole: str, digits: str, lookup: Lookup) -> int | None:
    """0 exact number, 1 number prefix, 2 name prefix, 3 found elsewhere; None when not every word matches."""
    number = fold(row[lookup.key[-1]])
    name = fold(row[lookup.columns[len(lookup.key)]])
    haystack = " ".join(fold(row[c]) for c in lookup.searched)
    phone = _digits(row[lookup.phone]) if lookup.phone else ""
    if not all(word in haystack for word in words) and not (len(digits) >= 4 and digits in phone):
        return None
    if number == whole:
        return 0
    if number.startswith(whole):
        return 1
    if name.startswith(whole):
        return 2
    return 3


def run_lookup(runtime: Runtime, company: Company, lookup: Lookup, query: str) -> ViewResult:
    started = time.perf_counter()
    text = query.strip()
    if not text or len(text) > 100:
        raise SageError(
            ErrorCode.PARSE_ERROR, "query must be 1 to 100 characters.", hint="Give part of a number or name."
        )
    require_modules(runtime, company, lookup.name, [lookup.module])
    master = _master(runtime, company, lookup)
    whole, digits = fold(text), _digits(text)
    words = whole.split()
    scored = []
    for raw_row in master.rows:
        row = dict(zip(lookup.columns, raw_row, strict=True))
        score = _score(row, words, whole, digits, lookup)
        if score is not None:
            scored.append(
                (
                    score,
                    fold(row[lookup.columns[len(lookup.key)]]),
                    tuple(str(row[k]).rstrip() for k in lookup.key),
                    raw_row,
                )
            )
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    matches = scored[: MATCHES + 1]
    shown = [item[3] for item in matches[:MATCHES]]
    figures = lookup.figures(runtime, company, shown)
    columns = [
        ColumnInfo(name=name, type=kind, semantic="code" if name in lookup.key else "text")
        for name, kind in zip(lookup.columns, lookup.types, strict=True)
    ] + [ColumnInfo(name=c.name, type=c.type, semantic=c.semantic) for c in lookup.figure_columns]
    rows = []
    for raw_row in shown:
        key = tuple(str(raw_row[i]).rstrip() for i in range(len(lookup.key)))
        extra = figures.get(key, {})
        cells: list[Any] = [_text(value) for value in raw_row]
        cells += [_figure(extra.get(c.name), c) for c in lookup.figure_columns]
        rows.append(cells)
    warnings = []
    if len(matches) > MATCHES:
        warnings.append(f"RESULT_TRUNCATED: More than {MATCHES} match; give more of the name or number.")
    return ViewResult(
        company=company.code,
        backend="providex" if runtime.backends[company.backend].kind == "providex" else "sqlserver",
        view=lookup.name,
        version=1,
        parameters={"query": text},
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=len(matches) > MATCHES,
        totals={},
        as_of=datetime.now().astimezone().isoformat(timespec="seconds"),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        null_dates_coerced=False,
        sent_sql=[f"SELECT {', '.join(lookup.columns)} FROM {lookup.table}"],
        warnings=warnings,
    )


def _text(value: Any) -> Any:
    """Master columns are all text; Premium's NULL and ProvideX's blank are the same thing (§5.4)."""
    return "" if value is None else str(value).rstrip()


def _figure(value: Any, column: OutputColumn) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return None if value == date(1753, 1, 1) else value.isoformat()
    if isinstance(value, Decimal):
        if column.semantic == "quantity":
            return format(value.normalize() if value else Decimal(0), "f")
        return format(value.quantize(Decimal("0.01")), "f")
    return value


def _marks(values: list[str]) -> str:
    return ", ".join("?" for _ in values)


def _open_balances(table: str, division: str, number: str) -> Callable[..., dict[tuple[str, ...], dict[str, Any]]]:
    def figures(
        runtime: Runtime, company: Company, rows: list[tuple[Any, ...]]
    ) -> dict[tuple[str, ...], dict[str, Any]]:
        numbers = sorted({str(row[1]).rstrip() for row in rows})
        if not numbers:
            return {}
        sql = f"SELECT {division}, {number}, InvoiceDate, Balance FROM {table} WHERE {number} IN ({_marks(numbers)})"
        found: dict[tuple[str, ...], dict[str, Any]] = {}
        for div, num, invoiced, balance in _read(runtime, company, sql, numbers).rows:
            entry = found.setdefault((str(div).rstrip(), str(num).rstrip()), {"open_balance": Decimal(0)})
            if balance:
                entry["open_balance"] += balance
            if isinstance(invoiced, datetime):
                invoiced = invoiced.date()
            if isinstance(invoiced, date) and invoiced != date(1753, 1, 1):
                latest = entry.get("last_invoice_date")
                entry["last_invoice_date"] = max(latest, invoiced) if latest else invoiced
        return found

    return figures


def _item_quantities(
    runtime: Runtime, company: Company, rows: list[tuple[Any, ...]]
) -> dict[tuple[str, ...], dict[str, Any]]:
    codes = sorted({str(row[0]).rstrip() for row in rows})
    if not codes:
        return {}
    available = available_sql(available_formula(runtime, company))
    sql = (
        "SELECT s.ItemCode, s.QuantityOnHand, " + available + ", s.QuantityOnPurchaseOrder "
        f"FROM IM_ItemWarehouse s WHERE s.ItemCode IN ({_marks(codes)})"
    )
    found: dict[tuple[str, ...], dict[str, Any]] = {}
    for code, on_hand, free, on_po in _read(runtime, company, sql, codes).rows:
        entry = found.setdefault(
            (str(code).rstrip(),),
            {"on_hand": Decimal(0), "available": Decimal(0), "on_purchase_order": Decimal(0), "warehouses": 0},
        )
        entry["on_hand"] += on_hand or 0
        entry["available"] += free or 0
        entry["on_purchase_order"] += on_po or 0
        entry["warehouses"] += 1
    return found


_MONEY = {"type": "decimal(17,2)", "semantic": "money"}
LOOKUPS = {
    "customer_lookup": Lookup(
        name="customer_lookup",
        module="AR",
        table="AR_Customer",
        key=("ARDivisionNo", "CustomerNo"),
        columns=("ARDivisionNo", "CustomerNo", "CustomerName", "EmailAddress", "TelephoneNo", "City", "State"),
        types=("varchar(2)", "varchar(20)", "varchar(50)", "varchar(250)", "varchar(20)", "varchar(30)", "varchar(2)"),
        searched=("CustomerNo", "CustomerName", "EmailAddress", "TelephoneNo"),
        phone="TelephoneNo",
        figures=_open_balances("AR_OpenInvoice", "ARDivisionNo", "CustomerNo"),
        figure_columns=(
            OutputColumn(name="open_balance", **_MONEY),
            OutputColumn(name="last_invoice_date", type="date", semantic="date"),
        ),
    ),
    "vendor_lookup": Lookup(
        name="vendor_lookup",
        module="AP",
        table="AP_Vendor",
        key=("APDivisionNo", "VendorNo"),
        columns=("APDivisionNo", "VendorNo", "VendorName", "EmailAddress", "TelephoneNo", "City", "State"),
        types=("varchar(2)", "varchar(7)", "varchar(50)", "varchar(250)", "varchar(20)", "varchar(30)", "varchar(2)"),
        searched=("VendorNo", "VendorName", "EmailAddress", "TelephoneNo"),
        phone="TelephoneNo",
        figures=_open_balances("AP_OpenInvoice", "APDivisionNo", "VendorNo"),
        figure_columns=(
            OutputColumn(name="open_balance", **_MONEY),
            OutputColumn(name="last_invoice_date", type="date", semantic="date"),
        ),
    ),
    "item_lookup": Lookup(
        name="item_lookup",
        module="CI",
        table="CI_Item",
        key=("ItemCode",),
        columns=("ItemCode", "ItemCodeDesc", "ProductLine", "ItemType"),
        types=("varchar(30)", "varchar(30)", "varchar(4)", "varchar(1)"),
        searched=("ItemCode", "ItemCodeDesc"),
        phone=None,
        figures=_item_quantities,
        figure_columns=(
            OutputColumn(name="on_hand", type="decimal(15,6)", semantic="quantity"),
            OutputColumn(name="available", type="decimal(15,6)", semantic="quantity"),
            OutputColumn(name="on_purchase_order", type="decimal(15,6)", semantic="quantity"),
            OutputColumn(name="warehouses", type="int", semantic="number"),
        ),
    ),
}
