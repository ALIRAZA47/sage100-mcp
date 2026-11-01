"""Run a canned view on either backend (SPEC.md §12).

Premium runs the view's one T-SQL statement. ProvideX runs its pulls (each a single-table SELECT
that goes through the planner, so the driver gets as much of the filtering as it can take), loads
them into an in-memory DuckDB and runs the view's local SQL there. Both results are formatted by
the view's declared output columns, so the two plans can be diffed cell for cell (§15.4).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from importlib.resources import files
from typing import Any, Literal

from pydantic import BaseModel, Field

from sage100_mcp.backends.base import RawResult, ResultColumn
from sage100_mcp.companies import Company
from sage100_mcp.config import parse_yaml
from sage100_mcp.errors import ErrorCode, SageError, WarningCode
from sage100_mcp.query import Cell, ColumnInfo, Executed, execute
from sage100_mcp.runtime import Runtime
from sage100_mcp.sql.hybrid import duckdb_type
from sage100_mcp.views.model import OutputColumn, Pull, ViewDef, bind_params, named, positional, render

EMPTY_DATE = date(1753, 1, 1)  # Premium's blank date (D18); a view never shows it
CENT = Decimal("0.01")
LOAD_CHUNK = 20_000


class ViewResult(BaseModel):
    """A canned view's rows, in the §11.2 envelope plus the view's parameters and totals."""

    company: str
    backend: Literal["providex", "sqlserver", "mixed"] = Field(
        description="mixed only when compare_companies spans both kinds of backend"
    )
    view: str
    version: int
    parameters: dict[str, Cell | list[str]] = Field(description="The arguments as the view applied them")
    columns: list[ColumnInfo]
    rows: list[list[Cell]]
    row_count: int
    truncated: bool
    totals: dict[str, Cell] = Field(description="Sums of the money and quantity columns over every row")
    as_of: str
    elapsed_ms: int
    null_dates_coerced: bool
    sent_sql: list[str]
    warnings: list[str]


@dataclass
class Prepared:
    """What a view's rules add before its SQL runs (views/rules.py)."""

    params: dict[str, Any]
    fragments: dict[str, str] = field(default_factory=dict)  # replaces {{name}} in either plan's SQL
    renames: dict[str, str] = field(default_factory=dict)  # output column -> the name shown
    warnings: list[str] = field(default_factory=list)


Rules = Callable[[Runtime, Company, ViewDef, dict[str, Any]], Prepared]


@cache
def load_views() -> dict[str, ViewDef]:
    """Every definition in views/*.yaml, by name."""
    found: dict[str, ViewDef] = {}
    for entry in sorted(files("sage100_mcp.views").iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".yaml"):
            view = ViewDef.model_validate(parse_yaml(entry.read_text(encoding="utf-8")))
            if f"{view.name}.yaml" != entry.name:
                raise ValueError(f"{entry.name} defines view {view.name}")
            found[view.name] = view
    return found


def warning(code: WarningCode, message: str) -> str:
    return f"{code}: {message}"


def substitute(template: str, fragments: dict[str, str], plan: str) -> str:
    """Fill ``{{name}}`` with the rules' SQL: ``plan:name`` (local or tsql) first, then ``name``."""
    for key, sql in sorted(fragments.items(), key=lambda item: ":" not in item[0]):
        scope, _, name = key.rpartition(":")
        if scope in ("", plan):
            template = template.replace("{{" + name + "}}", sql)
    if "{{" in template:
        raise SageError(ErrorCode.INTERNAL, "A view's SQL has a fragment its rules did not fill.")
    return template


def require_modules(runtime: Runtime, company: Company, name: str, modules: list[str]) -> None:
    """§8.4: fail before sending anything when the company lacks a module the view needs."""
    _, profile = runtime.catalog(company)
    missing = [m for m in modules if m not in profile.modules]
    if not missing:
        return
    having = sorted(
        code
        for code, state in runtime.catalogs.items()
        if code in runtime.visible_companies()
        and state.profile is not None
        and set(modules) <= set(state.profile.modules)
    )
    raise SageError(
        ErrorCode.MODULE_NOT_INSTALLED,
        f"Company {company.code} has no {', '.join(missing)} module, which {name} needs.",
        hint=f"Companies with it: {', '.join(having)}." if having else "No company here has it.",
        company=company.code,
        choices=having or None,
    )


# --- the two plans ---------------------------------------------------------------------------------


def _premium(runtime: Runtime, company: Company, sql: str, params: dict[str, Any], cap: int, timeout: int) -> Executed:
    text, values = positional(sql, params)
    return execute(runtime, company, text, values, row_cap=cap, timeout=timeout)


def run_pull(runtime: Runtime, company: Company, sql: str, values: list[Any], timeout: int) -> Executed:
    """One single-table read, planned like any query; a read past max_pushdown_rows fails (§5.5)."""
    cap = runtime.config.limits.max_pushdown_rows
    done = execute(runtime, company, sql, values, row_cap=cap, timeout=timeout)
    if done.truncated:
        table = done.resolved.outputs[0].table if done.resolved.outputs else None
        raise SageError(
            ErrorCode.PUSHDOWN_LIMIT,
            f"Reading {table.name if table else 'a table'} for this view passed {cap:,} rows.",
            hint="Narrow the view with its filters (a customer, vendor, item, warehouse or date).",
            company=company.code,
        )
    return done


def _pull_columns(runtime: Runtime, company: Company, pull: Pull) -> list[tuple[str, str]]:
    """(name, DuckDB type) of each column a pull reads, from the company catalog."""
    catalog, _ = runtime.catalog(company)
    table = catalog.table(pull.table)
    if table is None:
        raise SageError(ErrorCode.UNKNOWN_TABLE, f"{pull.table} is not in company {company.code}'s catalog.")
    found = []
    for name in pull.columns:
        column = table.column(name)
        if column is None:
            raise SageError(
                ErrorCode.UNKNOWN_COLUMN, f"{table.name}.{name} is not in company {company.code}'s catalog."
            )
        found.append((column.name, duckdb_type(column)))
    return found


def _load(connection: Any, name: str, columns: list[tuple[str, str]], rows: list[tuple[Any, ...]]) -> None:
    kinds = [kind for _, kind in columns]
    body = ", ".join(f'"{column}" {kind}' for column, kind in columns)
    connection.execute(f'CREATE TABLE "{name}" ({body})')
    if not rows:
        return
    unnest = ", ".join(f"UNNEST(?::{kind}[])" for kind in kinds)
    statement = f'INSERT INTO "{name}" SELECT {unnest}'
    for start in range(0, len(rows), LOAD_CHUNK):
        chunk = rows[start : start + LOAD_CHUNK]
        values = [[_loadable(row[i], kinds[i]) for row in chunk] for i in range(len(kinds))]
        connection.execute(statement, values)


def _loadable(value: Any, kind: str) -> Any:
    if isinstance(value, str):
        return value.rstrip()
    if kind == "DATE" and isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date) and value == EMPTY_DATE:
        return None
    return value


def _providex(
    runtime: Runtime, company: Company, view: ViewDef, local: str, params: dict[str, Any], cap: int, timeout: int
) -> tuple[RawResult, list[str], bool]:
    import duckdb

    sent: list[str] = []
    coerced = False
    pulled: dict[str, list[tuple[Any, ...]]] = {}
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET memory_limit = '1GB'")
        for pull in view.pulls:
            rows: list[tuple[Any, ...]] = []
            if not pull.wanted(params):
                pulled[pull.name] = rows  # not needed for these arguments: an empty table keeps the SQL valid
                _load(connection, pull.name, _pull_columns(runtime, company, pull), rows)
                continue
            if pull.keys_from is None:
                text, values = positional(render(pull.sql(params), params), params)
                reads = [run_pull(runtime, company, text, values, timeout)]
            else:
                reads = _keyed_pull(runtime, company, view, pull, pulled, params, timeout)
            for done in reads:
                rows += done.raw.rows
                sent += done.sent
                coerced = coerced or done.raw.null_dates_coerced
            pulled[pull.name] = rows
            _load(connection, pull.name, _pull_columns(runtime, company, pull), rows)
        sql, arguments = named(local, params)
        started = time.perf_counter()
        cursor = connection.execute(sql, arguments)
        description = cursor.description or []
        rows = [tuple(row) for row in cursor.fetchmany(cap + 1)]
    except duckdb.Error as exc:
        raise SageError(
            ErrorCode.INTERNAL,
            f"The local part of view {view.name} failed.",
            hint="Report it with the view's arguments.",
            detail=type(exc).__name__ + ": " + str(exc).splitlines()[0][:300],
        ) from None
    finally:
        connection.close()
    columns = tuple(ResultColumn(d[0], str(d[1]).lower()) for d in description)
    elapsed = int((time.perf_counter() - started) * 1000)
    return RawResult(columns, rows[:cap], len(rows) > cap, elapsed, coerced), sent, coerced


def _keyed_pull(
    runtime: Runtime,
    company: Company,
    view: ViewDef,
    pull: Pull,
    pulled: dict[str, list[tuple[Any, ...]]],
    params: dict[str, Any],
    timeout: int,
) -> list[Executed]:
    """A pull filtered by the keys another pull returned: ``filter IN (...)``, in chunks. No keys, no read."""
    assert pull.keys_from is not None
    source = next(p for p in view.pulls if p.name == pull.keys_from.pull)
    index = [c.lower() for c in source.columns].index(pull.keys_from.column.lower())
    keys = sorted({str(row[index]).rstrip() for row in pulled[source.name] if row[index] not in (None, "")})
    reads = []
    for start in range(0, len(keys), pull.keys_from.chunk):
        chunk = keys[start : start + pull.keys_from.chunk]
        sql = render(pull.sql(params, [f"{pull.keys_from.filter} IN (:__keys)"]), params)
        text, values = positional(sql, {**params, "__keys": chunk})
        reads.append(run_pull(runtime, company, text, values, timeout))
    return reads


# --- results -----------------------------------------------------------------------------------------


def _cell(value: Any, column: OutputColumn) -> Cell:
    if value is None:
        return None
    if column.scale is not None:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        number = number.quantize(Decimal(1).scaleb(-column.scale), rounding=ROUND_HALF_UP)
        if not number:
            number = abs(number)  # never "-0.00"
        if column.semantic == "quantity":  # 5, 2.5: Sage keeps six places that are almost always zero
            number = number.normalize() if number else Decimal(0)
        elif column.scale > 2:  # unit prices and costs: cents at least, no zeros past them
            exponent = number.normalize().as_tuple().exponent
            number = number.normalize() if isinstance(exponent, int) and exponent < -2 else number.quantize(CENT)
        return format(number, "f")
    if column.type == "int":
        return int(value)
    if column.type == "bool":
        return bool(value)
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return None if value == EMPTY_DATE else value.isoformat()
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value  # type: ignore[no-any-return]


def _echo(params: dict[str, Any], view: ViewDef) -> dict[str, Cell | list[str]]:
    shown: dict[str, Cell | list[str]] = {}
    for name in view.params:
        value = params.get(name)
        if isinstance(value, date):
            shown[name] = value.isoformat()
        elif isinstance(value, Decimal):
            shown[name] = format(value, "f")
        elif isinstance(value, list):
            shown[name] = [str(v) for v in value]
        else:
            shown[name] = value
    return shown


def run_view(
    runtime: Runtime,
    company: Company,
    view: ViewDef,
    args: dict[str, Any],
    *,
    max_rows: int | None = None,
    rules: Rules | None = None,
) -> ViewResult:
    started = time.perf_counter()
    require_modules(runtime, company, view.name, view.modules)
    limits = runtime.config.limits
    warnings: list[str] = []
    row_cap = max_rows or limits.max_rows
    if row_cap > limits.max_rows_hard:
        warnings.append(warning(WarningCode.LIMIT_CAPPED, f"max_rows is capped at {limits.max_rows_hard}."))
        row_cap = limits.max_rows_hard
    params = bind_params(view, args, today=date.today())
    prepared = rules(runtime, company, view, params) if rules else Prepared(params)
    params = prepared.params
    variant = view.variant(params)
    backend = runtime.backends[company.backend].kind
    timeouts = limits.query_timeout_s
    fetch_cap = limits.max_rows_hard  # every row is read, so totals cover them all; rows shown stop at row_cap
    if backend == "providex":
        local = render(substitute(variant.local, prepared.fragments, "local"), params)
        raw, sent, coerced = _providex(runtime, company, view, local, params, fetch_cap, timeouts.providex)
    else:
        sql = render(substitute(variant.tsql, prepared.fragments, "tsql"), params)
        done = _premium(runtime, company, sql, params, fetch_cap, timeouts.sqlserver)
        raw, sent, coerced = done.raw, done.sent, done.raw.null_dates_coerced
    if len(raw.columns) != len(variant.columns):
        raise SageError(
            ErrorCode.INTERNAL, f"View {view.name} returned {len(raw.columns)} columns, not {len(variant.columns)}."
        )
    rows = [[_cell(value, column) for value, column in zip(row, variant.columns, strict=True)] for row in raw.rows]
    totals: dict[str, Cell] = {}
    for name in variant.totals:
        position = next(i for i, c in enumerate(variant.columns) if c.name == name)
        column = variant.columns[position]
        total = sum((Decimal(str(row[position])) for row in rows if row[position] is not None), Decimal(0))
        totals[prepared.renames.get(name, name)] = _cell(total, column)
    if raw.truncated:
        warnings.append(
            warning(WarningCode.RESULT_TRUNCATED, f"More than {limits.max_rows_hard} rows; totals cover those read.")
        )
    truncated = raw.truncated or len(rows) > row_cap
    if len(rows) > row_cap:
        warnings.append(
            warning(
                WarningCode.RESULT_TRUNCATED,
                f"Only the first {row_cap} of {len(rows)} rows are shown; totals cover them all. Narrow the "
                f"view with its filters, or raise max_rows (up to {limits.max_rows_hard}).",
            )
        )
    if coerced:
        warnings.append(
            warning(
                WarningCode.NULL_DATES_COERCED,
                "Sage holds dates here that are not valid; the driver's Null Date option read them as null.",
            )
        )
    columns = [
        ColumnInfo(name=prepared.renames.get(c.name, c.name), type=c.type, semantic=c.semantic) for c in variant.columns
    ]
    return ViewResult(
        company=company.code,
        backend="providex" if backend == "providex" else "sqlserver",
        view=view.name,
        version=view.version,
        parameters=_echo(params, view),
        columns=columns,
        rows=rows[:row_cap],
        row_count=min(len(rows), row_cap),
        truncated=truncated,
        totals=totals,
        as_of=datetime.now().astimezone().isoformat(timespec="seconds"),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        null_dates_coerced=coerced,
        sent_sql=sent,
        warnings=prepared.warnings + warnings,
    )
