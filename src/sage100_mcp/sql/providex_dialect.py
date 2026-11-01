"""ProvideX SQL from a resolved query (SPEC.md §5.1, §5.2, §5.5 step 4).

The writer covers the subset Sage's driver build can run: joins inside {oj} or {IJ}, scalar
functions as {fn ...}, dates as {d '...'}, no semicolons, brackets, TOP or aliases. It asks the
capability profile before using anything beyond that, and records every feature the profile lacks
instead of stopping at the first, so the planner (and explain_query) can say why a query has to
run locally. Output columns are named by the resolver, so the SQL never needs aliases.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlglot import exp

from sage100_mcp.capabilities import Capabilities
from sage100_mcp.catalog.model import Catalog, Column, Table
from sage100_mcp.sql.bind import Bound

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING = re.compile(r"'(?:[^']|'')*'")
_JOIN_ESCAPE = re.compile(r"\{\s*(oj|ij)\b", re.IGNORECASE)
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[T ]00:00(?::00(?:\.0+)?)?)?")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_COMPARISONS: dict[type[exp.Expr], str] = {
    exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=",
}  # fmt: skip
_ARITHMETIC: dict[type[exp.Expr], str] = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/"}
_UNARY_FUNCTIONS: dict[type[exp.Expr], str] = {
    exp.Upper: "UCASE", exp.Lower: "LCASE", exp.Length: "LENGTH", exp.Abs: "ABS", exp.Floor: "FLOOR",
    exp.Ceil: "CEILING", exp.Year: "YEAR", exp.Month: "MONTH", exp.Quarter: "QUARTER", exp.Week: "WEEK",
    exp.DayOfWeek: "DAYOFWEEK", exp.DayOfMonth: "DAYOFMONTH", exp.Day: "DAYOFMONTH",
}  # fmt: skip
_EXTRACT_PARTS = {"YEAR": "YEAR", "MONTH": "MONTH", "QUARTER": "QUARTER", "WEEK": "WEEK", "DAY": "DAYOFMONTH"}
_INTERVALS = {"DAY": "SQL_TSI_DAY", "WEEK": "SQL_TSI_WEEK", "MONTH": "SQL_TSI_MONTH", "QUARTER": "SQL_TSI_QUARTER",
              "YEAR": "SQL_TSI_YEAR"}  # fmt: skip
_TEXT_FUNCTIONS = (
    exp.Upper, exp.Lower, exp.Substring, exp.Concat, exp.DPipe, exp.Trim, exp.Left, exp.Right, exp.Replace,
)  # fmt: skip
_CAST_TYPES = {
    exp.DataType.Type.VARCHAR: "VARCHAR", exp.DataType.Type.CHAR: "CHAR", exp.DataType.Type.DECIMAL: "DECIMAL",
    exp.DataType.Type.INT: "INTEGER", exp.DataType.Type.BIGINT: "INTEGER", exp.DataType.Type.DATE: "DATE",
}  # fmt: skip


def _blank_strings(sql: str) -> str:
    return _STRING.sub(lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", sql)


def _closing(masked: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(masked)):
        if masked[index] == opening:
            depth += 1
        elif masked[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def unescape_joins(sql: str) -> str:
    """ProvideX text with its {oj ...} and {IJ ...} join escapes opened, so the guard can parse it.

    sqlglot reads {d}, {ts}, {t} and {fn} itself. Used for raw dialect='providex' input, which is
    guarded and resolved like any query but sent to the driver as written.
    """
    while True:
        masked = _blank_strings(sql)
        found = _JOIN_ESCAPE.search(masked)
        if found is None:
            return sql
        end = _closing(masked, found.start(), "{", "}")
        if end is None:
            return sql  # unbalanced: the guard reports the parse error
        body = sql[found.end() : end].strip()
        if found.group(1).lower() == "ij" and body.startswith("("):
            # {IJ (a INNER JOIN b ON x) INNER JOIN c ON y}: inner joins nest either way
            close = _closing(_blank_strings(body), 0, "(", ")")
            if close is not None:
                body = body[1:close] + " " + body[close + 1 :]
        sql = sql[: found.start()] + " " + body + " " + sql[end + 1 :]


@dataclass(frozen=True)
class Missing:
    """A feature the statement needs that the driver profile doesn't have."""

    feature: str
    detail: str


@dataclass
class Native:
    sql: str
    params: list[Bound] = field(default_factory=list)
    fetch_limit: int | None = None  # rows to keep when a TOP or LIMIT could not be sent
    fetch_offset: int = 0  # rows to skip when an OFFSET could not be sent
    missing: list[Missing] = field(default_factory=list)
    used: list[str] = field(default_factory=list)  # the driver features the text relies on

    @property
    def runnable(self) -> bool:
        return not self.missing


class _Source:
    def __init__(self, alias: str, table: Table) -> None:
        self.alias = alias
        self.table = table


class ProvideXWriter:
    """One statement's ProvideX text, its bound values in marker order, and what it is missing."""

    def __init__(
        self,
        capabilities: Capabilities,
        catalog: Catalog,
        params: Sequence[Bound] = (),
        *,
        row_cap: int | None = None,
    ) -> None:
        self.caps = capabilities
        self.catalog = catalog
        self.params = params
        self.row_cap = row_cap
        self.missing: list[Missing] = []
        self.used: list[str] = []
        self.out_params: list[Bound] = []
        self.sources: dict[str, _Source] = {}
        self.qualify = False
        self.fetch_limit: int | None = None
        self.fetch_offset = 0

    # --- entry points ---------------------------------------------------------------------------

    def statement(self, tree: exp.Query) -> Native:
        tree = tree.copy()  # the comma-join rewrite moves ON conditions into WHERE
        if tree.args.get("with_"):
            self._lack("cte", "WITH")
        if isinstance(tree, exp.SetOperation):
            sql = self._set_operation(tree, top=True)
        elif isinstance(tree, exp.Select):
            sql = self._select(tree, top=True)
        else:
            self._lack("statement", type(tree).__name__)
            sql = ""
        return Native(sql, self.out_params, self.fetch_limit, self.fetch_offset, self.missing, self.used)

    def _lack(self, feature: str, detail: str) -> None:
        if not any(m.feature == feature and m.detail == detail for m in self.missing):
            self.missing.append(Missing(feature, detail))

    def _need(self, feature: str, detail: str) -> bool:
        if feature not in self.used:
            self.used.append(feature)
        if self.caps.supports(feature):
            return True
        self._lack(feature, detail)
        return False

    # --- statements --------------------------------------------------------------------------------

    def _set_operation(self, tree: exp.SetOperation, *, top: bool) -> str:
        if not isinstance(tree, exp.Union):
            self._lack("set_operation", type(tree).__name__.upper())
            return ""
        feature = "union" if tree.args.get("distinct") else "union_all"
        keyword = " UNION " if feature == "union" else " UNION ALL "
        self._need(feature, keyword.strip())
        parts = []
        for branch in (tree.this, tree.expression):
            while isinstance(branch, exp.Subquery):
                branch = branch.this
            if isinstance(branch, exp.Union):
                parts.append(self._set_operation(branch, top=False))
            elif isinstance(branch, exp.Select):
                parts.append(self._select(branch, top=False))
            else:
                self._lack("set_operation", type(branch).__name__)
        sql = keyword.join(parts)
        order = tree.args.get("order")
        if top:
            self._limits(tree, ordered=order is not None, plain=False)
            if order is not None:
                sql += self._order_by(order, _first_select(tree), positions_only=True)
        elif order is not None or tree.args.get("limit") is not None:
            self._lack("order_by", "ORDER BY or LIMIT inside a UNION")
        return sql

    def _select(self, select: exp.Select, *, top: bool, nested: bool = False) -> str:
        if select.args.get("with_"):
            self._lack("cte", "WITH")
        outer = (self.sources, self.qualify)
        self._sources(select, nested=nested)
        parts = ["SELECT"]
        if select.args.get("distinct"):
            if select.args["distinct"].args.get("on"):
                self._lack("distinct_on", "DISTINCT ON")
            self._need("distinct", "SELECT DISTINCT")
            parts.append("DISTINCT")
        projections = []
        for item in select.expressions:
            node = item.this if isinstance(item, exp.Alias) else item
            projections.append(self._expr(node))
        parts.append(", ".join(projections))
        parts.append("FROM " + self._from(select))
        where = select.args.get("where")
        if where is not None:
            parts.append("WHERE " + self._condition(where.this))
        group = select.args.get("group")
        if group is not None:
            parts.append("GROUP BY " + ", ".join(self._group_item(g, select) for g in group.expressions))
        having = select.args.get("having")
        if having is not None:
            self._need("having", "HAVING")
            parts.append("HAVING " + self._condition(having.this))
        sql = " ".join(parts)
        order = select.args.get("order")
        if top:
            sql += self._limits(select, ordered=order is not None, plain=True)
            if order is not None:
                sql += self._order_by(order, select, positions_only=False)
        elif order is not None or select.args.get("limit") is not None:
            self._lack("order_by", "ORDER BY or LIMIT inside a subquery or UNION branch")
        self.sources, self.qualify = outer
        return sql

    def _limits(self, query: exp.Query, *, ordered: bool, plain: bool) -> str:
        """LIMIT/OFFSET or TOP from the query, then the row cap; returns SQL to append before ORDER BY."""
        limit_node, offset_node = query.args.get("limit"), query.args.get("offset")
        limit = _limit_value(limit_node)
        offset = _int_value(offset_node.expression) if isinstance(offset_node, exp.Offset) else None
        has_limit = limit_node is not None or offset_node is not None
        if (limit_node is not None and limit is None) or (offset_node is not None and offset is None):
            self._lack("limit", "TOP, LIMIT or OFFSET with a value other than a whole number")
            return ""
        if has_limit and ordered:
            # The grammar puts LIMIT before ORDER BY, so the driver could cut rows before sorting them.
            self._lack("order_by_with_limit", "ORDER BY with TOP, LIMIT or OFFSET")
            return ""
        if limit is not None or offset:
            if plain and self.caps.supports("limit_offset" if offset else "limit"):
                return f" LIMIT {limit if limit is not None else 2**31 - 1}" + (f" OFFSET {offset}" if offset else "")
            self.fetch_limit, self.fetch_offset = limit, offset or 0
            return ""
        if plain and not ordered and self.row_cap is not None and self.caps.supports("limit"):
            return f" LIMIT {self.row_cap}"
        return ""

    # --- FROM and joins ----------------------------------------------------------------------------

    def _sources(self, select: exp.Select, *, nested: bool) -> None:
        sources: dict[str, _Source] = {}
        nodes = [select.args["from_"].this] if select.args.get("from_") else []
        nodes += [join.this for join in select.args.get("joins") or []]
        for node in nodes:
            if isinstance(node, exp.Table):
                table = self.catalog.table(node.name)
                if table is not None:
                    sources[node.alias_or_name] = _Source(node.alias_or_name, table)
        # A subquery may name the outer query's tables, so its columns are always qualified.
        self.sources = {**self.sources, **sources} if nested else sources
        self.qualify = len(nodes) > 1 or nested or (self.qualify and nested)

    def _table_ref(self, node: exp.Expr) -> str:
        if isinstance(node, exp.Table):
            if self.catalog.table(node.name) is None:
                self._lack("cte", "WITH")  # resolve already refused unknown tables, so this is a WITH name
            alias = node.alias_or_name
            name = _ident(node.name)
            if self.qualify and alias != node.name:
                self._need("table_alias", f"{node.name} {alias}")
                return f"{name} {_ident(alias)}"
            return name
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select):
            self._need("derived_table", "FROM (SELECT ...)")
            text = self._select(node.this, top=False, nested=True)
            return f"({text}) {_ident(node.alias_or_name)}"
        self._lack("from", type(node).__name__)
        return ""

    def _from(self, select: exp.Select) -> str:
        from_ = select.args.get("from_")
        if from_ is None:
            self._lack("no_from", "SELECT without FROM")
            return ""
        first = self._table_ref(from_.this)
        joins = select.args.get("joins") or []
        if not joins:
            return first
        sides = {(join.args.get("side") or "").upper() for join in joins}
        if sides - {"", "LEFT"}:
            self._lack("outer_join", f"{'/'.join(sorted(sides - {''}))} JOIN")
            return first
        if "LEFT" in sides:
            return self._left_joins(select, first, joins)
        return self._inner_joins(select, first, joins)

    def _on(self, join: exp.Join) -> exp.Expr | None:
        if join.args.get("using"):
            self._lack("join_using", "JOIN ... USING")
        on = join.args.get("on")
        if on is None and (join.args.get("kind") or "").upper() != "CROSS":
            self._lack("join", "JOIN without ON")
        return on

    def _left_joins(self, select: exp.Select, first: str, joins: list[exp.Join]) -> str:
        if any((join.args.get("side") or "").upper() != "LEFT" for join in joins):
            self._lack("mixed_joins", "INNER and LEFT joins in one statement")
            return first
        self._need("oj_left" if len(joins) == 1 else "oj_left_chain", f"{len(joins)} LEFT OUTER JOIN(s)")
        outer = {join.this.alias_or_name for join in joins if isinstance(join.this, exp.Table)}
        where = select.args.get("where")
        if where is not None and any(column.table in outer for column in where.find_all(exp.Column)):
            # Sage's driver applies such a filter as an inner join (§5.2), so the result would differ.
            self._lack("outer_join_where", "WHERE on a LEFT-joined table")
        parts = [first]
        for join in joins:
            on = self._on(join)
            table = self._table_ref(join.this)  # written before the condition: ? values follow the text
            condition = f" ON ({self._condition(on)})" if on is not None else ""
            parts.append(f"LEFT OUTER JOIN {table}{condition}")
        return "{oj " + " ".join(parts) + "}"

    def _inner_joins(self, select: exp.Select, first: str, joins: list[exp.Join]) -> str:
        # FROM a, b and CROSS JOIN have no ON to put in a join escape; they stay comma joins.
        crosses = any((j.args.get("kind") or "").upper() == "CROSS" or j.args.get("on") is None for j in joins)
        if not crosses and len(joins) <= 2 and self.caps.supports("ij" if len(joins) == 1 else "ij_nested"):
            text = first
            for position, join in enumerate(joins):
                on = self._on(join)
                piece = f"{text} INNER JOIN {self._table_ref(join.this)} ON {self._condition(on) if on else ''}"
                text = f"({piece})" if position == 0 and len(joins) > 1 else piece
            return "{IJ " + text + "}"
        if not crosses and len(joins) == 1 and self.caps.supports("oj_inner"):
            on = self._on(joins[0])
            table = self._table_ref(joins[0].this)
            condition = self._condition(on) if on is not None else ""
            return "{oj " + f"{first} INNER JOIN {table} ON ({condition})" + "}"
        self._need("comma_join", "FROM a, b with the join conditions in WHERE")
        tables = [first] + [self._table_ref(join.this) for join in joins]
        conditions = []
        for join in joins:
            if join.args.get("using"):
                self._lack("join_using", "JOIN ... USING")
            if join.args.get("on") is not None:
                conditions.append(join.args["on"])
        where = select.args.get("where")
        if conditions:  # the ON conditions join WHERE, and _select writes WHERE from the rewritten tree
            combined = exp.and_(*conditions, *([where.this] if where is not None else []))
            select.set("where", exp.Where(this=combined))
            select.set("joins", [])
        return ", ".join(tables)

    # --- clauses ------------------------------------------------------------------------------------

    def _group_item(self, node: exp.Expr, select: exp.Select) -> str:
        self._need("group_by", "GROUP BY")
        if isinstance(node, exp.Column) and node.table:
            return self._expr(node)
        aliased = _projection(select, node)
        if isinstance(aliased, exp.Column) and aliased.table:
            return self._expr(aliased)
        self._need("group_by_expression", "GROUP BY an expression")
        return self._expr(aliased if aliased is not None else node)

    def _order_by(self, order: exp.Order, select: exp.Select | None, *, positions_only: bool) -> str:
        items = []
        for ordered in order.expressions:
            node = ordered.this
            position = None
            if isinstance(node, exp.Literal) and not node.is_string:
                position = int(node.this)
            elif isinstance(node, exp.Column) and not node.table:
                position = _position(select, node.name) if select is not None else None
                if position is None and positions_only:
                    self._lack("order_by", f"ORDER BY {node.name}")
            if position is not None:
                self._need("order_by_position", "ORDER BY a position")
                text = str(position)
            elif isinstance(node, exp.Column) and node.table and not positions_only:
                self._need("order_by_column", "ORDER BY a column")
                text = self._expr(node)
            else:
                self._lack("order_by_expression", "ORDER BY an expression")
                text = ""
            if ordered.args.get("desc"):
                self._need("order_by_desc", "ORDER BY ... DESC")
                text += " DESC"
            items.append(text)
        return " ORDER BY " + ", ".join(items)

    # --- conditions -----------------------------------------------------------------------------------

    def _condition(self, node: exp.Expr) -> str:
        if isinstance(node, exp.And):
            conjuncts = list(node.flatten())
            if len(conjuncts) > 1:
                self._need("compound_and", "several AND conditions")
            return " AND ".join(self._condition(c) for c in conjuncts)
        if isinstance(node, exp.Or):
            rewritten = _or_to_in(node)
            if rewritten is not None:
                return self._condition(rewritten)
            self._need("or", "OR")
            return "(" + " OR ".join(self._condition(d) for d in node.flatten()) + ")"
        if isinstance(node, exp.Paren):
            return "(" + self._condition(node.this) + ")"
        return self._expr(node)

    # --- expressions ----------------------------------------------------------------------------------

    def _column(self, node: exp.Column) -> Column | None:
        source = self.sources.get(node.table) if node.table else None
        if source is None and len(self.sources) == 1:
            source = next(iter(self.sources.values()))
        return source.table.column(node.name) if source is not None else None

    def _is_date(self, node: exp.Expr) -> bool:
        if isinstance(node, exp.Column):
            column = self._column(node)
            return column is not None and column.type.lower().startswith(("date", "datetime", "smalldatetime"))
        return isinstance(node, exp.Date | exp.CurrentDate)

    def _expr(self, node: exp.Expr, *, as_date: bool = False) -> str:
        if isinstance(node, exp.TimeStrToTime | exp.TsOrDsToDate | exp.TsOrDsToTimestamp):
            # sqlglot's markers for T-SQL date semantics; ODBC date functions take the value as is.
            return self._expr(node.this, as_date=as_date)
        if isinstance(node, exp.Column):
            name = _ident(node.name)
            return f"{_ident(node.table)}.{name}" if self.qualify and node.table else name
        if isinstance(node, exp.Literal):
            return self._literal(node, as_date=as_date)
        if isinstance(node, exp.Null):
            return "NULL"
        if isinstance(node, exp.Placeholder):
            return self._param(node, as_date=as_date)
        if isinstance(node, exp.Paren):
            return "(" + self._expr(node.this, as_date=as_date) + ")"
        if isinstance(node, exp.Neg):
            if self.caps.supports("negative_literal") and isinstance(node.this, exp.Literal):
                return "-" + self._expr(node.this)
            self._need("arithmetic", "a negative value written as 0 - x")
            return f"(0 - {self._expr(node.this)})"
        if type(node) in _COMPARISONS:
            dates = self._is_date(node.this) or self._is_date(node.expression)
            left = self._expr(node.this, as_date=dates)
            right = self._expr(node.expression, as_date=dates)
            return f"{left} {_COMPARISONS[type(node)]} {right}"
        if type(node) in _ARITHMETIC:
            if isinstance(node, exp.Add) and (self._is_text(node.this) or self._is_text(node.expression)):
                return self._function("CONCAT", [node.this, node.expression])
            self._need("arithmetic", "arithmetic")
            return f"({self._expr(node.this)} {_ARITHMETIC[type(node)]} {self._expr(node.expression)})"
        if isinstance(node, exp.Mod):
            return self._function("MOD", [node.this, node.expression])
        if isinstance(node, exp.DPipe):
            return self._function("CONCAT", [node.this, node.expression])
        if isinstance(node, exp.In):
            return self._in(node)
        if isinstance(node, exp.Between):
            self._need("between", "BETWEEN")
            dates = self._is_date(node.this)
            subject = self._expr(node.this)
            low = self._expr(node.args["low"], as_date=dates)
            high = self._expr(node.args["high"], as_date=dates)
            return f"{subject} BETWEEN {low} AND {high}"
        if isinstance(node, exp.Like):  # sqlglot writes NOT LIKE as Like(negate=True)
            self._need("like", "LIKE")
            keyword = " NOT LIKE " if node.args.get("negate") else " LIKE "
            return self._expr(node.this) + keyword + self._expr(node.expression)
        if isinstance(node, exp.ILike):
            self._lack("ilike", "ILIKE")
            return ""
        if isinstance(node, exp.Escape):
            self._lack("like_escape", "LIKE ... ESCAPE")
            return ""
        if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
            self._need("is_null", "IS NULL")
            return self._expr(node.this) + (" IS NOT NULL" if node.args.get("negate") else " IS NULL")
        if isinstance(node, exp.Not):
            return self._not(node)
        if isinstance(node, exp.Exists):
            self._need("exists", "EXISTS")
            return f"EXISTS ({self._subselect(node.this)})"
        if isinstance(node, exp.And | exp.Or):
            return "(" + self._condition(node) + ")"
        if isinstance(node, exp.Case):
            return self._case(node)
        if isinstance(node, exp.Cast):
            return self._cast(node)
        if isinstance(node, exp.Date) and isinstance(node.this, exp.Literal) and node.this.is_string:
            return self._date_literal(node.this.this)
        if isinstance(node, exp.Count):
            return self._count(node)
        if isinstance(node, exp.Boolean):
            self._lack("boolean", "TRUE or FALSE")
            return ""
        if isinstance(node, exp.Subquery):
            self._lack("scalar_subquery", "a subquery used as a value")
            return ""
        if isinstance(node, exp.Window):
            self._lack("window", "a window function (OVER)")
            return ""
        if isinstance(node, exp.Sum | exp.Avg | exp.Min | exp.Max):
            return self._aggregate(node)
        return self._scalar_function(node)

    def _is_text(self, node: exp.Expr) -> bool:
        if isinstance(node, exp.Literal):
            return bool(node.is_string)
        if isinstance(node, exp.Column):
            column = self._column(node)
            return column is not None and column.type.lower().startswith(("varchar", "char", "nvarchar"))
        return isinstance(node, _TEXT_FUNCTIONS)

    def _literal(self, node: exp.Literal, *, as_date: bool) -> str:
        if node.is_string:
            if as_date:
                return self._date_literal(node.this)
            return "'" + str(node.this).replace("'", "''") + "'"
        text = str(node.this)
        if not _NUMBER.fullmatch(text):
            self._lack("number", f"number literal {text}")
        return text

    def _date_literal(self, text: str) -> str:
        found = _ISO_DATE.fullmatch(text.strip())
        if found is None:
            self._lack("date_value", f"'{text}' is not a YYYY-MM-DD date")
            return "NULL"
        try:
            date.fromisoformat(found.group(1))
        except ValueError:
            self._lack("date_value", f"'{text}' is not a valid date")
            return "NULL"
        self._need("date_literal", "{d 'yyyy-mm-dd'}")
        return "{d '" + found.group(1) + "'}"

    def _param(self, node: exp.Placeholder, *, as_date: bool) -> str:
        index = node.meta.get("param")
        if not isinstance(index, int) or index >= len(self.params):
            self._lack("parameters", "a ? marker without a value")
            return "?"
        value = self.params[index]
        if self.caps.supports("parameters"):
            if as_date and isinstance(value, str):
                found = _ISO_DATE.fullmatch(value)
                if found is None:
                    self._lack("date_value", f"'{value}' is not a YYYY-MM-DD date")
                else:
                    value = date.fromisoformat(found.group(1))
            self.out_params.append(value)
            return "?"
        return self._inline(value, as_date=as_date)

    def _inline(self, value: Bound, *, as_date: bool) -> str:
        """A bound value written as a typed literal, for drivers that reject ? markers (§5.5 step 5)."""
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            self._lack("boolean", "a true/false value")
            return "NULL"
        if isinstance(value, datetime):
            if value.time() != datetime.min.time():
                self._lack("datetime_value", "a date and time value")
            return self._date_literal(value.date().isoformat())
        if isinstance(value, date):
            return self._date_literal(value.isoformat()) if as_date else f"'{value.isoformat()}'"
        if isinstance(value, int | Decimal):
            text = format(value, "f") if isinstance(value, Decimal) else str(value)
            if text.startswith("-"):
                return self._expr(exp.Neg(this=exp.Literal.number(text[1:])))
            return text
        if as_date:
            return self._date_literal(value)
        return "'" + value.replace("'", "''") + "'"

    def _in(self, node: exp.In) -> str:
        query = node.args.get("query")
        if query is not None:
            self._need("subquery_in", "IN (SELECT ...)")
            subquery = query.this if isinstance(query, exp.Subquery) else query
            return f"{self._expr(node.this)} IN ({self._subselect(subquery)})"
        if node.args.get("unnest") or node.args.get("field"):
            self._lack("in", "IN with a non-list value")
            return ""
        self._need("in_list", "IN (list)")
        dates = self._is_date(node.this)
        subject = self._expr(node.this)
        values = ", ".join(self._expr(v, as_date=dates) for v in node.expressions)
        return f"{subject} IN ({values})"

    def _not(self, node: exp.Not) -> str:
        inner = node.this
        if isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, exp.In | exp.Like | exp.Between):
            text = self._expr(inner)
            keyword = {exp.In: " IN ", exp.Like: " LIKE ", exp.Between: " BETWEEN "}[type(inner)]
            return text.replace(keyword, " NOT" + keyword, 1)
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            self._need("is_null", "IS NOT NULL")
            return f"{self._expr(inner.this)} IS NOT NULL"
        if isinstance(inner, exp.Exists):
            self._need("exists", "NOT EXISTS")
            return f"NOT EXISTS ({self._subselect(inner.this)})"
        self._lack("not", "NOT applied to a condition")
        return ""

    def _subselect(self, node: exp.Expr) -> str:
        while isinstance(node, exp.Subquery):
            node = node.this
        if not isinstance(node, exp.Select):
            self._lack("subquery", type(node).__name__)
            return ""
        return self._select(node, top=False, nested=True)

    def _case(self, node: exp.Case) -> str:
        self._need("case", "CASE")
        parts = ["CASE"]
        if node.this is not None:
            parts.append(self._expr(node.this))
        for branch in node.args.get("ifs") or []:
            parts.append(f"WHEN {self._condition(branch.this)} THEN {self._expr(branch.args['true'])}")
        default = node.args.get("default")
        if default is not None:
            parts.append(f"ELSE {self._expr(default)}")
        parts.append("END")
        return " ".join(parts)

    def _cast(self, node: exp.Cast) -> str:
        target = node.to
        if (
            target.this == exp.DataType.Type.DATE
            and isinstance(node.this, exp.Literal)
            and node.this.is_string
            and not isinstance(node, exp.TryCast)
        ):
            return self._date_literal(node.this.this)
        if target.this == exp.DataType.Type.TEXT and not isinstance(node, exp.TryCast) and self._is_text(node.this):
            # The T-SQL reader wraps string-function arguments in CAST(x AS TEXT); text already is text.
            return self._expr(node.this)
        name = _CAST_TYPES.get(target.this)
        if isinstance(node, exp.TryCast) or name is None:
            self._lack("cast", f"CAST to {target.sql()}")
            return ""
        self._need("cast", "CAST")
        size = ", ".join(p.sql() for p in target.expressions)
        return f"CAST({self._expr(node.this)} AS {name}{f'({size})' if size else ''})"

    def _count(self, node: exp.Count) -> str:
        argument = node.this
        if isinstance(argument, exp.Star) or argument is None:
            return "COUNT(*)"
        if isinstance(argument, exp.Distinct):
            self._need("count_distinct", "COUNT(DISTINCT x)")
            return "COUNT(DISTINCT " + ", ".join(self._expr(e) for e in argument.expressions) + ")"
        return f"COUNT({self._expr(argument)})"

    def _aggregate(self, node: exp.Expr) -> str:
        name = type(node).__name__.upper()
        argument = node.this
        if isinstance(argument, exp.Distinct):
            self._lack("aggregate_distinct", f"{name}(DISTINCT x)")
            return ""
        if isinstance(node, exp.Sum | exp.Avg) and not self._exact(argument):
            self._need("aggregate_exact", f"{name} of a decimal column")
        return f"{name}({self._expr(argument)})"

    def _exact(self, node: exp.Expr) -> bool:
        """True when a SUM/AVG argument needs no exact-decimal guarantee (not a decimal column)."""
        if isinstance(node, exp.Column):
            column = self._column(node)
            return column is None or not column.type.lower().startswith(("decimal", "numeric"))
        return False

    def _scalar_function(self, node: exp.Expr) -> str:
        if isinstance(node, exp.Length):
            argument = node.this
            if isinstance(argument, exp.Cast) and argument.to.this == exp.DataType.Type.TEXT:
                argument = argument.this  # T-SQL LEN; ODBC LENGTH also ignores trailing blanks
            return self._function("LENGTH", [argument])
        if type(node) in _UNARY_FUNCTIONS:
            return self._function(_UNARY_FUNCTIONS[type(node)], [node.this])
        if isinstance(node, exp.Extract):
            part = node.this.name.upper() if isinstance(node.this, exp.Var | exp.Identifier) else ""
            if part in _EXTRACT_PARTS:
                return self._function(_EXTRACT_PARTS[part], [node.expression])
        elif isinstance(node, exp.Substring):
            args = [node.this, node.args.get("start"), node.args.get("length")]
            return self._function("SUBSTRING", [a for a in args if a is not None])
        elif isinstance(node, exp.Trim):
            position = (node.args.get("position") or "").upper()
            if node.args.get("expression") is None:
                if position == "LEADING":
                    return self._function("LTRIM", [node.this])
                if position == "TRAILING":
                    return self._function("RTRIM", [node.this])
                inner = self._function("RTRIM", [node.this])
                return self._wrap("LTRIM", inner)
        elif isinstance(node, exp.Left | exp.Right):
            return self._function(type(node).__name__.upper(), [node.this, node.expression])
        elif isinstance(node, exp.Concat):
            texts = [self._expr(arg) for arg in node.expressions]  # in written order, for the ? values
            text = texts[-1] if texts else "''"
            for earlier in reversed(texts[:-1]):
                text = self._wrap("CONCAT", earlier + ", " + text)
            return text
        elif isinstance(node, exp.StrPosition):
            args = [node.args.get("substr"), node.this, node.args.get("position")]
            return self._function("LOCATE", [a for a in args if a is not None])
        elif isinstance(node, exp.Replace):
            return self._function("REPLACE", [node.this, node.expression, node.args.get("replacement")])
        elif isinstance(node, exp.Round):
            decimals = node.args.get("decimals") or exp.Literal.number(0)
            return self._function("ROUND", [node.this, decimals])
        elif isinstance(node, exp.CurrentDate):
            return self._wrap("CURDATE", "")
        elif isinstance(node, exp.CurrentTimestamp):
            return self._wrap("NOW", "")
        elif isinstance(node, exp.Coalesce) and len(node.expressions) == 1:
            return self._function("IFNULL", [node.this, node.expressions[0]])
        elif isinstance(node, exp.DateAdd | exp.DateDiff):
            unit = node.args.get("unit")
            interval = _INTERVALS.get(unit.name.upper() if unit is not None else "DAY")
            if interval is not None:
                if isinstance(node, exp.DateAdd):
                    return self._function("TIMESTAMPADD", [exp.var(interval), node.expression, node.this])
                return self._function("TIMESTAMPDIFF", [exp.var(interval), node.expression, node.this])
        if isinstance(node, exp.Anonymous):
            name = node.name
        else:
            name = node.sql_name() if isinstance(node, exp.Func) else type(node).__name__
        self._lack(f"fn:{name.upper()}", f"{name} has no ProvideX equivalent here")
        return ""

    def _function(self, name: str, args: list[exp.Expr | None]) -> str:
        text = ", ".join(_var_or(self, a) for a in args)
        return self._wrap(name, text)

    def _wrap(self, name: str, args: str) -> str:
        form = self.caps.function_form(name)
        if form is None:
            self._lack(f"fn:{name}", f"{name}()")
            return f"{name}({args})"
        return "{fn " + f"{name}({args})" + "}" if form == "escaped" else f"{name}({args})"


def _var_or(writer: ProvideXWriter, node: exp.Expr | None) -> str:
    if isinstance(node, exp.Var):
        return node.name
    if node is None:
        writer._lack("function", "a missing argument")
        return ""
    return writer._expr(node)


def _ident(name: str) -> str:
    return name if _NAME.fullmatch(name) else '"' + name.replace('"', '""') + '"'


def _int_value(node: exp.Expr | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string and str(node.this).isdigit():
        return int(node.this)
    return None


def _limit_value(node: exp.Expr | None) -> int | None:
    if isinstance(node, exp.Limit):
        return _int_value(node.expression)
    if isinstance(node, exp.Fetch):
        return _int_value(node.args.get("count"))
    return None


def _first_select(tree: exp.Expr) -> exp.Select | None:
    while isinstance(tree, exp.SetOperation | exp.Subquery):
        tree = tree.this
    return tree if isinstance(tree, exp.Select) else None


def _position(select: exp.Select | None, name: str) -> int | None:
    if select is None:
        return None
    for index, item in enumerate(select.expressions, start=1):
        if item.alias_or_name.lower() == name.lower():
            return index
    return None


def _unalias(item: exp.Expr) -> exp.Expr:
    inner: exp.Expr = item.this if isinstance(item, exp.Alias) else item
    return inner


def _projection(select: exp.Select, node: exp.Expr) -> exp.Expr | None:
    """The select-list expression a GROUP BY alias or position stands for."""
    if isinstance(node, exp.Literal) and not node.is_string and str(node.this).isdigit():
        index = int(node.this) - 1
        if 0 <= index < len(select.expressions):
            return _unalias(select.expressions[index])
    if isinstance(node, exp.Column) and not node.table:
        for item in select.expressions:
            if item.alias_or_name.lower() == node.name.lower():
                return _unalias(item)
    return None


def _or_to_in(node: exp.Or) -> exp.Expr | None:
    """``a = 1 OR a = 2`` as ``a IN (1, 2)``: an OR the driver needn't support (§5.2)."""
    column: exp.Column | None = None
    values: list[exp.Expr] = []
    for disjunct in node.flatten():
        while isinstance(disjunct, exp.Paren):
            disjunct = disjunct.this
        if isinstance(disjunct, exp.EQ) and isinstance(disjunct.this, exp.Column):
            left, value = disjunct.this, disjunct.expression
        elif isinstance(disjunct, exp.EQ) and isinstance(disjunct.expression, exp.Column):
            left, value = disjunct.expression, disjunct.this
        elif isinstance(disjunct, exp.In) and isinstance(disjunct.this, exp.Column) and not disjunct.args.get("query"):
            left, value = disjunct.this, None
        else:
            return None
        if column is None:
            column = left
        elif (left.table, left.name.lower()) != (column.table, column.name.lower()):
            return None
        if value is None:
            values.extend(disjunct.expressions)
        elif isinstance(value, exp.Literal | exp.Placeholder | exp.Neg):
            values.append(value)
        else:
            return None
    if column is None:
        return None
    return exp.In(this=column.copy(), expressions=[v.copy() for v in values])
