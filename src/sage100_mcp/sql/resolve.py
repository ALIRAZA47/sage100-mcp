"""Resolve a checked query against the company catalog (SPEC.md §5.5 step 3, §9.2, §13.1).

Every table and column must exist in the catalog; names are matched case-insensitively and sent
in Sage's casing. A top-level ``SELECT *`` is narrowed to each table's core columns. Masked
columns may be selected, and come back masked, but may not be used anywhere else in the
statement: a filter, join, sort, group or expression on them would reveal their values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope
from sqlglot.schema import MappingSchema

from sage100_mcp.catalog.model import Catalog, Column, Table
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.policy import Policy
from sage100_mcp.sql.guard import CheckedQuery

_UNRESOLVED = re.compile(r"Column '([^']+)' could not be resolved")


@dataclass(frozen=True)
class OutputColumn:
    name: str
    table: Table | None  # set when the output is a plain column of a catalog table
    column: Column | None

    @property
    def masked(self) -> bool:
        return self.column is not None and self.column.masked


@dataclass(frozen=True)
class ResolvedQuery:
    tree: exp.Query
    tables: tuple[Table, ...]
    outputs: tuple[OutputColumn, ...]
    warnings: tuple[str, ...]


def _catalog_table(table: exp.Table, catalog: Catalog, policy: Policy) -> Table:
    found = catalog.table(table.name)
    if found is not None:
        return found
    rule = policy.denied_by(table.name)
    if rule is not None:
        raise SageError(
            ErrorCode.TABLE_DENIED,
            f"{table.name} is not available: {rule.reason}",
            hint=f"Policy rule {rule.pattern} hides it (sage://policy).",
        )
    suggestions = catalog.suggest(table.name)
    raise SageError(
        ErrorCode.UNKNOWN_TABLE,
        f"There is no table {table.name} in company {catalog.company}.",
        hint=f"Did you mean {', '.join(suggestions)}?" if suggestions else "Use search_catalog or list_tables.",
        choices=suggestions or None,
    )


def _canonical_names(tree: exp.Expr, tables: list[Table]) -> dict[str, str]:
    """Lower-cased name -> the spelling to send: Sage's for catalog names, else the first one written."""
    names: dict[str, str] = {}
    for table in tables:
        names.setdefault(table.name.lower(), table.name)
        for column in table.columns:
            names.setdefault(column.name.lower(), column.name)
    for identifier in tree.find_all(exp.Identifier):
        names.setdefault(identifier.name.lower(), identifier.name)
    return names


def _expand_root_star(select: exp.Select, sources: dict[str, Table]) -> list[str]:
    """Replace * and t.* at the top level with the core columns of catalog tables."""
    warnings: list[str] = []
    expanded: list[exp.Expr] = []
    for projection in select.expressions:
        star_of = None
        if isinstance(projection, exp.Star):
            star_of = list(sources)
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            star_of = [projection.table] if projection.table in sources else None
        if star_of is None:
            expanded.append(projection)
            continue
        for alias in star_of:
            table = sources[alias]
            shown = table.default_columns()
            expanded.extend(exp.column(c.name, table=alias, quoted=True) for c in shown)
            if len(shown) < len(table.columns):
                warnings.append(
                    f"SELECT * shows the {len(shown)} core columns of {table.name} (of {len(table.columns)}); "
                    "name other columns to get them."
                )
    if warnings:
        select.set("expressions", expanded)
    return warnings


def _first_select(tree: exp.Expr) -> exp.Select | None:
    """The leftmost SELECT of a set operation, which names the output columns."""
    while isinstance(tree, exp.SetOperation | exp.Subquery):
        tree = tree.this
    return tree if isinstance(tree, exp.Select) else None


def _name_expressions(select: exp.Select) -> None:
    """Name each unaliased computed output after its own text; SQL Server would leave it nameless."""
    named: list[exp.Expr] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Alias | exp.Column | exp.Star):
            named.append(projection)
        else:
            text = projection.sql(dialect="tsql", comments=False)[:128]  # SQL Server's identifier limit
            named.append(exp.alias_(projection, text, quoted=True))
    select.set("expressions", named)


def _from_sources(select: exp.Select, catalog: Catalog) -> dict[str, Table] | None:
    """alias -> catalog table for the top-level FROM and JOINs, or None if any source is derived."""
    sources: dict[str, Table] = {}
    from_ = select.args.get("from_") or select.args.get("from")
    nodes = ([from_.this] if from_ else []) + [join.this for join in select.args.get("joins") or []]
    for node in nodes:
        if not isinstance(node, exp.Table):
            return None
        table = catalog.table(node.name)
        if table is None:
            return None
        sources[node.alias_or_name] = table
    return sources


def _branches(scope: Scope) -> list[Scope]:
    """The SELECT scopes whose rows make up the output: the scope itself, or every set-operation branch."""
    if not scope.set_operation_scopes:
        return [scope]
    return [leaf for branch in scope.set_operation_scopes for leaf in _branches(branch)]


def _direct_projections(scope: Scope) -> set[int]:
    """ids of Column nodes that are a whole output column of the statement (allowed to be masked)."""
    allowed: set[int] = set()
    for branch in _branches(scope):
        if isinstance(branch.expression, exp.Select):
            for projection in branch.expression.expressions:
                node = projection.this if isinstance(projection, exp.Alias) else projection
                if isinstance(node, exp.Column):
                    allowed.add(id(node))
    return allowed


def _source_table(scope: Scope, column: exp.Column, catalog: Catalog) -> Table | None:
    source = scope.sources.get(column.table)
    if isinstance(source, exp.Table):
        return catalog.table(source.name)
    return None


def resolve(checked: CheckedQuery, catalog: Catalog, policy: Policy) -> ResolvedQuery:
    tree = checked.tree.copy()
    tables = []
    for node in tree.find_all(exp.Table):
        if node.name and node.name.lower() not in checked.cte_names:
            tables.append(_catalog_table(node, catalog, policy))

    warnings: list[str] = []
    if isinstance(tree, exp.Select):
        sources = _from_sources(tree, catalog)
        if sources:
            warnings += _expand_root_star(tree, sources)

    leftmost = _first_select(tree)
    if leftmost is not None:
        _name_expressions(leftmost)
    names = _canonical_names(tree, tables)
    schema = MappingSchema({t.name: {c.name: c.type for c in t.columns} for t in tables}, dialect="tsql")
    try:  # qualify lower-cases names (T-SQL compares them case-insensitively); Sage's casing is restored below
        qualified = qualify(tree, schema=schema, dialect="tsql", validate_qualify_columns=True, quote_identifiers=True)
    except OptimizeError as exc:
        raise _column_error(str(exc), tables, catalog, names) from None
    except Exception as exc:  # sqlglot raises plain exceptions for constructs it cannot scope
        raise SageError(ErrorCode.PARSE_ERROR, f"The query could not be resolved against the catalog: {exc}") from None
    root = build_scope(qualified) if isinstance(qualified, exp.Query) else None
    if root is None or not isinstance(qualified, exp.Query):
        raise SageError(ErrorCode.PARSE_ERROR, "The query could not be resolved against the catalog.")

    allowed = _direct_projections(root)
    for scope in traverse_scope(qualified):
        for column in scope.columns:
            table = _source_table(scope, column, catalog)
            entry = table.column(column.name) if table else None
            if table is not None and entry is not None and entry.masked and id(column) not in allowed:
                raise SageError(
                    ErrorCode.PERMISSION_DENIED,
                    f"{table.name}.{entry.name} is masked by policy, so it can only be selected, not filtered, "
                    "joined, sorted, grouped or computed on.",
                    hint="Select it directly from the outer query; its values come back as ***.",
                )

    branches = _branches(root)
    outputs: list[OutputColumn] = []
    first = branches[0].expression
    for position, projection in enumerate(first.expressions if isinstance(first, exp.Select) else []):
        found: list[tuple[Table, Column]] = []
        for branch in branches:  # a set-operation output is masked if any branch puts a masked column there
            select = branch.expression
            if not isinstance(select, exp.Select) or position >= len(select.expressions):
                continue
            item = select.expressions[position]
            node = item.this if isinstance(item, exp.Alias) else item
            table = _source_table(branch, node, catalog) if isinstance(node, exp.Column) else None
            entry = table.column(node.name) if table is not None and isinstance(node, exp.Column) else None
            if table is not None and entry is not None:
                found.append((table, entry))
        masked = next(((t, c) for t, c in found if c.masked), None)
        source = masked or (found[0] if found and len(branches) == 1 else None)
        name = names.get(projection.alias_or_name.lower(), projection.alias_or_name)
        outputs.append(OutputColumn(name, *(source or (None, None))))

    for identifier in qualified.find_all(exp.Identifier):
        identifier.set("this", names.get(identifier.name.lower(), identifier.name))
    return ResolvedQuery(
        tree=qualified,
        tables=tuple({t.name: t for t in tables}.values()),
        outputs=tuple(outputs),
        warnings=tuple(warnings),
    )


def _column_error(message: str, tables: list[Table], catalog: Catalog, names: dict[str, str]) -> SageError:
    match = _UNRESOLVED.search(message)
    name = names.get(match.group(1).lower(), match.group(1)) if match else None
    if name:
        holders = [t.name for t in tables if t.column(name)]
        if len(holders) > 1:
            return SageError(
                ErrorCode.UNKNOWN_COLUMN,
                f"Column {name} is ambiguous: it is in {', '.join(holders)}.",
                hint="Qualify it with a table alias, such as c.CustomerNo.",
            )
        suggestions = list(dict.fromkeys(s for t in tables for s in catalog.suggest_column(t, name)))  # closest first
        where = ", ".join(t.name for t in tables) or "the referenced tables"
        return SageError(
            ErrorCode.UNKNOWN_COLUMN,
            f"There is no column {name} in {where}.",
            hint=f"Did you mean {', '.join(suggestions)}?" if suggestions else "Use describe_table to see the columns.",
            choices=suggestions or None,
        )
    return SageError(ErrorCode.UNKNOWN_COLUMN, f"A column could not be resolved: {message}")
