"""A stand-in for Sage's ProvideX ODBC driver (SPEC.md §15.2): tier-2 tests on any OS.

It is a DB-API shim with pyodbc's surface (connect, cursor, execute, fetchmany, tables, columns,
primaryKeys, getinfo, cancel, timeout) over DuckDB. It accepts the {oj}, {IJ}, {d}, {ts}, {t} and
{fn} escapes and refuses whatever its capability profile marks as failing, with the driver's own
message texts where one is known (tests/fixtures/errors/providex.yaml lists the sources). Messages
the field reports don't give say "(emulator)". Schemas are the pinned MAS_EMP / MAS_PCC structures
and the rows are the test bed's synthetic ones, never real data. ProvideX has no NULL strings or
numbers (a blank is '' or 0), dates have no time part, and a blank date reads as NULL.

A test registers an install with ``install(...)`` (or ``standard_install``) and passes this module
where pyodbc would go.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from sage100_mcp.capabilities import FUNCTIONS, Capabilities
from sage100_mcp.catalog.overlay import Overlay, load_overlay
from tests.premium_fixture import columns_of, schema

DRIVER = "MAS 90 4.0 ODBC Driver"
SQL_DRIVER_NAME, SQL_DRIVER_VER, SQL_DBMS_NAME, SQL_DBMS_VER = 6, 7, 17, 18
SQL_TYPES = {"varchar": 12, "decimal": 3, "date": 91, "int": 4}
EMPTY_DATE = datetime(1753, 1, 1)  # the test bed's blank date; ProvideX reads it as NULL


class Error(Exception):
    """pyodbc.Error: args are (sqlstate, message)."""


class InterfaceError(Error):
    pass


class OperationalError(Error):
    pass


class ProgrammingError(Error):
    pass


def _error(kind: type[Error], state: str, text: str, native: int = 0, function: str = "SQLExecDirect") -> Error:
    return kind(state, f"[{state}] {text} ({native}) ({function})")


def lexical() -> Error:
    text = "[ProvideX][ODBC Driver]Expected lexical element not found: <identifier>"
    return _error(ProgrammingError, "37000", text)


def extra_token(token: str) -> Error:
    return _error(ProgrammingError, "37000", f"[ProvideX][ODBC Driver]Unexpected extra token: {token}", 1017)


def internal() -> Error:
    return _error(OperationalError, "S1000", "[ProvideX][ODBC Driver]Internal error")


def not_accessible() -> Error:
    return _error(OperationalError, "S1000", "[ProvideX][ODBC Driver][FILEIO]Table is not accessible")


def logon_failed() -> Error:
    text = "[ProvideX][ODBC Driver][PVKIO]Logon failed"
    return _error(InterfaceError, "S0000", text, 0, "SQLDriverConnect")


def not_capable(function: str = "SQLSetStmtAttr") -> Error:
    return _error(OperationalError, "HYC00", "[ProvideX][ODBC Driver]Driver not capable", 0, function)


def emulator_error(text: str, state: str = "S1000", function: str = "SQLExecDirect") -> Error:
    """A failure the field reports give no message for; the text says it comes from the emulator."""
    return _error(OperationalError, state, f"[ProvideX][ODBC Driver]{text} (emulator)", 0, function)


# --- installs ------------------------------------------------------------------------------------


@dataclass
class EmulatedCompany:
    code: str
    name: str
    source: str  # the pinned schema to use: MAS_EMP or MAS_PCC
    rows: dict[str, list[dict[str, Any]]]
    only_modules: tuple[str, ...] | None = None


@dataclass
class Install:
    """One Sage 100 install: a MAS90 directory with companies, users and a driver build."""

    directory: str
    profile: Capabilities
    users: dict[str, str]
    companies: dict[str, EmulatedCompany]
    driver_version: str = "4.40.0004"
    system_tables_visible: bool = True  # SY_Company readable from a company connection (§19.1)
    cs_available: bool = True  # the PVXIOSVR service answers
    honors_timeout: bool = True  # with query_timeout accepted, the driver also enforces it
    honors_cancel: bool = True  # SQLCancel interrupts a running statement
    accepts_connection_timeout: bool = True  # SQL_ATTR_CONNECTION_TIMEOUT, which pyodbc sets on assignment
    slow_tables: dict[str, float] = field(default_factory=dict)  # table -> seconds an execute takes
    bad_dates: set[tuple[str, str]] = field(default_factory=set)  # (company, table) holding an invalid date
    connections: int = 0  # opened so far
    open_connections: int = 0
    peak_open: int = 0
    statements: list[str] = field(default_factory=list)
    _databases: dict[str, duckdb.DuckDBPyConnection] = field(default_factory=dict)
    _created: set[tuple[str, str]] = field(default_factory=set)
    _tables: dict[tuple[str, bool, tuple[str, ...] | None], dict[str, list[tuple[str, str, bool]]]] = field(
        default_factory=dict
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def tables(self, company: str) -> dict[str, list[tuple[str, str, bool]]]:
        key = (company, self.system_tables_visible, self.companies[company].only_modules)
        cached = self._tables.get(key)
        if cached is None:
            cached = self._tables[key] = self._read_tables(company)
        return cached

    def _read_tables(self, company: str) -> dict[str, list[tuple[str, str, bool]]]:
        emulated = self.companies[company]
        found = {}
        for entry in schema(emulated.source)["tables"]:
            module = entry["name"].split("_", 1)[0]
            if emulated.only_modules and module not in emulated.only_modules:
                continue
            found[entry["name"]] = [(c.name, _providex_type(c.type), c.nullable) for c in columns_of(entry)]
        if self.system_tables_visible:
            found["SY_Company"] = [("CompanyCode", "varchar(3)", False), ("CompanyName", "varchar(50)", True)]
        return found

    def make_folders(self) -> None:
        """The MAS90 layout on disk: MAS_<code> per company, as the folder-listing fallback reads it."""
        root = Path(self.directory)
        for name in ["SY", "==", "Home", "MAS_SYSTEM", *(f"MAS_{code}" for code in self.companies)]:
            (root / name).mkdir(parents=True, exist_ok=True)

    def database(self, company: str) -> duckdb.DuckDBPyConnection:
        with self._lock:
            if company not in self._databases:
                self._databases[company] = duckdb.connect(":memory:")
            return self._databases[company]

    def ensure(self, company: str, names: set[str]) -> None:
        """Create and fill the tables a statement reads, on first use."""
        tables = {name.lower(): (name, columns) for name, columns in self.tables(company).items()}
        connection = self.database(company)
        with self._lock:
            for wanted in names:
                found = tables.get(wanted.lower())
                if found is None or (company, found[0]) in self._created:
                    continue
                name, columns = found
                kinds = [_duck_type(sql_type) for _, sql_type, _ in columns]
                body = ", ".join(f'"{c}" {kind}' for (c, _, _), kind in zip(columns, kinds, strict=True))
                connection.execute(f'CREATE TABLE "{name}" ({body})')
                rows = self._rows(company, name)
                if rows:
                    unnest = ", ".join(f"UNNEST(?::{kind}[])" for kind in kinds)
                    values = [[_cell(row.get(c), t) for row in rows] for c, t, _ in columns]
                    connection.execute(f'INSERT INTO "{name}" SELECT {unnest}', values)
                self._created.add((company, name))

    def _rows(self, company: str, table: str) -> list[dict[str, Any]]:
        if table == "SY_Company":
            return [{"CompanyCode": c.code, "CompanyName": c.name} for c in self.companies.values()]
        return self.companies[company].rows.get(table, [])


INSTALLS: dict[str, Install] = {}


def install(directory: str, profile: Capabilities, companies: dict[str, EmulatedCompany], **options: Any) -> Install:
    users = options.pop("users", {"mcp_reader": "reader-pass"})
    emulated = Install(directory, profile, users, companies, **options)
    INSTALLS[directory.lower()] = emulated
    return emulated


def standard_install(directory: str, profile: Capabilities, **options: Any) -> Install:
    """ABC (a little data), EMP (the test bed's data set) and PCC (financials only), as on the test bed."""
    from tests.premium_testbed import COMPANY_NAMES, emp_rows, pcc_rows, small_rows

    companies = {
        "ABC": EmulatedCompany("ABC", COMPANY_NAMES["ABC"], "MAS_EMP", small_rows("ABC")),
        "EMP": EmulatedCompany("EMP", COMPANY_NAMES["EMP"], "MAS_EMP", emp_rows()),
        "PCC": EmulatedCompany("PCC", COMPANY_NAMES["PCC"], "MAS_PCC", pcc_rows()),
    }
    return install(directory, profile, companies, **options)


def _providex_type(sql_type: str) -> str:
    base = sql_type.split("(")[0].lower()
    if base in ("datetime", "smalldatetime", "datetime2"):
        return "date"  # ProvideX stores dates as YYYYMMDD with no time (§5.4)
    return sql_type.lower()


def _duck_type(sql_type: str) -> str:
    base = sql_type.split("(")[0]
    if base == "decimal":
        return sql_type.upper()
    return {"date": "DATE", "int": "BIGINT"}.get(base, "VARCHAR")


def _cell(value: Any, sql_type: str) -> Any:
    """A test-bed value as ProvideX holds it: no NULL text or numbers, and a blank date is NULL."""
    if isinstance(value, datetime):
        return None if value == EMPTY_DATE else value.date()
    if value is None:
        if sql_type.startswith("decimal"):
            return Decimal(0)
        if sql_type == "int":
            return 0
        return None if sql_type == "date" else ""
    return value


# --- connection strings ---------------------------------------------------------------------------

_BASE_KEYWORDS = {
    "driver", "uid", "pwd", "company", "directory", "prefix", "viewdll", "silent", "server", "striptrailingspaces",
    "dirtyreads", "burstmode", "cachesize", "remotepvkiohost", "remotepvkioport",
}  # fmt: skip


def parse_connection_string(text: str) -> dict[str, str]:
    """``key=value;`` pairs, with ``{...}`` values that may hold ``;`` (``}}`` escapes ``}``)."""
    pairs: dict[str, str] = {}
    position = 0
    while position < len(text):
        equals = text.find("=", position)
        if equals < 0:
            break
        key = text[position:equals].strip().lower()
        position = equals + 1
        if text[position : position + 1] == "{":
            end = position + 1
            value = []
            while end < len(text):
                if text[end] == "}" and text[end + 1 : end + 2] == "}":
                    value.append("}")
                    end += 2
                    continue
                if text[end] == "}":
                    break
                value.append(text[end])
                end += 1
            pairs[key] = "".join(value)
            semicolon = text.find(";", end)
            position = semicolon + 1 if semicolon >= 0 else len(text)
        else:
            end = text.find(";", position)
            end = len(text) if end < 0 else end
            pairs[key] = text[position:end].strip()
            position = end + 1
    return pairs


def connect(connection_string: str, autocommit: bool = False, timeout: int = 0, **_: Any) -> Connection:
    keywords = parse_connection_string(connection_string)
    if keywords.get("driver", "").strip("{}") != DRIVER:
        text = "[Microsoft][ODBC Driver Manager] Data source name not found and no default driver specified"
        raise _error(InterfaceError, "IM002", text, 0, "SQLDriverConnect")
    if not autocommit:
        raise not_capable("SQLSetConnectAttr")  # the driver has no transactions to turn autocommit off for
    emulated = INSTALLS.get(keywords.get("directory", "").lower())
    if emulated is None:
        raise emulator_error("No Sage 100 data dictionary in Directory", "S1000", "SQLDriverConnect")
    if keywords.get("silent") != "1":
        raise emulator_error(
            "Silent=1 is missing, so the driver would open a logon dialog", "S1000", "SQLDriverConnect"
        )
    for key in keywords:
        if key in _BASE_KEYWORDS:
            continue
        canonical = next((k for k in emulated.profile.keywords if k.lower() == key), None)
        if canonical is None or not emulated.profile.accepts(canonical):
            raise emulator_error(f"Invalid connection attribute {key}", "S1000", "SQLDriverConnect")
    if "remotepvkiohost" in keywords and not emulated.cs_available:
        host, port = keywords["remotepvkiohost"], keywords.get("remotepvkioport", "20222")
        text = f"[ProvideX][ODBC Driver]Connection failure to host: [TCP]{host}:{port}"
        raise _error(OperationalError, "08001", text, 0, "SQLDriverConnect")
    if emulated.users.get(keywords.get("uid", "")) != keywords.get("pwd"):
        raise logon_failed()
    company = keywords.get("company", "").upper()
    if company not in emulated.companies:
        text = f"[ProvideX][ODBC Driver]Company {company} is not on file"
        raise _error(InterfaceError, "S1000", text, 0, "SQLDriverConnect")
    with emulated._lock:
        emulated.connections += 1
        emulated.open_connections += 1
        emulated.peak_open = max(emulated.peak_open, emulated.open_connections)
    return Connection(emulated, company, keywords)


class Connection:
    def __init__(self, emulated: Install, company: str, keywords: dict[str, str]) -> None:
        self.install = emulated
        self.company = company
        self.keywords = keywords
        self._timeout = 0
        self.autocommit = True
        self.closed = False

    @property
    def timeout(self) -> int:
        return self._timeout

    @timeout.setter
    def timeout(self, seconds: int) -> None:
        """As in pyodbc: SQL_ATTR_CONNECTION_TIMEOUT at once, even for 0, and the query timeout per new cursor."""
        if not self.install.accepts_connection_timeout:
            raise not_capable("SQLSetConnectAttr")
        self._timeout = int(seconds)

    def cursor(self) -> Cursor:
        if self.closed:
            raise _error(ProgrammingError, "08003", "Attempt to use a closed connection.")
        if self._timeout and not self.install.profile.supports("query_timeout"):
            raise not_capable("SQLSetStmtAttr")
        return Cursor(self)

    def getinfo(self, code: int) -> str:
        return {
            SQL_DRIVER_NAME: "PVXODBC.DLL",
            SQL_DRIVER_VER: self.install.driver_version,
            SQL_DBMS_NAME: "ProvideX",
            SQL_DBMS_VER: "9.20",
        }.get(code, "")

    def close(self) -> None:
        if not self.closed:
            with self.install._lock:
                self.install.open_connections -= 1
        self.closed = True


# --- statements -------------------------------------------------------------------------------------

_ESCAPE = re.compile(r"\{\s*(oj|ij|fn|d|ts|t)\b", re.IGNORECASE)
_FN = "PXFN_"  # how an accepted scalar function is carried through the parse
_LITERALS = {"PXDATE": "DATE", "PXTS": "TIMESTAMP", "PXTIME": "TIME"}  # {d}, {ts}, {t}
_NOT_FUNCTIONS = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "EXISTS", "ON", "AS", "JOIN", "BY", "HAVING", "UNION",
    "ALL", "ANY", "SOME", "CASE", "WHEN", "THEN", "ELSE", "BETWEEN", "LIKE", "IS", "DISTINCT", "VALUES", "CAST",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "VARCHAR", "CHAR", "DECIMAL", "NUMERIC", "INTEGER", "INT", "DATE",
    "DOUBLE", "FLOAT",
}  # fmt: skip
_DUCK_FUNCTIONS = {
    "UCASE": "UPPER", "LCASE": "LOWER", "LTRIM": "LTRIM", "RTRIM": "RTRIM", "SUBSTRING": "SUBSTRING",
    "CONCAT": "CONCAT", "REPLACE": "REPLACE", "ABS": "ABS", "ROUND": "ROUND", "FLOOR": "FLOOR",
    "CEILING": "CEIL", "MOD": "MOD", "TRUNCATE": "TRUNC", "YEAR": "YEAR", "MONTH": "MONTH", "QUARTER": "QUARTER",
    "WEEK": "WEEK", "DAYOFMONTH": "DAYOFMONTH", "LEFT": "LEFT", "RIGHT": "RIGHT", "IFNULL": "COALESCE",
}  # fmt: skip
_UNITS = {"SQL_TSI_DAY": "day", "SQL_TSI_WEEK": "week", "SQL_TSI_MONTH": "month", "SQL_TSI_QUARTER": "quarter",
          "SQL_TSI_YEAR": "year"}  # fmt: skip


def _mask_strings(sql: str) -> str:
    """The SQL with string literal contents blanked, so scans for syntax ignore what is inside quotes."""
    return re.sub(r"'(?:[^']|'')*'", lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", sql)


def _matching(masked: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(start, len(masked)):
        if masked[index] == opening:
            depth += 1
        elif masked[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    raise lexical()


def _outside_escapes(sql: str) -> str:
    """The SQL with every {...} escape blanked out."""
    result, depth = [], 0
    for ch in _mask_strings(sql):
        depth += ch == "{"
        result.append(" " if depth else ch)
        depth -= ch == "}"
    return "".join(result)


def _strip_comments(sql: str) -> str:
    masked = _mask_strings(sql)
    for found in reversed(list(re.finditer(r"/\*.*?\*/|--[^\n]*", masked, re.S))):
        sql = sql[: found.start()] + " " + sql[found.end() :]
    return sql


def _depth(node: exp.Expr) -> int:
    depth = 0
    while node.parent is not None:
        node, depth = node.parent, depth + 1
    return depth


class _Translator:
    """ProvideX SQL to DuckDB SQL, refusing what the profile lacks."""

    def __init__(self, emulated: Install, company: str) -> None:
        self.install = emulated
        self.caps = emulated.profile
        self.company = company

    def require(self, feature: str, failure: Error) -> None:
        if not self.caps.supports(feature):
            raise failure

    def translate(self, sql: str) -> tuple[str, set[str]]:
        masked = _mask_strings(sql)
        if ";" in masked:
            raise extra_token(";")
        if "[" in masked:
            raise lexical()
        if "--" in masked or "/*" in masked:
            self.require("comments", extra_token("/*" if "/*" in masked else "--"))
            sql = _strip_comments(sql)
        outside = _outside_escapes(sql)
        join = re.search(r"\b(?:(INNER|LEFT|RIGHT|FULL|CROSS)\s+)?(?:OUTER\s+)?JOIN\b", outside, re.IGNORECASE)
        if join:
            raise extra_token((join.group(1) or "JOIN").upper())
        top = re.match(r"\s*SELECT\s+TOP\s+(\S+)\s", outside, re.IGNORECASE)
        rows = None
        if top:
            self.require("top", extra_token(top.group(1)))
            if not top.group(1).isdigit():
                raise lexical()
            rows = int(top.group(1))
            sql = re.sub(r"^(\s*SELECT\s+)TOP\s+\d+\s", r"\1", sql, count=1, flags=re.IGNORECASE)
        plain = self._bare_functions(self._unescape(sql))
        try:
            tree = sqlglot.parse_one(plain, read="")
        except sqlglot.errors.ParseError:
            raise lexical() from None
        self._checks(tree)
        self._to_duckdb(tree)
        if rows is not None and isinstance(tree, exp.Select):
            tree = tree.limit(rows)
        return tree.sql(dialect="duckdb"), {t.name for t in tree.find_all(exp.Table)}

    # --- escapes -----------------------------------------------------------------------------------

    def _unescape(self, sql: str) -> str:
        """Replace escapes innermost first: {fn F(x)} -> PXFN_F(x), {d 'x'} -> PXDATE('x'), {oj ...} -> ..."""
        while True:
            masked = _mask_strings(sql)
            if "{" not in masked:
                if "}" in masked:
                    raise lexical()
                return sql
            start = end = -1
            for opening in (m.start() for m in re.finditer(r"\{", masked)):
                close = _matching(masked, opening, "{", "}")
                if "{" not in masked[opening + 1 : close]:
                    start, end = opening, close
                    break
            kind = _ESCAPE.match(masked, start)
            if kind is None:
                raise lexical()
            body = sql[kind.end() : end].strip()
            sql = sql[:start] + " " + self._escape(kind.group(1).lower(), body) + " " + sql[end + 1 :]

    def _escape(self, kind: str, body: str) -> str:
        if kind in ("d", "ts", "t"):
            if not re.fullmatch(r"'[^']*'", body):
                raise lexical()
            if kind == "d":
                self.require("date_literal", lexical())
                return f"PXDATE({body})"
            return f"{'PXTS' if kind == 'ts' else 'PXTIME'}({body})"
        if kind == "fn":
            found = re.match(r"([A-Za-z_]+)\s*\(", body)
            if found is None or not body.endswith(")"):
                raise lexical()
            name = found.group(1).upper()
            support = self.caps.functions.get(name)
            if name not in FUNCTIONS or support is None or not support.escaped:
                raise lexical()
            return _FN + name + body[found.end(1) :]
        upper = _mask_strings(body).upper()
        lefts = len(re.findall(r"\bLEFT\s+OUTER\s+JOIN\b", upper))
        inners = len(re.findall(r"\bINNER\s+JOIN\b", upper))
        if kind == "oj":
            if re.search(r"\b(RIGHT|FULL)\s+(OUTER\s+)?JOIN\b", upper):
                raise lexical()
            if inners:
                self.require("oj_inner", extra_token("INNER"))
            if lefts:
                self.require("oj_left" if lefts == 1 else "oj_left_chain", lexical())
            return body
        self.require("ij" if inners <= 1 else "ij_nested", lexical())
        masked = _mask_strings(body)
        if masked.startswith("("):  # {IJ (a INNER JOIN b ON ...) INNER JOIN c ON ...}
            close = _matching(masked, 0, "(", ")")
            body = body[1:close] + " " + body[close + 1 :]
        return body

    def _bare_functions(self, sql: str) -> str:
        """Function calls written without {fn}: allowed only where the profile says the bare form works."""
        masked = _mask_strings(sql)
        pieces, last = [], 0
        for found in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", masked):
            name = found.group(1).upper()
            if name.startswith(_FN) or name in _LITERALS or name in _NOT_FUNCTIONS:
                continue
            support = self.caps.functions.get(name)
            if name not in FUNCTIONS or support is None or not support.bare:
                raise lexical()
            pieces.append(sql[last : found.start(1)] + _FN)
            last = found.start(1)
        return "".join(pieces) + sql[last:]

    # --- feature checks ------------------------------------------------------------------------------

    def _date_columns(self, tree: exp.Expr) -> set[str]:
        tables = {t.name.lower() for t in tree.find_all(exp.Table)}
        found = set()
        for name, columns in self.install.tables(self.company).items():
            if name.lower() in tables:
                found |= {c.lower() for c, t, _ in columns if t == "date"}
        return found

    def _checks(self, tree: exp.Expr) -> None:
        if isinstance(tree, exp.Union):
            self.require("union" if tree.args.get("distinct") else "union_all", extra_token("UNION"))
        elif not isinstance(tree, exp.Select):
            raise lexical()
        for subquery in tree.find_all(exp.Subquery):
            if isinstance(subquery.parent, exp.From | exp.Join):
                self.require("derived_table", lexical())
            elif not isinstance(subquery.parent, exp.In | exp.Exists):
                raise lexical()  # a subquery used as a value
        if tree.find(exp.Exists) is not None:
            self.require("exists", lexical())
        for node in tree.find_all(exp.In):
            self.require("subquery_in" if node.args.get("query") is not None else "in_list", lexical())
        if tree.find(exp.Any, exp.All) is not None:
            raise lexical()
        for select in tree.find_all(exp.Select):
            if select.args.get("distinct"):
                self.require("distinct", extra_token("DISTINCT"))
            if select.args.get("having"):
                self.require("having", extra_token("HAVING"))
            group = select.args.get("group")
            if group is not None:
                self.require("group_by", extra_token("GROUP"))
                if any(not isinstance(g, exp.Column) for g in group.expressions):
                    self.require("group_by_expression", lexical())
            for item in select.expressions:
                if isinstance(item, exp.Alias):
                    self.require("column_alias", extra_token(item.alias))
            joins = select.args.get("joins") or []
            if any(not j.args.get("on") and not j.args.get("kind") and not j.args.get("side") for j in joins):
                self.require("comma_join", extra_token(","))
            for table in select.find_all(exp.Table):
                if table.alias and table.alias.lower() != table.name.lower():
                    self.require("table_alias", extra_token(table.alias))
        limit, offset, order = tree.args.get("limit"), tree.args.get("offset"), tree.args.get("order")
        if limit is not None:
            self.require("limit", extra_token(limit.expression.sql()))
        if offset is not None:
            self.require("limit_offset", extra_token("OFFSET"))
        if order is not None and (limit is not None or offset is not None):
            # The grammar puts LIMIT before ORDER BY; sage100-mcp never sends both, so neither does a test.
            raise emulator_error("ORDER BY together with LIMIT or TOP is not emulated", "37000")
        for ordered in order.expressions if order is not None else []:
            if isinstance(ordered.this, exp.Literal):
                self.require("order_by_position", extra_token("ORDER"))
            else:
                self.require("order_by_column", extra_token("ORDER"))
            if ordered.args.get("desc"):
                self.require("order_by_desc", extra_token("DESC"))
        checks = (
            (exp.Or, "or", extra_token("OR")),
            (exp.And, "compound_and", extra_token("AND")),
            (exp.Case, "case", lexical()),
            (exp.Cast, "cast", lexical()),
            (exp.Between, "between", lexical()),
            (exp.Like, "like", lexical()),
            (exp.Is, "is_null", lexical()),
            (exp.Placeholder, "parameters", lexical()),
            (exp.Neg, "negative_literal", extra_token("-")),
        )
        for kind, feature, failure in checks:
            if tree.find(kind) is not None:
                self.require(feature, failure)
        if tree.find(exp.Add, exp.Sub, exp.Mul, exp.Div) is not None:
            self.require("arithmetic", lexical())
        if any(isinstance(c.this, exp.Distinct) for c in tree.find_all(exp.Count)):
            self.require("count_distinct", lexical())
        dates = self._date_columns(tree)
        for test in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In):
            values = [test.args.get(k) for k in ("expression", "low", "high")] + list(test.expressions)
            if (
                isinstance(test.this, exp.Column)
                and test.this.name.lower() in dates
                and any(isinstance(v, exp.Literal) and v.is_string for v in values)
            ):
                self.require("iso_date_string", lexical())

    # --- DuckDB ------------------------------------------------------------------------------------------

    def _to_duckdb(self, tree: exp.Expr) -> None:
        calls = [
            n for n in tree.find_all(exp.Anonymous) if n.name.upper().startswith(_FN) or n.name.upper() in _LITERALS
        ]
        for node in sorted(calls, key=_depth, reverse=True):  # innermost first, so arguments are already DuckDB
            name = node.name.upper()
            args = [a.sql(dialect="duckdb") for a in node.expressions]
            text = f"{_LITERALS[name]} {args[0]}" if name in _LITERALS else _duck_function(name[len(_FN) :], args)
            node.replace(sqlglot.parse_one(text, read="duckdb"))
        if not self.caps.supports("aggregate_exact"):  # such a driver sums in floating point
            for aggregate in list(tree.find_all(exp.Sum, exp.Avg)):
                aggregate.replace(exp.cast(aggregate.copy(), exp.DataType.Type.DOUBLE))


def _duck_function(name: str, args: list[str]) -> str:
    if name == "LENGTH":
        return f"LENGTH(RTRIM({args[0]}))"  # ODBC LENGTH ignores trailing blanks
    if name == "LOCATE":
        if len(args) < 3:
            return f"STRPOS({args[1]}, {args[0]})"
        found = f"STRPOS(SUBSTRING({args[1]}, {args[2]}), {args[0]})"
        return f"(CASE WHEN {found} = 0 THEN 0 ELSE {found} + {args[2]} - 1 END)"
    if name == "CURDATE":
        return "CURRENT_DATE"
    if name == "NOW":
        return "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"
    if name == "DAYOFWEEK":
        return f"(DAYOFWEEK({args[0]}) + 1)"  # ODBC counts Sunday as 1
    if name in ("TIMESTAMPADD", "TIMESTAMPDIFF"):
        unit = _UNITS.get(args[0].upper())
        if unit is None:
            raise lexical()
        if name == "TIMESTAMPDIFF":
            return f"DATE_DIFF('{unit}', CAST({args[1]} AS DATE), CAST({args[2]} AS DATE))"
        months = {"month": "1", "quarter": "3", "year": "12"}.get(unit)
        step = f"TO_MONTHS({months} * CAST({args[1]} AS INTEGER))" if months else f"TO_DAYS(CAST({args[1]} AS INTEGER))"
        if unit == "week":
            step = f"TO_DAYS(7 * CAST({args[1]} AS INTEGER))"
        return f"CAST(({args[2]} + {step}) AS DATE)"
    return f"{_DUCK_FUNCTIONS[name]}({', '.join(args)})"


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.description: list[tuple[Any, ...]] | None = None
        self._rows: Iterator[Any] = iter(())
        self._cancelled = threading.Event()

    @property
    def install(self) -> Install:
        return self.connection.install

    def execute(self, sql: str, *params: Any) -> Cursor:
        if len(params) == 1 and isinstance(params[0], list | tuple):
            params = tuple(params[0])
        self._cancelled.clear()
        company = self.connection.company
        self.install.statements.append(sql)
        markers = _mask_strings(sql).count("?")
        if markers != len(params):
            text = f"The SQL contains {markers} parameter markers, but {len(params)} parameters were supplied"
            raise _error(ProgrammingError, "07002", text, 0, "SQLBindParameter")
        duck_sql, tables = _Translator(self.install, company).translate(sql)
        known = {name.lower() for name in self.install.tables(company)}
        if any(name.lower() not in known for name in tables):
            raise not_accessible()
        if (
            any((company, n) in self.install.bad_dates for n in tables)
            and self.connection.keywords.get("enforcenulldate") != "1"
        ):
            raise internal()
        self._wait(tables)
        self.install.ensure(company, tables)
        cursor = self.install.database(company).cursor()
        try:
            result = cursor.execute(duck_sql, list(params))
            self.description = [_describe(d) for d in result.description or []]
            self._rows = iter([tuple(row) for row in result.fetchall()])
        except duckdb.Error as exc:  # a translation bug, never a driver behaviour: fail loudly
            raise _error(Error, "HY000", f"emulator: DuckDB rejected {duck_sql!r}: {exc}") from None
        finally:
            cursor.close()
        return self

    def _wait(self, tables: set[str]) -> None:
        delay = max((self.install.slow_tables.get(name, 0.0) for name in tables), default=0.0)
        if not delay:
            return
        started = time.monotonic()
        timeout = self.connection.timeout if self.install.honors_timeout else 0
        while time.monotonic() - started < delay:
            if self._cancelled.is_set() and self.install.honors_cancel:
                raise _error(OperationalError, "HY008", "[ProvideX][ODBC Driver]Operation canceled")
            if timeout and time.monotonic() - started >= timeout:
                raise _error(OperationalError, "HYT00", "[ProvideX][ODBC Driver]Timeout expired.")
            time.sleep(0.01)

    def fetchone(self) -> Any:
        return next(self._rows, None)

    def fetchmany(self, size: int = 1) -> list[Any]:
        rows = []
        for _ in range(size):
            row = next(self._rows, None)
            if row is None:
                break
            rows.append(row)
        return rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        self._rows = iter(())

    # --- catalog functions (pyodbc's names and keyword arguments) ---------------------------------------

    def tables(
        self,
        table: str | None = None,
        catalog: str | None = None,
        schema: str | None = None,
        tableType: str | None = None,
    ) -> Cursor:
        pattern = _like(table)
        names = sorted(n for n in self.install.tables(self.connection.company) if pattern.match(n))
        self._rows = iter([_Row(table_cat=None, table_schem=None, table_name=n, table_type="TABLE") for n in names])
        self.description = _catalog_description(_TABLE_FIELDS)
        return self

    def columns(
        self, table: str | None = None, catalog: str | None = None, schema: str | None = None, column: str | None = None
    ) -> Cursor:
        rows = []
        pattern = _like(table)
        for name, columns in self.install.tables(self.connection.company).items():
            if not pattern.match(name):
                continue
            for position, (column_name, sql_type, nullable) in enumerate(columns, start=1):
                base = sql_type.split("(")[0]
                size, digits = _size(sql_type)
                rows.append(
                    _Row(
                        table_cat=None,
                        table_schem=None,
                        table_name=name,
                        column_name=column_name,
                        data_type=SQL_TYPES.get(base, 12),
                        type_name=base.upper(),
                        column_size=size,
                        buffer_length=size,
                        decimal_digits=digits,
                        num_prec_radix=10,
                        nullable=1 if nullable else 0,
                        remarks="",
                        ordinal_position=position,
                    )
                )
        self._rows = iter(rows)
        self.description = _catalog_description(_COLUMN_FIELDS)
        return self

    def primaryKeys(self, table: str, catalog: str | None = None, schema: str | None = None) -> Cursor:
        if not self.install.profile.supports("primary_keys"):
            raise not_capable("SQLPrimaryKeys")
        note = _overlay().table(table)
        found = next((columns for name, columns in self.install.tables(self.connection.company).items()
                      if name.lower() == table.lower()), [])  # fmt: skip
        keys = list(note.key) if note and note.key else [c for c, _, _ in found[:1]]
        rows = [_Row(table_name=table, column_name=k, key_seq=i, pk_name="KPRIMARY") for i, k in enumerate(keys, 1)]
        self._rows = iter(rows)
        self.description = _catalog_description(_KEY_FIELDS)
        return self


@cache
def _overlay() -> Overlay:
    return load_overlay()


# pyodbc describes a catalog function's result set like a query's, with lower-case names. These are the
# fields the rows above carry, in their order.
_TABLE_FIELDS = ("table_cat", "table_schem", "table_name", "table_type")
_COLUMN_FIELDS = (
    "table_cat", "table_schem", "table_name", "column_name", "data_type", "type_name", "column_size", "buffer_length",
    "decimal_digits", "num_prec_radix", "nullable", "remarks", "ordinal_position",
)  # fmt: skip
_KEY_FIELDS = ("table_name", "column_name", "key_seq", "pk_name")
_NUMBER_FIELDS = {"data_type", "column_size", "buffer_length", "decimal_digits", "num_prec_radix", "nullable",
                  "ordinal_position", "key_seq"}  # fmt: skip


def _catalog_description(fields: Sequence[str]) -> list[tuple[Any, ...]]:
    return [(name, int, None, 10, 10, 0, True) if name in _NUMBER_FIELDS else (name, str, None, 128, 128, 0, True)
            for name in fields]  # fmt: skip


class _Row:
    """A pyodbc-like row: indexable, iterable, and its fields are attributes."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)
        self._values = tuple(fields.values())

    def __getitem__(self, index: int) -> Any:
        return self._values[index]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _like(pattern: str | None) -> re.Pattern[str]:
    """An SQLTables/SQLColumns search pattern: % for any run, _ for any one character."""
    if not pattern:
        return re.compile(".*")
    regex = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.compile(f"^{regex}$", re.IGNORECASE)


def _size(sql_type: str) -> tuple[int, int]:
    found = re.search(r"\((\d+)(?:,\s*(\d+))?\)", sql_type)
    if not found:
        return (10, 0) if sql_type.startswith("date") else (0, 0)
    return int(found.group(1)), int(found.group(2) or 0)


def _describe(column: Sequence[Any]) -> tuple[Any, ...]:
    """A DuckDB description entry as pyodbc reports it: (name, Python type, None, size, precision, scale, null)."""
    name, type_name = column[0], str(column[1]).upper()
    if type_name.startswith("DECIMAL"):
        precision, scale = _size(type_name.lower())
        return (name, Decimal, None, precision, precision, scale, True)
    if type_name == "DATE":
        return (name, date, None, 10, 10, 0, True)
    if type_name in ("BIGINT", "INTEGER", "HUGEINT", "SMALLINT", "TINYINT"):
        return (name, int, None, 10, 10, 0, True)
    if type_name in ("DOUBLE", "FLOAT"):
        return (name, float, None, 15, 15, 0, True)
    if type_name.startswith("TIMESTAMP"):
        return (name, datetime, None, 19, 19, 0, True)
    return (name, str, None, 255, 255, 0, True)
