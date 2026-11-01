"""What each canned view works out before its SQL runs, and checks after (SPEC.md §12).

Site rules come from the company's own *_Options rows (aging days and basis, divisions) and from
config.yaml (the available formula), never from assumed defaults; where a rule is missing the view
says so in a warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sage100_mcp.companies import Company
from sage100_mcp.errors import ErrorCode, SageError, WarningCode
from sage100_mcp.query import execute
from sage100_mcp.runtime import Runtime
from sage100_mcp.views.engine import Prepared, Rules, ViewResult, warning
from sage100_mcp.views.model import ViewDef

DEFAULT_AGING = (30, 60, 90, 120)  # §12.2: used, with a warning, when AP_Options has no aging categories
_TERMS = {
    "onhand": "s.QuantityOnHand",
    "so": "s.QuantityOnSalesOrder",
    "bo": "s.QuantityOnBackOrder",
    "wo_required": "s.QuantityRequiredForWO",
    "in_shipping": "s.QuantityInShipping",
    "po": "s.QuantityOnPurchaseOrder",
    "wo": "s.QuantityOnWorkOrder",
}
# IM_Options.QuantityAvailableToInclude: which supply Sage counts as available (P, W, B or N).
_SUPPLY = {"P": " + po", "W": " + wo", "B": " + po + wo", "N": ""}
PROJECTED = "onhand + po + wo - so - bo - wo_required"


def options_row(runtime: Runtime, company: Company, table: str, columns: list[str]) -> dict[str, Any] | None:
    """One module's settings row, read through the company's own backend; None when it has none."""
    catalog, _ = runtime.catalog(company)
    found = catalog.table(table)
    if found is None:
        return None
    present = [c for c in columns if found.column(c) is not None]
    if not present:
        return None
    timeout = runtime.config.limits.query_timeout_s
    seconds = timeout.providex if runtime.backends[company.backend].kind == "providex" else timeout.sqlserver
    done = execute(runtime, company, f"SELECT {', '.join(present)} FROM {found.name}", [], row_cap=1, timeout=seconds)
    if not done.raw.rows:
        return None
    values = {c.name.lower(): value for c, value in zip(done.raw.columns, done.raw.rows[0], strict=True)}
    return {name: values.get(name.lower()) for name in present}


# --- aging ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Buckets:
    starts: tuple[int, int, int, int]  # first day (or month) of each past-due category
    unit: str = "days"  # or "months"

    def labels(self) -> list[str]:
        """current, then each category after the days (or months) it holds: days_1_15 ... days_61_plus."""
        s1, s2, s3, s4 = self.starts
        spans = [(s1, s2 - 1), (s2, s3 - 1), (s3, s4 - 1)]
        middle = [f"{self.unit}_{a}" if a == b else f"{self.unit}_{a}_{b}" for a, b in spans]
        return ["current", *middle, f"{self.unit}_{s4}_plus"]

    def params(self) -> dict[str, Any]:
        values: dict[str, Any] = {f"c{i}": start for i, start in enumerate(self.starts, start=1)}
        values.update({f"label_{i}": label for i, label in enumerate(self.labels())})
        return values


def _starts(row: dict[str, Any] | None) -> tuple[int, int, int, int] | None:
    if row is None:
        return None
    values = [row.get(f"AgingCategory{i}") for i in range(1, 5)]
    if not all(isinstance(v, int | Decimal) for v in values):
        return None
    starts = tuple(int(v) for v in values if isinstance(v, int | Decimal))
    if len(starts) != 4 or list(starts) != sorted(set(starts)) or starts[0] < 1:
        return None
    return starts[0], starts[1], starts[2], starts[3]


def _flag(row: dict[str, Any], column: str, default: str) -> str:
    return str(row.get(column) or default).strip().upper()


def _aging(
    runtime: Runtime,
    company: Company,
    params: dict[str, Any],
    table: str,
    party: str,
    *,
    unit_column: str,
    basis_column: str | None = None,
) -> Prepared:
    """Buckets, basis and unit from the options row. The unit (D days, M months) applies to all four
    categories; months are calendar months, as DATEDIFF(month) counts them. AP stores no basis: its
    aging report asks, so age_by does (default due); for AR age_by overrides AgeInvoicesBy."""
    wanted = [basis_column, unit_column, "AgingCategory1", "AgingCategory2", "AgingCategory3", "AgingCategory4"]
    row = options_row(runtime, company, table, [c for c in [*wanted, "Divisions"] if c]) or {}
    warnings: list[str] = []
    starts = _starts(row)
    unit = "months" if _flag(row, unit_column, "D") == "M" else "days"
    if starts is None:
        starts, unit = DEFAULT_AGING, "days"
        warnings.append(
            warning(
                WarningCode.AGING_DEFAULTS_USED,
                f"{table} has no usable aging categories, so {', '.join(map(str, starts))} days apply.",
            )
        )
    basis = params.get("age_by")
    if basis is None:
        basis = "invoice" if basis_column and _flag(row, basis_column, "D") == "I" else "due"
    buckets = Buckets(starts, unit)
    if params.get("division") and _flag(row, "Divisions", "N") != "Y":
        warnings.append(
            warning(
                WarningCode.DIVISION_DISABLED,
                f"Divisions are off in {table}, so every {party} is in division 00 or the one shown.",
            )
        )
    renames = {f"bucket_{i}": label for i, label in enumerate(buckets.labels())}
    if unit == "months":
        renames["days"] = "months"
    part = unit[:-1]  # DATEDIFF's day or month
    fragments = {"local:unit": f"'{part}'", "tsql:unit": part}
    return Prepared(
        {**params, **buckets.params(), "basis": basis}, fragments=fragments, renames=renames, warnings=warnings
    )


def ar_aging(runtime: Runtime, company: Company, view: ViewDef, params: dict[str, Any]) -> Prepared:
    return _aging(
        runtime, company, params, "AR_Options", "customer", unit_column="AgingCategory", basis_column="AgeInvoicesBy"
    )


def ap_aging(runtime: Runtime, company: Company, view: ViewDef, params: dict[str, Any]) -> Prepared:
    return _aging(runtime, company, params, "AP_Options", "vendor", unit_column="AgingCategoriesToUseForInv")


# --- inventory -------------------------------------------------------------------------------------------


def available_sql(formula: str) -> str:
    """config.yaml's formula (already validated: terms joined by + or -) over IM_ItemWarehouse ``s``."""
    pieces = []
    for token in formula.split():
        if token in ("+", "-"):
            pieces.append(token)
        else:
            pieces.append(f"COALESCE({_TERMS[token]}, 0)")
    return " ".join(pieces)


def _like_escaped(text: str) -> str:
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_").replace("[", "![")


def available_formula(runtime: Runtime, company: Company) -> str:
    """config.yaml's formula, with "sage" read from the company's IM_Options as Sage's Item Inquiry does:
    on hand, plus purchase and work orders where the option includes them, less sales orders, back
    orders and work order requirements."""
    formula = runtime.config.views.inventory_available_formula
    if formula != "sage":
        return formula
    row = options_row(runtime, company, "IM_Options", ["QuantityAvailableToInclude"]) or {}
    supply = _SUPPLY.get(str(row.get("QuantityAvailableToInclude") or "N").strip().upper(), "")
    return f"onhand{supply} - so - bo - wo_required"


def inventory_availability(runtime: Runtime, company: Company, view: ViewDef, params: dict[str, Any]) -> Prepared:
    item = params.get("item")
    extra = {
        # The driver's LIKE takes % and _ as wildcards; the local SQL then keeps exact prefix matches only.
        "item_pattern": f"{item}%" if item else None,
        "item_escaped": f"{_like_escaped(item)}%" if item else None,
    }
    fragments = {
        "available": available_sql(available_formula(runtime, company)),
        "projected": available_sql(PROJECTED),
    }
    return Prepared({**params, **extra}, fragments=fragments)


# --- sales history -------------------------------------------------------------------------------------

_GROUPS = {
    # group_by: (DuckDB key, T-SQL key, name); i = invoice header, l = line, c/s/p = the name tables
    "customer": ("i.ARDivisionNo || '-' || i.CustomerNo", "i.ARDivisionNo + '-' + i.CustomerNo",
                 "COALESCE(c.CustomerName, i.BillToName, '')"),
    "item": ("COALESCE(l.ItemCode, '')", "ISNULL(l.ItemCode, '')", "COALESCE(l.ItemCodeDesc, '')"),
    "salesperson": ("COALESCE(i.SalespersonDivisionNo, '') || '-' || COALESCE(i.SalespersonNo, '')",
                    "ISNULL(i.SalespersonDivisionNo, '') + '-' + ISNULL(i.SalespersonNo, '')",
                    "COALESCE(s.SalespersonName, '')"),
    "product_line": ("COALESCE(l.ProductLine, '')", "ISNULL(l.ProductLine, '')", "COALESCE(p.ProductLineDesc, '')"),
    "month": ("strftime(i.InvoiceDate, '%Y-%m')",
              "CAST(YEAR(i.InvoiceDate) AS VARCHAR(4)) + '-' "
              "+ RIGHT('0' + CAST(MONTH(i.InvoiceDate) AS VARCHAR(2)), 2)",
              "''"),
}  # fmt: skip


def sales_history(runtime: Runtime, company: Company, view: ViewDef, params: dict[str, Any]) -> Prepared:
    if params["from_date"] > params["to_date"]:
        raise SageError(ErrorCode.PARSE_ERROR, "from_date is after to_date.", hint="Swap them.")
    local_key, tsql_key, name = _GROUPS[params["group_by"]]
    fragments = {"local:key": local_key, "tsql:key": tsql_key, "name": name}
    return Prepared(params, fragments=fragments)


# --- trial balance -----------------------------------------------------------------------------------


def trial_balance_check(result: ViewResult) -> list[str]:
    debit = Decimal(str(result.totals.get("debit") or 0))
    credit = Decimal(str(result.totals.get("credit") or 0))
    if debit == credit:
        return []
    return [
        warning(
            WarningCode.TB_OUT_OF_BALANCE,
            f"Debits ({debit}) and credits ({credit}) differ by {debit - credit}. Check for unposted entries "
            "or a prior year that is not closed.",
        )
    ]


RULES: dict[str, Rules] = {
    "ar_aging": ar_aging,
    "ap_aging": ap_aging,
    "inventory_availability": inventory_availability,
    "sales_history": sales_history,
}
CHECKS = {"gl_trial_balance": trial_balance_check}
