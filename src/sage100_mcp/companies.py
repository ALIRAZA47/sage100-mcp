"""Companies (SPEC.md §8): which company a call uses, and each company's profile.

A profile is read from the company's *_Options tables, one row each. A module counts as installed
when its options table exists and has a row (MAS_PCC, for example, has AP and GL but no SO or IM).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sage100_mcp.config import parse_company_code
from sage100_mcp.errors import ErrorCode, SageError

if TYPE_CHECKING:
    from sage100_mcp.backends.base import Executor

OPTION_SETTINGS = (  # the *_Options columns a profile reads
    "DataVersion", "CurrentFiscalYr", "CurrentPeriod", "Divisions", "AgeInvoicesBy", "AgingCategory",
    "AgingCategoriesToUseForInv", "QuantityAvailableToInclude",
    "AgingCategory1", "AgingCategory2", "AgingCategory3", "AgingCategory4", "DaysToRetainPaidInvs",
)  # fmt: skip

OPTIONS_COLUMNS = """
SELECT c.TABLE_NAME, c.COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS AS c
JOIN INFORMATION_SCHEMA.TABLES AS t ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE c.TABLE_SCHEMA = 'dbo' AND t.TABLE_TYPE = 'BASE TABLE' AND c.TABLE_NAME LIKE '%[_]Options'
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""


@dataclass(frozen=True)
class Company:
    """A company the server exposes, after the allow and hide lists."""

    code: str
    name: str | None
    backend: str  # the backend's name in config.yaml
    database: str


def resolve_company(
    requested: str | None, *, active: str | None, default: str | None, available: Mapping[str, Company]
) -> Company:
    """The call's company, else the active one, else the default, else the only one; never a guess (§8.3)."""
    choices = sorted(available)
    if requested is not None:
        try:
            code = parse_company_code(requested)
        except ValueError as exc:
            raise SageError(ErrorCode.COMPANY_NOT_FOUND, str(exc), choices=choices or None) from None
    elif active:
        code = active
    elif default:
        code = default
    elif len(choices) == 1:
        code = choices[0]
    else:
        raise SageError(
            ErrorCode.COMPANY_REQUIRED,
            "Several companies are available; say which one." if choices else "No company is available.",
            hint="Pass company, or call set_company." if choices else "Check sage_status for backend errors.",
            choices=choices or None,
        )
    company = available.get(code)
    if company is None:
        raise SageError(
            ErrorCode.COMPANY_NOT_FOUND,
            f"Company {code} is not available.",
            hint="Choose one of the listed companies." if choices else "Check sage_status for backend errors.",
            choices=choices or None,
        )
    return company


@dataclass(frozen=True)
class Aging:
    basis: str  # "due" or "invoice"; AP has no stored basis, so its aging report's default (due) applies
    starts: tuple[int, ...]  # first day (or month) of each past-due category, from the options table
    unit: str = "days"  # "days" or "months" (AR_Options.AgingCategory, AP_Options.AgingCategoriesToUseForInv)


@dataclass(frozen=True)
class Profile:
    code: str
    name: str | None
    backend: str
    database: str
    modules: tuple[str, ...]
    data_versions: dict[str, str]
    current_periods: dict[str, str] = field(default_factory=dict)  # module -> "2024-12"
    divisions: dict[str, bool] = field(default_factory=dict)
    ar_aging: Aging | None = None
    ap_aging: Aging | None = None


def _text(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value).strip()


def options_queries(columns: list[tuple[str, str]]) -> dict[str, str]:
    """One SELECT per *_Options table, reading only the settings it has."""
    by_table: dict[str, list[str]] = {}
    for table, column in columns:
        if column in OPTION_SETTINGS:
            by_table.setdefault(table, []).append(column)
        else:
            by_table.setdefault(table, [])
    queries = {}
    for table, wanted in by_table.items():
        select = ", ".join(f"[{c}]" for c in wanted) or "1 AS [Present]"
        queries[table] = f"SELECT TOP 1 {select} FROM [dbo].[{table}]"
    return queries


def read_tsql_options(executor: Executor, database: str, timeout_s: int) -> dict[str, dict[str, Any] | None]:
    """The *_Options rows through INFORMATION_SCHEMA and one SELECT TOP 1 each (SQL Server)."""
    columns = [(str(t), str(c)) for t, c in executor.scalar_rows(database, OPTIONS_COLUMNS)]
    found: dict[str, dict[str, Any] | None] = {}
    for table, sql in options_queries(columns).items():
        result = executor.query(database, sql, max_rows=1, timeout_s=timeout_s)
        names = [c.name for c in result.columns]
        found[table] = dict(zip(names, result.rows[0], strict=True)) if result.rows else None
    return found


def _aging(row: dict[str, Any], basis_column: str | None, unit_column: str) -> Aging | None:
    starts = [row.get(f"AgingCategory{i}") for i in range(1, 5)]
    if not all(isinstance(value, int | Decimal) for value in starts):
        return None
    basis = "due"
    if basis_column is not None:
        basis = "invoice" if str(row.get(basis_column, "D")).strip().upper() == "I" else "due"
    unit = "months" if str(row.get(unit_column, "D")).strip().upper() == "M" else "days"
    return Aging(basis, tuple(int(value) for value in starts if isinstance(value, int | Decimal)), unit)


def build_profile(
    code: str, name: str | None, backend: str, database: str, options: dict[str, dict[str, Any] | None]
) -> Profile:
    """``options`` maps each *_Options table to its row, or None when the table is empty."""
    modules, versions, periods, divisions = [], {}, {}, {}
    ar_aging = ap_aging = None
    for table, row in sorted(options.items()):
        if row is None:
            continue
        module = table.rsplit("_Options", 1)[0].upper()
        modules.append(module)
        if row.get("DataVersion") is not None:
            versions[module] = _text(row["DataVersion"])
        if row.get("CurrentFiscalYr") and row.get("CurrentPeriod"):
            periods[module] = f"{_text(row['CurrentFiscalYr'])}-{_text(row['CurrentPeriod']).zfill(2)}"
        if row.get("Divisions") is not None:
            divisions[module] = _text(row["Divisions"]).upper() == "Y"
        if module == "AR":
            ar_aging = _aging(row, "AgeInvoicesBy", "AgingCategory")
        if module == "AP":
            ap_aging = _aging(row, None, "AgingCategoriesToUseForInv")
    return Profile(code, name, backend, database, tuple(modules), versions, periods, divisions, ar_aging, ap_aging)
