"""run_query and explain_query (SPEC.md §5.5, §11.2): guard, resolve, plan, bind, execute, shape.

On Premium every query runs natively: the checked tree is regenerated as T-SQL with TOP injected
and sent with ``?`` parameters. On ProvideX the planner compares the query with the driver's probed
capabilities: native sends the generator's ProvideX text, hybrid reads each table through the
driver with the filters it can take and runs the query in DuckDB (sql/planner.py, sql/hybrid.py).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlglot import exp

from sage100_mcp import principals
from sage100_mcp.backends.base import RawResult
from sage100_mcp.capabilities import Capabilities
from sage100_mcp.catalog.model import Catalog, Semantic, infer_semantic
from sage100_mcp.companies import Company
from sage100_mcp.errors import ErrorCode, SageError, WarningCode
from sage100_mcp.formatting import text_table
from sage100_mcp.http.cursors import invalid, query_key, rows_digest
from sage100_mcp.policy import MASK
from sage100_mcp.runtime import Runtime
from sage100_mcp.sql import hybrid
from sage100_mcp.sql.bind import Bound, bind, number_placeholders
from sage100_mcp.sql.guard import CheckedQuery, check, to_sql
from sage100_mcp.sql.planner import Plan, Pull, plan, pushable
from sage100_mcp.sql.planner import hybrid as hybrid_plan
from sage100_mcp.sql.providex_dialect import Missing, unescape_joins
from sage100_mcp.sql.resolve import OutputColumn, ResolvedQuery, resolve

logger = logging.getLogger(__name__)

Mode = Literal["auto", "native", "hybrid"]
InputDialect = Literal["ansi", "providex", "tsql"]
Cell = str | int | float | bool | None
EMPTY_DATE = datetime(1753, 1, 1)  # how Sage 100 Premium stores a blank date
_ESCAPE_TYPES = {exp.Date: "DATE", exp.Timestamp: "DATETIME2", exp.Time: "TIME"}  # {d}, {ts}, {t}
_ORDER_FEATURES = {"order_by_column", "order_by_position", "order_by_desc", "order_by_with_limit", "order_by"}


class ColumnInfo(BaseModel):
    name: str
    type: str
    semantic: Semantic
    masked: bool = False
    source: str | None = Field(default=None, description="Table.Column when the output is a plain catalog column")


class QueryResult(BaseModel):
    """The result envelope of SPEC.md §11.2. Rows are arrays in column order."""

    company: str
    backend: Literal["providex", "sqlserver"]
    mode: Literal["native", "hybrid"]
    columns: list[ColumnInfo]
    rows: list[list[Cell]]
    row_count: int
    truncated: bool
    as_of: str
    elapsed_ms: int
    null_dates_coerced: bool
    sent_sql: list[str]
    warnings: list[str]
    next_cursor: str | None = Field(
        default=None, description="When truncated: pass it as cursor, with the same sql and params, for the next page"
    )


def _warning(code: WarningCode, message: str) -> str:
    return f"{code}: {message}"


def _leaf_selects(tree: exp.Expr) -> list[exp.Select]:
    """The SELECTs whose rows form the output: the root, or every branch of a set operation."""
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    if isinstance(tree, exp.SetOperation):
        return _leaf_selects(tree.this) + _leaf_selects(tree.expression)
    return [tree] if isinstance(tree, exp.Select) else []


def _mask(tree: exp.Query, positions: list[int]) -> None:
    """Send '***' in place of each masked output, so a masked value never leaves the database (§9.2).

    The constant also keeps DISTINCT, UNION and ORDER BY on the output alias from revealing anything
    about the hidden values.
    """
    for select in _leaf_selects(tree):
        projections = list(select.expressions)
        for position in positions:
            if position < len(projections):
                name = projections[position].alias_or_name
                projections[position] = exp.alias_(exp.Literal.string(MASK), name, quoted=True)
        select.set("expressions", projections)


def _for_tsql(tree: exp.Query, top: int) -> exp.Query:
    """ODBC date escapes become CASTs, and TOP caps a plain SELECT so SQL Server stops early."""
    for node in list(tree.find_all(*_ESCAPE_TYPES)):
        literal = node.this
        others = [value for key, value in node.args.items() if key != "this" and value not in (None, [])]
        if isinstance(literal, exp.Literal) and literal.is_string and not others:
            node.replace(exp.cast(literal.copy(), _ESCAPE_TYPES[type(node)]))
    if isinstance(tree, exp.Select) and not tree.args.get("limit") and not tree.args.get("offset"):
        tree = tree.limit(top)
    return tree


def _cell(value: Any, empty_dates: list[int], *, blank_date: bool = True) -> Cell:
    """A driver value as JSON: exact decimal strings, ISO dates, trailing spaces trimmed (§5.4, §11.2).

    ``blank_date``: Premium stores a blank date as 1753-01-01, which reads as null here.
    """
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if blank_date and value == EMPTY_DATE:
            empty_dates.append(1)
            return None
        if value.time() == clock(0) and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date):
        if blank_date and value == EMPTY_DATE.date():
            empty_dates.append(1)
            return None
        return value.isoformat()
    if isinstance(value, clock):
        return value.isoformat()
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value).hex().upper()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _columns(raw: RawResult, outputs: tuple[OutputColumn, ...]) -> list[ColumnInfo]:
    aligned = len(outputs) == len(raw.columns)
    columns = []
    for position, raw_column in enumerate(raw.columns):
        output = outputs[position] if aligned else None
        name = output.name if output else raw_column.name
        if output is not None and output.table is not None and output.column is not None:
            source = output.column
            columns.append(
                ColumnInfo(
                    name=name,
                    type=source.type,
                    semantic=source.semantic,
                    masked=source.masked,
                    source=f"{output.table.name}.{source.name}",
                )
            )
        else:
            columns.append(ColumnInfo(name=name, type=raw_column.type, semantic=infer_semantic(name, raw_column.type)))
    return columns


@dataclass
class _Limits:
    row_cap: int
    timeout: int
    warnings: list[str]


def _limits(runtime: Runtime, backend: str, max_rows: int | None, timeout_s: int | None) -> _Limits:
    limits = runtime.config.limits
    warnings: list[str] = []
    row_cap = max_rows or limits.max_rows
    if row_cap > limits.max_rows_hard:
        warnings.append(_warning(WarningCode.LIMIT_CAPPED, f"max_rows is capped at {limits.max_rows_hard}."))
        row_cap = limits.max_rows_hard
    timeout_cap = limits.query_timeout_s.providex if backend == "providex" else limits.query_timeout_s.sqlserver
    timeout = timeout_s or timeout_cap
    if timeout > timeout_cap:
        warnings.append(_warning(WarningCode.LIMIT_CAPPED, f"timeout_s is capped at {timeout_cap} on this backend."))
        timeout = timeout_cap
    return _Limits(row_cap, timeout, warnings)


def _shape(
    runtime: Runtime,
    company: Company,
    raw: RawResult,
    resolved: ResolvedQuery | None,
    *,
    backend: Literal["providex", "sqlserver"],
    mode: Literal["native", "hybrid"],
    sent: list[str],
    row_cap: int,
    warnings: list[str],
    started: float,
    truncated: bool | None = None,
    columns: list[ColumnInfo] | None = None,
) -> QueryResult:
    """The §11.2 envelope from driver rows: JSON cells, masks, warnings."""
    limits = runtime.config.limits
    columns = columns if columns is not None else _columns(raw, resolved.outputs if resolved else ())
    masked = [position for position, column in enumerate(columns) if column.masked]
    empty_dates: list[int] = []
    rows = []
    for raw_row in raw.rows:
        row = [_cell(value, empty_dates, blank_date=backend == "sqlserver") for value in raw_row]
        for position in masked:
            row[position] = MASK
        rows.append(row)
    truncated = raw.truncated if truncated is None else truncated
    if resolved is not None:
        warnings += [_warning(WarningCode.COLUMNS_PRUNED, message) for message in resolved.warnings]
    if masked:
        names = ", ".join(columns[position].source or columns[position].name for position in masked)
        warnings.append(_warning(WarningCode.COLUMNS_MASKED, f"{names}: masked by policy; values show as {MASK}."))
    if truncated:
        more = f", or raise max_rows (up to {limits.max_rows_hard})" if row_cap < limits.max_rows_hard else ""
        warnings.append(
            _warning(
                WarningCode.RESULT_TRUNCATED,
                f"Only the first {row_cap} rows are shown; more matched. Pass next_cursor for the next page, "
                f"or add filters or aggregate{more}.",
            )
        )
    if empty_dates:
        many = len(empty_dates) > 1
        count = f"{len(empty_dates)} dates of 1753-01-01 show" if many else "1 date of 1753-01-01 shows"
        warnings.append(
            _warning(
                WarningCode.NULL_DATES_COERCED,
                f"{count} as null: it is Sage's blank date. To find blank dates filter with = '1753-01-01', "
                "not IS NULL.",
            )
        )
    if raw.null_dates_coerced:
        warnings.append(
            _warning(
                WarningCode.NULL_DATES_COERCED,
                "Sage holds dates here that are not valid; the driver's Null Date option read them as null.",
            )
        )
    return QueryResult(
        company=company.code,
        backend=backend,
        mode=mode,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        as_of=datetime.now().astimezone().isoformat(timespec="seconds"),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        null_dates_coerced=bool(empty_dates) or raw.null_dates_coerced,
        sent_sql=sent,
        warnings=warnings,
    )


def run_query(
    runtime: Runtime,
    company: Company,
    sql: str,
    params: Sequence[object] | None = None,
    *,
    max_rows: int | None = None,
    mode: Mode = "auto",
    dialect: InputDialect = "ansi",
    timeout_s: int | None = None,
) -> QueryResult:
    started = time.perf_counter()
    backend = runtime.backends[company.backend].kind
    caps = _limits(runtime, backend, max_rows, timeout_s)
    result = _run(runtime, company, sql, params, caps, mode=mode, dialect=dialect, started=started)
    if result.truncated:
        result.next_cursor = runtime.cursors.issue(
            principals.current().id,
            company.code,
            query_key(sql, params, mode, dialect),
            result.row_count,
            shown=rows_digest(result.rows),
        )
    return result


def _run(
    runtime: Runtime,
    company: Company,
    sql: str,
    params: Sequence[object] | None,
    caps: _Limits,
    *,
    mode: Mode,
    dialect: InputDialect,
    started: float,
) -> QueryResult:
    if runtime.backends[company.backend].kind == "providex":
        return _run_providex(runtime, company, sql, params, caps, mode=mode, dialect=dialect, started=started)
    return _run_premium(runtime, company, sql, params, caps, mode=mode, dialect=dialect, started=started)


def next_page(
    runtime: Runtime,
    cursor: str,
    sql: str,
    params: Sequence[object] | None = None,
    *,
    company: str | None = None,
    max_rows: int | None = None,
    mode: Mode = "auto",
    dialect: InputDialect = "ansi",
    timeout_s: int | None = None,
) -> QueryResult:
    """The page a cursor points at (§13.3); its first use runs the query again into a snapshot."""
    started = time.perf_counter()
    caller = principals.current()
    position = runtime.cursors.read(cursor, principal=caller.id, query=query_key(sql, params, mode, dialect))
    if company is not None and company.strip().upper() != position.company:
        raise invalid(
            f"This cursor is for company {position.company}, not {company.strip().upper()}.",
            hint="Leave company out to page on, or run the query again without cursor.",
        )
    target = runtime.resolve_company(position.company)
    caps = _limits(runtime, runtime.backends[target.backend].kind, max_rows, timeout_s)
    snapshot = runtime.cursors.opened(position)
    sent: list[str] = []
    if snapshot is None:
        keep = max(runtime.config.limits.max_pushdown_rows, position.offset + caps.row_cap)
        full = _run(runtime, target, sql, params, _Limits(keep, caps.timeout, []), mode=mode, dialect=dialect,
                    started=started)  # fmt: skip
        if rows_digest(full.rows[: position.offset]) != position.shown:
            raise invalid(
                "The rows changed since the first page, or came back in another order.",
                hint="Order by a unique key so that pages are stable, then run the query again without cursor.",
            )
        kept = [w for w in full.warnings if not w.startswith(WarningCode.RESULT_TRUNCATED + ":")]
        head = full.model_copy(update={"rows": [], "row_count": 0, "sent_sql": [], "warnings": kept})
        snapshot = runtime.cursors.save(position, full.rows, capped=full.truncated, head=head)
        sent = full.sent_sql
    head = snapshot.head
    assert isinstance(head, QueryResult)
    rows = runtime.cursors.page(snapshot, position.offset, caps.row_cap)
    end = position.offset + len(rows)
    more = end < snapshot.count
    warnings = caps.warnings + head.warnings
    if more:
        warnings.append(
            _warning(
                WarningCode.RESULT_TRUNCATED,
                f"Rows {position.offset + 1}-{end} of {snapshot.count} are shown. Pass next_cursor for the next page.",
            )
        )
    elif snapshot.capped:
        warnings.append(
            _warning(
                WarningCode.RESULT_TRUNCATED,
                f"Paging stops after {snapshot.count} rows (limits.max_pushdown_rows); more matched. Add filters "
                "to see the rest.",
            )
        )
    return head.model_copy(
        update={
            "rows": rows,
            "row_count": len(rows),
            "truncated": more or snapshot.capped,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "sent_sql": sent,
            "warnings": warnings,
            "next_cursor": runtime.cursors.issue(caller.id, target.code, position.query, end, snapshot=snapshot)
            if more
            else None,
        },
        deep=True,
    )


# --- Premium (SQL Server) --------------------------------------------------------------------------------


def _run_premium(
    runtime: Runtime,
    company: Company,
    sql: str,
    params: Sequence[object] | None,
    caps: _Limits,
    *,
    mode: Mode,
    dialect: InputDialect,
    started: float,
) -> QueryResult:
    if dialect == "providex":
        raise SageError(
            ErrorCode.DIALECT_UNSUPPORTED,
            f"Company {company.code} is on SQL Server (Sage 100 Premium), which does not read ProvideX SQL.",
            hint="Write ANSI SQL or T-SQL instead.",
            company=company.code,
        )
    if mode == "hybrid":
        caps.warnings.append(
            _warning(WarningCode.MODE_IGNORED, "Premium runs every query natively; mode='hybrid' is moot.")
        )
    done = _execute_premium(runtime, company, sql, params, row_cap=caps.row_cap, timeout=caps.timeout)
    return _shape(
        runtime, company, done.raw, done.resolved, backend="sqlserver", mode="native", sent=done.sent,
        row_cap=caps.row_cap, warnings=caps.warnings, started=started,
    )  # fmt: skip


@dataclass
class Executed:
    """One statement run on either backend, before formatting: what the view engine builds on."""

    raw: RawResult
    resolved: ResolvedQuery
    sent: list[str]
    mode: Literal["native", "hybrid"]
    warnings: list[str]
    truncated: bool  # more rows matched than were kept


def _execute_premium(
    runtime: Runtime, company: Company, sql: str, params: Sequence[object] | None, *, row_cap: int, timeout: int
) -> Executed:
    # T-SQL is read first whatever the dialect says: the T-SQL reader takes ANSI SQL unchanged, while the
    # ANSI reader misreads T-SQL such as DATEDIFF(day, a, b) (docs/decisions.md D17).
    checked = check(sql, dialect="tsql")
    catalog, _ = runtime.catalog(company)
    resolved = resolve(checked, catalog, runtime.require_policy())
    values = bind(params, checked.placeholders)
    _mask(resolved.tree, [position for position, output in enumerate(resolved.outputs) if output.masked])
    tree = _for_tsql(resolved.tree, row_cap + 1)
    sent = to_sql(tree, "tsql")
    raw = runtime.executor(company).query(
        company.database, sent, values, max_rows=row_cap, timeout_s=timeout, company=company.code
    )
    return Executed(raw, resolved, [sent], "native", [], raw.truncated)


def execute(
    runtime: Runtime,
    company: Company,
    sql: str,
    params: Sequence[object] | None,
    *,
    row_cap: int,
    timeout: int,
    mode: Mode = "auto",
) -> Executed:
    """``sql`` (ANSI or T-SQL) on the company's backend: guarded, resolved, planned on ProvideX."""
    if runtime.backends[company.backend].kind == "providex":
        return _execute_providex(runtime, company, sql, params, row_cap=row_cap, timeout=timeout, mode=mode)
    return _execute_premium(runtime, company, sql, params, row_cap=row_cap, timeout=timeout)


# --- ProvideX ----------------------------------------------------------------------------------------------


@dataclass
class _Prepared:
    """A ProvideX query after the guard, the resolver, masking and binding: ready to plan."""

    checked: CheckedQuery
    resolved: ResolvedQuery
    values: list[Bound]
    catalog: Catalog
    capabilities: Capabilities


def _prepare(runtime: Runtime, company: Company, sql: str, params: Sequence[object] | None) -> _Prepared:
    checked = check(sql, dialect="tsql")
    catalog, _ = runtime.catalog(company)
    resolved = resolve(checked, catalog, runtime.require_policy())
    values = bind(params, checked.placeholders)
    _mask(resolved.tree, [position for position, output in enumerate(resolved.outputs) if output.masked])
    number_placeholders(resolved.tree)
    _require_filters(resolved.tree, catalog, company)
    capabilities = runtime.executor(company).capabilities
    assert capabilities is not None, "a ProvideX executor always has a profile, if only the baseline"
    return _Prepared(checked, resolved, values, catalog, capabilities)


def _require_filters(tree: exp.Query, catalog: Catalog, company: Company) -> None:
    """§13.1: a big history table is read only with a date range or a key filter on it."""
    for node in tree.find_all(exp.Table):
        table = catalog.table(node.name)
        if table is None or not table.requires_filter:
            continue
        alias = node.alias_or_name
        dates = [c for c in table.columns if c.semantic == "date"]
        wanted = {name.lower() for name in table.key} | {c.name.lower() for c in dates}
        conditions = pushable(node, alias)
        if any(column.name.lower() in wanted for condition in conditions for column in condition.find_all(exp.Column)):
            continue
        since = (date.today() - timedelta(days=365)).isoformat()
        example = f"{alias}.{dates[0].name} >= '{since}'" if dates else f"{alias}.{table.key[0]} = ..."
        raise SageError(
            ErrorCode.FILTER_REQUIRED,
            f"{table.name} is a large history table; on ProvideX it is read only with a date range or a key "
            "filter, because the driver scans the whole file over the network.",
            hint=f"Add a filter such as WHERE {example}.",
            company=company.code,
        )


def _read_pulls(runtime: Runtime, company: Company) -> hybrid.Reader:
    executor = runtime.executor(company)

    def read(pull: Pull, cap: int, seconds_left: float) -> RawResult:
        return executor.query(
            company.database, pull.native.sql, pull.native.params, max_rows=cap, timeout_s=max(1, int(seconds_left))
        )

    return read


def _hybrid_warnings(plan_: Plan, tree: exp.Query) -> list[str]:
    reasons = "; ".join(dict.fromkeys(m.detail for m in plan_.reasons)) or "mode='hybrid' was asked for"
    warnings = [_warning(WarningCode.HYBRID_MODE, f"Run locally over {len(plan_.pulls)} table read(s): {reasons}.")]
    if tree.args.get("order") is not None:
        why = "the driver does not support it" if {m.feature for m in plan_.reasons} & _ORDER_FEATURES else "hybrid"
        warnings.append(_warning(WarningCode.ORDER_APPLIED_LOCALLY, f"ORDER BY applied locally: {why}."))
    return warnings


def _execute_hybrid(
    runtime: Runtime, company: Company, prepared: _Prepared, plan_: Plan, *, row_cap: int, timeout: int
) -> Executed:
    limits = runtime.config.limits
    result = hybrid.run(
        plan_,
        _read_pulls(runtime, company),
        max_pushdown_rows=limits.max_pushdown_rows,
        row_cap=row_cap,
        timeout_s=timeout,
        temp_directory=runtime.home / "tmp",
    )
    warnings = _hybrid_warnings(plan_, prepared.resolved.tree)
    return Executed(result.raw, prepared.resolved, plan_.sent_sql, "hybrid", warnings, result.raw.truncated)


def _execute_providex(
    runtime: Runtime,
    company: Company,
    sql: str,
    params: Sequence[object] | None,
    *,
    row_cap: int,
    timeout: int,
    mode: Mode,
) -> Executed:
    prepared = _prepare(runtime, company, sql, params)
    plan_ = plan(
        prepared.resolved.tree, prepared.catalog, prepared.capabilities, prepared.values, mode=mode,
        row_cap=row_cap + 1,
    )  # fmt: skip
    if plan_.mode == "hybrid":
        return _execute_hybrid(runtime, company, prepared, plan_, row_cap=row_cap, timeout=timeout)
    native = plan_.native
    assert native is not None
    # A TOP or LIMIT the driver can't take is applied by stopping the fetch (§5.2).
    wanted = native.fetch_limit if native.fetch_limit is not None and native.fetch_limit <= row_cap else None
    try:
        raw = runtime.executor(company).query(
            company.database,
            native.sql,
            native.params,
            max_rows=wanted if wanted is not None else row_cap,
            timeout_s=timeout,
            company=company.code,
            skip=native.fetch_offset,
        )
    except SageError as error:
        if error.code != ErrorCode.DIALECT_UNSUPPORTED or mode != "auto":
            raise
        # §14.2: the probe said yes but the driver said no (ORDER BY can fail per field on the 64-bit
        # driver, for one); re-plan once as hybrid.
        logger.info("Driver refused native SQL on %s (%s); re-planning as hybrid", company.code, error.detail)
        refused = Missing("driver_refused", f"the driver refused the native SQL ({error.message})")
        replanned = hybrid_plan(
            prepared.resolved.tree, prepared.catalog, prepared.capabilities, prepared.values, reasons=[refused]
        )
        return _execute_hybrid(runtime, company, prepared, replanned, row_cap=row_cap, timeout=timeout)
    truncated = False if wanted is not None else raw.truncated
    return Executed(raw, prepared.resolved, [native.sql], "native", [], truncated)


def _run_providex(
    runtime: Runtime,
    company: Company,
    sql: str,
    params: Sequence[object] | None,
    caps: _Limits,
    *,
    mode: Mode,
    dialect: InputDialect,
    started: float,
) -> QueryResult:
    if dialect == "providex":
        return _run_raw(runtime, company, sql, params, caps, mode=mode, started=started)
    done = _execute_providex(runtime, company, sql, params, row_cap=caps.row_cap, timeout=caps.timeout, mode=mode)
    return _shape(
        runtime, company, done.raw, done.resolved, backend="providex", mode=done.mode, sent=done.sent,
        row_cap=caps.row_cap, warnings=caps.warnings + done.warnings, started=started, truncated=done.truncated,
    )  # fmt: skip


def _run_raw(
    runtime: Runtime,
    company: Company,
    sql: str,
    params: Sequence[object] | None,
    caps: _Limits,
    *,
    mode: Mode,
    started: float,
) -> QueryResult:
    """dialect='providex': guarded and resolved like any query, then sent exactly as written (§9.1)."""
    text = sql.strip().rstrip(";").strip()
    checked = check(unescape_joins(text), dialect="tsql")
    catalog, _ = runtime.catalog(company)
    resolved = resolve(checked, catalog, runtime.require_policy())
    values = bind(params, checked.placeholders)
    masked = [output.name for output in resolved.outputs if output.masked]
    if masked:
        raise SageError(
            ErrorCode.PERMISSION_DENIED,
            f"Raw ProvideX SQL is sent as written, so masked columns can't be replaced: {', '.join(masked)}.",
            hint="Leave out the masked columns, or write the query in ANSI SQL, which masks them.",
            company=company.code,
        )
    _require_filters(resolved.tree, catalog, company)
    if mode == "hybrid":
        caps.warnings.append(_warning(WarningCode.MODE_IGNORED, "Raw ProvideX SQL always runs natively."))
    logger.info("raw ProvideX SQL on %s: %s", company.code, text)
    raw = runtime.executor(company).query(
        company.database, text, values, max_rows=caps.row_cap, timeout_s=caps.timeout, company=company.code
    )
    return _shape(
        runtime, company, raw, resolved, backend="providex", mode="native", sent=[text], row_cap=caps.row_cap,
        warnings=caps.warnings, started=started,
    )  # fmt: skip


# --- explain_query (§11.1) ----------------------------------------------------------------------------


class FeatureGap(BaseModel):
    feature: str
    detail: str


class PullInfo(BaseModel):
    table: str
    alias: str
    columns: list[str]
    pushed_conditions: int
    sql: str


class ExplainResult(BaseModel):
    """What run_query would do, without touching the database."""

    company: str
    backend: Literal["providex", "sqlserver"]
    mode: Literal["native", "hybrid"]
    capabilities: str = Field(description="Which driver profile the plan used: probe, baseline or not_applicable")
    driver_version: str | None
    features_used: list[str] = Field(description="Driver features the native SQL relies on")
    missing: list[FeatureGap] = Field(description="Why the query can't run natively, if it can't")
    sent_sql: list[str] = Field(description="The statements the driver would receive")
    pulls: list[PullInfo] = Field(description="Hybrid: one read per table, with the filters pushed to the driver")
    local_sql: str | None = Field(description="Hybrid: the query DuckDB runs over the reads")
    warnings: list[str]


def explain_query(
    runtime: Runtime,
    company: Company,
    sql: str,
    params: Sequence[object] | None = None,
    *,
    mode: Mode = "auto",
    dialect: InputDialect = "ansi",
    max_rows: int | None = None,
) -> ExplainResult:
    executor = runtime.executor(company)
    caps = _limits(runtime, executor.kind, max_rows, None)
    if executor.kind != "providex":
        if dialect == "providex":
            raise SageError(
                ErrorCode.DIALECT_UNSUPPORTED,
                f"Company {company.code} is on SQL Server (Sage 100 Premium), which does not read ProvideX SQL.",
                hint="Write ANSI SQL or T-SQL instead.",
                company=company.code,
            )
        checked = check(sql, dialect="tsql")
        catalog, _ = runtime.catalog(company)
        resolved = resolve(checked, catalog, runtime.require_policy())
        bind(params, checked.placeholders)
        _mask(resolved.tree, [position for position, output in enumerate(resolved.outputs) if output.masked])
        sent = to_sql(_for_tsql(resolved.tree, caps.row_cap + 1), "tsql")
        warnings = [_warning(WarningCode.COLUMNS_PRUNED, message) for message in resolved.warnings]
        return ExplainResult(
            company=company.code, backend="sqlserver", mode="native", capabilities="not_applicable",
            driver_version=executor.driver_version, features_used=[], missing=[], sent_sql=[sent], pulls=[],
            local_sql=None, warnings=caps.warnings + warnings,
        )  # fmt: skip
    if dialect == "providex":
        text = sql.strip().rstrip(";").strip()
        checked = check(unescape_joins(text), dialect="tsql")
        catalog, _ = runtime.catalog(company)
        resolve(checked, catalog, runtime.require_policy())
        bind(params, checked.placeholders)
        source = executor.capabilities.source if executor.capabilities is not None else "baseline"
        return ExplainResult(
            company=company.code, backend="providex", mode="native", capabilities=source,
            driver_version=executor.driver_version, features_used=[], missing=[], sent_sql=[text], pulls=[],
            local_sql=None, warnings=[*caps.warnings, "Raw ProvideX SQL is sent exactly as written."],
        )  # fmt: skip
    prepared = _prepare(runtime, company, sql, params)
    plan_ = plan(
        prepared.resolved.tree, prepared.catalog, prepared.capabilities, prepared.values, mode=mode,
        row_cap=caps.row_cap + 1,
    )  # fmt: skip
    warnings = [*caps.warnings, *(_warning(WarningCode.COLUMNS_PRUNED, m) for m in prepared.resolved.warnings)]
    if plan_.mode == "hybrid":
        warnings += _hybrid_warnings(plan_, prepared.resolved.tree)
    used = plan_.native.used if plan_.native is not None else []
    return ExplainResult(
        company=company.code,
        backend="providex",
        mode=plan_.mode,
        capabilities=prepared.capabilities.source,
        driver_version=executor.driver_version,
        features_used=used,
        missing=[FeatureGap(feature=m.feature, detail=m.detail) for m in plan_.reasons],
        sent_sql=plan_.sent_sql,
        pulls=[
            PullInfo(
                table=p.table.name,
                alias=p.alias,
                columns=[c.name for c in p.columns],
                pushed_conditions=p.pushed,
                sql=p.native.sql,
            )
            for p in plan_.pulls
        ],
        local_sql=plan_.local.sql(dialect="duckdb") if plan_.local is not None else None,
        warnings=warnings,
    )


def result_text(result: QueryResult) -> str:
    count = f"{result.row_count} row{'' if result.row_count == 1 else 's'}{' (truncated)' if result.truncated else ''}"
    lines = [
        f"{result.company} ({result.backend}, {result.mode}): {count} in {result.elapsed_ms} ms",
        text_table([c.name for c in result.columns], result.rows),
    ]
    if result.warnings:
        lines.append("\n".join(f"- {warning}" for warning in result.warnings))
    if result.next_cursor:
        lines.append(f"next_cursor: {result.next_cursor}")
    return "\n\n".join(lines)


def explain_text(result: ExplainResult) -> str:
    lines = [f"{result.company} ({result.backend}): {result.mode}"]
    if result.missing:
        lines.append("Runs locally because: " + "; ".join(gap.detail for gap in result.missing))
    if result.pulls:
        lines += [f"Read {p.table} ({p.pushed_conditions} filter(s) pushed): {p.sql}" for p in result.pulls]
        lines.append(f"Then locally: {result.local_sql}")
    else:
        lines += [f"Sent: {statement}" for statement in result.sent_sql]
    lines += [f"- {warning}" for warning in result.warnings]
    return "\n".join(lines)
