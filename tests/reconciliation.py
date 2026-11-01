"""Reconciling the canned views to Sage's own reports (SPEC.md §12, the M3 exit gate).

A golden is one Sage report for the ABC sample company, saved as CSV with a header row that names its
columns as the view names them (docs/reconciliation.md says how to export each one). manifest.yaml
lists, per view, the report it came from, the arguments that reproduce it, the key columns and the
columns to compare. Every compared figure must match to the cent, row by row and in total.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from sage100_mcp.views.engine import ViewResult
from tests.paths import REPO

GOLDENS_ENV = "SAGE100_MCP_TEST_GOLDENS"
DEFAULT_GOLDENS = REPO / "tests" / "reconciliation" / "abc"
# The views the gate reconciles; the lookups and compare_companies only reuse their figures.
VIEWS = (
    "ar_aging",
    "ap_aging",
    "open_sales_orders",
    "open_purchase_orders",
    "inventory_availability",
    "gl_trial_balance",
    "sales_history",
)


@dataclass(frozen=True)
class Golden:
    view: str
    report: str
    company: str
    args: dict[str, Any]
    key: tuple[str, ...]
    compare: tuple[str, ...]
    rows: list[dict[str, str]]


def amount(text: str) -> Decimal:
    """A figure as Sage reports print it: 1,234.56, $1,234.56, (1,234.56) or 1,234.56- are all fine."""
    cleaned = re.sub(r"[\s$,]", "", text or "")
    negative = (cleaned.startswith("(") and cleaned.endswith(")")) or cleaned.endswith("-")
    cleaned = cleaned.strip("()").rstrip("-")
    if not cleaned:
        return Decimal(0)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{text!r} is not an amount") from None
    return -value if negative else value


def load_goldens(directory: Path) -> dict[str, Golden]:
    manifest = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8"))
    found = {}
    for view, entry in (manifest.get("views") or {}).items():
        with (directory / entry["file"]).open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        found[view] = Golden(
            view=view,
            report=entry["report"],
            company=str(entry.get("company") or manifest.get("company") or "ABC"),
            args=dict(entry.get("args") or {}),
            key=tuple(entry["key"]),
            compare=tuple(entry["compare"]),
            rows=rows,
        )
    return found


def _key(values: list[Any]) -> tuple[str, ...]:
    return tuple(str("" if v is None else v).strip().upper() for v in values)


def differences(result: ViewResult, golden: Golden) -> list[str]:
    """Every row and total where the view and the report disagree; empty when they reconcile."""
    names = [c.name for c in result.columns]
    missing = [c for c in (*golden.key, *golden.compare) if c not in names]
    if missing:
        return [f"{golden.view} has no column {', '.join(missing)} (columns: {', '.join(names)})"]
    if result.truncated:
        return [f"{golden.view} was truncated at {result.row_count} rows; raise max_rows to compare every row"]
    ours = {_key([row[names.index(k)] for k in golden.key]): row for row in result.rows}
    theirs = {_key([row.get(k) for k in golden.key]): row for row in golden.rows}
    found = []
    totals = {name: [Decimal(0), Decimal(0)] for name in golden.compare}
    for key in sorted(set(ours) | set(theirs)):
        view_row, report_row = ours.get(key), theirs.get(key)
        for name in golden.compare:
            mine = amount(str(view_row[names.index(name)] or "")) if view_row else Decimal(0)
            sage = amount(report_row.get(name) or "") if report_row else Decimal(0)
            totals[name][0] += mine
            totals[name][1] += sage
            if mine != sage:
                where = "only in the view" if report_row is None else "only in the report" if view_row is None else ""
                found.append(f"{'/'.join(key)} {name}: view {mine}, report {sage} {where}".rstrip())
    for name, (mine, sage) in totals.items():
        if mine != sage:
            found.append(f"total {name}: view {mine}, report {sage}, off by {mine - sage}")
    return found
