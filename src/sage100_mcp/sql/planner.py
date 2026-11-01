"""Native or hybrid (SPEC.md §5.5 step 4, §13.1).

A query runs natively when the ProvideX writer can express every part of it with what the driver
profile says works. Otherwise it runs hybrid: each table it reads is pulled with a single-table
SELECT carrying only conditions the driver handles, and DuckDB runs the original query over the
pulls. Pushed conditions only ever remove rows the original query would remove too: DuckDB still
applies every condition, so pushdown changes the cost of a query, never its result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlglot import exp

from sage100_mcp.capabilities import Capabilities
from sage100_mcp.catalog.model import Catalog, Column, Table
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.sql.bind import Bound, ordered_params
from sage100_mcp.sql.providex_dialect import Missing, Native, ProvideXWriter, _or_to_in

Mode = Literal["auto", "native", "hybrid"]
_PUSHABLE_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_VALUES = (exp.Literal, exp.Placeholder, exp.Neg, exp.Date, exp.Null)
_INPUT_ERRORS = {"date_value"}


@dataclass(frozen=True)
class Pull:
    """One table read for a hybrid plan, and the DuckDB table it fills."""

    alias: str
    table: Table
    columns: tuple[Column, ...]
    native: Native
    local_name: str
    pushed: int  # conditions sent to the driver


@dataclass
class Plan:
    mode: Literal["native", "hybrid"]
    native: Native | None = None
    pulls: list[Pull] = field(default_factory=list)
    local: exp.Query | None = None  # what DuckDB runs, over the pulls
    local_params: list[Bound] = field(default_factory=list)
    reasons: list[Missing] = field(default_factory=list)  # why the query could not run natively

    @property
    def sent_sql(self) -> list[str]:
        if self.native is not None:
            return [self.native.sql]
        return [pull.native.sql for pull in self.pulls]


def plan(
    tree: exp.Query,
    catalog: Catalog,
    capabilities: Capabilities,
    params: Sequence[Bound],
    *,
    mode: Mode,
    row_cap: int,
) -> Plan:
    """``row_cap`` is the most rows the caller wants plus one, to detect truncation."""
    native = ProvideXWriter(capabilities, catalog, params, row_cap=row_cap).statement(tree)
    for gap in native.missing:
        if gap.feature in _INPUT_ERRORS:  # the query's own mistake, which no engine could run
            raise SageError(ErrorCode.PARSE_ERROR, f"{gap.detail}.", hint="Write dates as YYYY-MM-DD.")
    if mode == "hybrid" or (mode == "auto" and not native.runnable):
        return hybrid(tree, catalog, capabilities, params, reasons=native.missing)
    if not native.runnable:
        details = "; ".join(m.detail for m in native.missing)
        raise SageError(
            ErrorCode.DIALECT_UNSUPPORTED,
            f"This driver can't run the query natively: {details}.",
            hint="Use mode='auto' or 'hybrid', which run what the driver can't locally.",
        )
    return Plan("native", native=native)


def hybrid(
    tree: exp.Query,
    catalog: Catalog,
    capabilities: Capabilities,
    params: Sequence[Bound],
    *,
    reasons: list[Missing] | None = None,
) -> Plan:
    local = tree.copy()
    pulls: list[Pull] = []
    for number, node in enumerate(list(local.find_all(exp.Table)), start=1):
        table = catalog.table(node.name)
        if table is None:
            continue  # a CTE name
        alias = node.alias_or_name
        conditions = pushable(node, alias)
        columns = _needed_columns(local, table, alias)
        local_name = f"pull_{number}"
        writable = _writable(conditions, table, alias, columns, catalog, capabilities, params)
        select = _pull_select(table, columns, writable)
        native = ProvideXWriter(capabilities, catalog, params).statement(select)
        pulls.append(Pull(alias, table, columns, native, local_name, len(writable)))
        node.replace(exp.Table(this=exp.to_identifier(local_name), alias=exp.TableAlias(this=exp.to_identifier(alias))))
    return Plan(
        "hybrid", pulls=pulls, local=local, local_params=ordered_params(local, params, "duckdb"), reasons=reasons or []
    )


def _needed_columns(tree: exp.Expr, table: Table, alias: str) -> tuple[Column, ...]:
    """Every column of ``table`` the query names (a superset is harmless); at least one, for row counts."""
    names = {
        node.name.lower()
        for node in tree.find_all(exp.Column)
        if node.table in ("", alias) and table.column(node.name) is not None
    }
    columns = tuple(c for c in table.columns if c.name.lower() in names and not c.masked)
    if columns:
        return columns
    keys = [table.column(k) for k in table.key]
    first = next((c for c in keys if c is not None), None) or next((c for c in table.columns if not c.masked), None)
    return (first,) if first is not None else ()


def _conjuncts(condition: exp.Expr | None) -> list[exp.Expr]:
    if condition is None:
        return []
    return list(condition.flatten()) if isinstance(condition, exp.And) else [condition]


def pushable(node: exp.Table, alias: str) -> list[exp.Expr]:
    """Single-table conditions on ``node`` that can be applied while reading it."""
    select = node.parent.parent if isinstance(node.parent, exp.Join | exp.From) else None
    if not isinstance(select, exp.Select):
        return []
    found: list[exp.Expr] = []
    where = select.args.get("where")
    for condition in _conjuncts(where.this if where is not None else None):
        if _single_table(condition, alias):
            found.append(condition)
    for join in select.args.get("joins") or []:
        side = (join.args.get("side") or "").upper()
        if side == "FULL":
            continue
        joined = join.this.alias_or_name if isinstance(join.this, exp.Table) else None
        # An ON condition removes rows only on the join's nullable side; on the preserved side it only
        # decides which rows match. LEFT JOIN: the joined table is nullable; RIGHT JOIN: the tables before it.
        nullable = side == "" or (side == "LEFT" and joined == alias) or (side == "RIGHT" and joined != alias)
        for condition in _conjuncts(join.args.get("on")):
            if nullable and _single_table(condition, alias):
                found.append(condition)
    return found


def _single_table(condition: exp.Expr, alias: str) -> bool:
    """A null-rejecting test of one column of ``alias`` against constants."""
    while isinstance(condition, exp.Paren):
        condition = condition.this
    columns = list(condition.find_all(exp.Column))
    if not columns or any(column.table != alias for column in columns):
        return False
    if isinstance(condition, exp.Or):
        condition = _or_to_in(condition) or condition
    if isinstance(condition, exp.Is):  # IS NULL keeps the outer join's padding rows; IS NOT NULL removes rows
        return bool(condition.args.get("negate")) and isinstance(condition.this, exp.Column)
    if isinstance(condition, exp.Not):
        inner = condition.this
        if isinstance(inner, exp.Is):
            return isinstance(inner.expression, exp.Null) and isinstance(inner.this, exp.Column)
        condition = inner
    if isinstance(condition, _PUSHABLE_COMPARISONS):
        sides = (condition.this, condition.expression)
        return any(isinstance(s, exp.Column) for s in sides) and all(
            isinstance(s, (exp.Column, *_VALUES)) for s in sides
        )
    if isinstance(condition, exp.In):
        return (
            isinstance(condition.this, exp.Column)
            and not condition.args.get("query")
            and all(isinstance(v, _VALUES) for v in condition.expressions)
        )
    if isinstance(condition, exp.Between):
        return isinstance(condition.this, exp.Column) and all(
            isinstance(condition.args[k], _VALUES) for k in ("low", "high")
        )
    if isinstance(condition, exp.Like):
        return isinstance(condition.this, exp.Column) and isinstance(
            condition.expression, exp.Literal | exp.Placeholder
        )
    return False


def _pull_select(table: Table, columns: tuple[Column, ...], conditions: list[exp.Expr]) -> exp.Select:
    select = exp.select(*(exp.column(c.name, table=table.name, quoted=True) for c in columns)).from_(
        exp.Table(this=exp.to_identifier(table.name, quoted=True))
    )
    if conditions:
        select = select.where(exp.and_(*conditions))
    return select


def _writable(
    conditions: list[exp.Expr],
    table: Table,
    alias: str,
    columns: tuple[Column, ...],
    catalog: Catalog,
    capabilities: Capabilities,
    params: Sequence[Bound],
) -> list[exp.Expr]:
    """The conditions the driver can take, rewritten onto the bare table name."""
    kept: list[exp.Expr] = []
    for condition in conditions:
        moved = condition.copy()
        for column in moved.find_all(exp.Column):
            column.set("table", exp.to_identifier(table.name, quoted=True))
        if isinstance(moved, exp.Or):
            moved = _or_to_in(moved) or moved
        probe = ProvideXWriter(capabilities, catalog, params).statement(_pull_select(table, columns, [moved]))
        if probe.runnable:
            kept.append(moved)
    if len(kept) > 1 and not capabilities.supports("compound_and"):
        kept = kept[:1]  # the first condition is the one written first, usually the most selective
    return kept
