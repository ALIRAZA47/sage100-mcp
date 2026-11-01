"""Run a canned view by name, or one view across companies, and describe them all (SPEC.md §11, §12)."""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from sage100_mcp.companies import Company
from sage100_mcp.errors import ErrorCode, SageError, WarningCode
from sage100_mcp.formatting import text_table
from sage100_mcp.query import Cell, ColumnInfo
from sage100_mcp.runtime import Runtime
from sage100_mcp.views.engine import ViewResult, load_views, require_modules, run_view, warning
from sage100_mcp.views.lookups import LOOKUPS, MATCHES, run_lookup
from sage100_mcp.views.rules import CHECKS, RULES


def view_names() -> list[str]:
    """Every canned view, lookups included; compare_companies takes any of these."""
    return sorted([*load_views(), *LOOKUPS])


def run_named(
    runtime: Runtime, company: Company, name: str, args: dict[str, Any], *, max_rows: int | None = None
) -> ViewResult:
    if name in LOOKUPS:
        unknown = sorted(set(args) - {"query"})
        if unknown:
            raise SageError(
                ErrorCode.PARSE_ERROR, f"{name}: unknown argument(s) {', '.join(unknown)}.", hint="It takes query."
            )
        return run_lookup(runtime, company, LOOKUPS[name], str(args.get("query") or ""))
    views = load_views()
    if name not in views:
        raise SageError(ErrorCode.PARSE_ERROR, f"There is no view {name!r}.", choices=view_names())
    result = run_view(runtime, company, views[name], args, max_rows=max_rows, rules=RULES.get(name))
    check = CHECKS.get(name)
    if check is not None:
        result.warnings += check(result)
    return result


def compare_companies(
    runtime: Runtime, name: str, args: dict[str, Any], companies: list[str], *, max_rows: int | None = None
) -> ViewResult:
    """One view per company, as one result with a leading company column (§8.3)."""
    started = time.perf_counter()
    if name == "compare_companies":
        raise SageError(ErrorCode.PARSE_ERROR, "compare_companies can't compare itself.", choices=view_names())
    if name not in view_names():
        raise SageError(ErrorCode.PARSE_ERROR, f"There is no view {name!r}.", choices=view_names())
    codes = list(dict.fromkeys(c.strip().upper() for c in companies if c.strip()))
    if len(codes) < 2:
        raise SageError(
            ErrorCode.PARSE_ERROR,
            "Give at least two companies to compare.",
            choices=sorted(runtime.visible_companies()),
        )
    targets = [runtime.resolve_company(code) for code in codes]
    modules = [LOOKUPS[name].module] if name in LOOKUPS else load_views()[name].modules
    for company in targets:  # every company is checked before any of them is queried
        require_modules(runtime, company, name, modules)
    results = [run_named(runtime, company, name, args, max_rows=max_rows) for company in targets]
    first = results[0]
    named_alike = all([c.name for c in r.columns] == [c.name for c in first.columns] for r in results)
    columns = [ColumnInfo(name="company", type="varchar(3)", semantic="code")]
    warnings: list[str] = []
    if named_alike:
        columns += first.columns
    else:  # each company's own aging categories: keep their positions, and say what each holds
        columns += [
            ColumnInfo(name=f"column_{i}", type=c.type, semantic=c.semantic) for i, c in enumerate(first.columns)
        ]
        for result in results:
            labels = ", ".join(f"column_{i}={c.name}" for i, c in enumerate(result.columns))
            warnings.append(warning(WarningCode.COLUMNS_DIFFER, f"{result.company} columns: {labels}."))
    rows: list[list[Cell]] = []
    totals: dict[str, Decimal] = {}
    for result in results:
        rows += [[result.company, *row] for row in result.rows]
        warnings += [f"{result.company}: {w}" for w in result.warnings]
        for key, value in result.totals.items():
            if value is not None:
                totals[key] = totals.get(key, Decimal(0)) + Decimal(str(value))
    shared = set.intersection(*(set(r.totals) for r in results)) if named_alike else set()
    backends = {r.backend for r in results}
    return ViewResult(
        company=",".join(codes),
        backend=first.backend if len(backends) == 1 else "mixed",
        view=name,
        version=first.version,
        parameters=first.parameters,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=any(r.truncated for r in results),
        totals={key: format(value, "f") for key, value in totals.items() if key in shared},
        as_of=datetime.now().astimezone().isoformat(timespec="seconds"),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        null_dates_coerced=any(r.null_dates_coerced for r in results),
        sent_sql=[sql for r in results for sql in r.sent_sql],
        warnings=warnings,
    )


def view_text(result: ViewResult) -> str:
    """The text block for clients that read no structuredContent: a table of the first rows, then totals."""
    count = f"{result.row_count} row{'' if result.row_count == 1 else 's'}{' (truncated)' if result.truncated else ''}"
    lines = [
        f"{result.company} ({result.backend}) {result.view}: {count} in {result.elapsed_ms} ms",
        text_table([c.name for c in result.columns], result.rows),
    ]
    if result.totals:
        lines.append("Totals: " + ", ".join(f"{name} {value}" for name, value in result.totals.items()))
    if result.warnings:
        lines.append("\n".join(f"- {w}" for w in result.warnings))
    return "\n\n".join(lines)


def describe_views() -> dict[str, Any]:
    """sage://views (§11.4): each view's definition, parameters and the SQL each plan sends."""
    views = []
    for view in load_views().values():
        views.append(
            {
                "name": view.name,
                "version": view.version,
                "title": view.title,
                "description": view.description,
                "modules": view.modules,
                "parameters": {name: p.model_dump(exclude_none=True) for name, p in view.params.items()},
                "variant_parameter": view.variant_param,
                "providex_pulls": [p.model_dump(by_alias=True, exclude_none=True) for p in view.pulls],
                "variants": {
                    key: {
                        "columns": [c.model_dump(exclude_none=True) for c in variant.columns],
                        "totals": variant.totals,
                        "providex_local_sql": variant.local.strip(),
                        "premium_tsql": variant.tsql.strip(),
                    }
                    for key, variant in view.variants.items()
                },
                "notes": view.notes,
            }
        )
    lookups = [
        {
            "name": lookup.name,
            "modules": [lookup.module],
            "parameters": {"query": {"type": "text", "required": True, "description": "Part of a number or name"}},
            "master_sql": f"SELECT {', '.join(lookup.columns)} FROM {lookup.table}",
            "figures": [c.model_dump(exclude_none=True) for c in lookup.figure_columns],
            "notes": [
                "The master read is cached per company for 10 minutes; matching is local, case- and accent-blind, "
                f"across {', '.join(lookup.searched)}. Figures are read for the first {MATCHES} matches only."
            ],
        }
        for lookup in LOOKUPS.values()
    ]
    return {
        "syntax": (
            ":name is a parameter. A line ending '-- if name' is sent only when name has a value ('-- if not "
            "name': only when it has none). {{name}} is filled from the company's options (aging unit, available "
            "formula, group key). ProvideX runs the pulls through the planner, then the local SQL in DuckDB; "
            "Premium runs the T-SQL."
        ),
        "views": views,
        "lookups": lookups,
        "compare_companies": "Runs any of these views for two or more companies and adds a leading company column.",
    }
