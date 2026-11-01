"""The capability probe (SPEC.md §5.3): which SQL one ProvideX driver build accepts.

Each check is a one-row read of a small table the company has data in. A check passes when the
driver runs it and the answer is right: LIMIT 1 must return one row, an AND must exclude what it
should, {fn UCASE('abc')} must return ABC. A driver that accepts syntax but ignores it would
otherwise send wrong results down the native path. The probe needs one row to compare against;
without one it gives up, and the baseline profile stays until the next connect.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sage100_mcp.capabilities import FEATURES, FUNCTIONS, KEYWORDS, Capabilities, FunctionSupport
from sage100_mcp.driver_errors import redact

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 5.0
_NONE = "zz~none~"  # a value no key or description holds
_VENDOR = re.compile(r"^(?:\[[^\]]*\]\s*)+")
_TRAILER = re.compile(r"\s*\(-?\d+\)\s*(?:\(SQL[A-Za-z]+\))?\s*$")
_DECIMAL_TYPES = ("DEC", "NUMERIC")

Rows = list[tuple[Any, ...]]
Expect = Callable[[Rows, Sequence[Any]], str | None]  # rows, description -> what was wrong, or None


class ProbeFailure(Exception):
    """The driver refused a check; the message is its own text, trimmed."""


class NoProbeTable(Exception):
    """None of the candidate tables has a row to probe with."""


class Session(Protocol):
    """One open driver connection, as the executor gives it to the probe."""

    def rows(self, sql: str, params: Sequence[Any] = (), *, limit: int = 1) -> tuple[Rows, Sequence[Any]]: ...

    def columns(self, table: str) -> list[tuple[str, str]]: ...  # (name, TYPE_NAME)

    def primary_keys(self, table: str) -> list[str]: ...

    def connects_with(self, keyword: str, value: str, sql: str) -> None: ...

    def timeout_accepted(self, seconds: float) -> None: ...


def probe_table_candidates() -> list[tuple[str, str]]:
    """Small tables first (§5.3); then a customer row, then the one-row module options tables."""
    options = [(f"{module}_Options", "ModuleCode") for module in ("CI", "AR", "AP", "GL", "SO", "IM", "PO")]
    return [
        ("AP_Division", "APDivisionNo"),
        ("AR_Division", "ARDivisionNo"),
        ("GL_Account", "AccountKey"),
        ("CI_Item", "ItemCode"),
        ("AR_Customer", "CustomerNo"),
        *options,
    ]


def trim(message: str) -> str:
    """A driver message without its SQLSTATE, vendor brackets and native-error trailer, literals redacted."""
    text = _TRAILER.sub("", _VENDOR.sub("", message.strip()))
    return redact(text)[:300]


@dataclass(frozen=True)
class Target:
    table: str
    key: str
    text: str  # a text column; the key itself when the table has no other
    date: str | None
    k0: str  # the key of the first row
    c0: str  # the text column of that row


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _same(value: Any, expected: Any) -> bool:
    if expected is None:
        return True
    if isinstance(expected, str):
        return isinstance(value, str) and value.rstrip() == expected
    if isinstance(expected, date):
        found = value.date() if isinstance(value, datetime) else value
        return bool(found == expected)
    try:
        return Decimal(str(value)) == Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False


def _shown(rows: Rows) -> str:
    return repr(rows[0][0]) if rows else "no rows"


def _equals(expected: Any) -> Expect:
    def check(rows: Rows, description: Sequence[Any]) -> str | None:
        if rows and _same(rows[0][0], expected):
            return None
        return f"returned {_shown(rows)}, expected {expected!r}"

    return check


def _has(value: str) -> Expect:
    def check(rows: Rows, description: Sequence[Any]) -> str | None:
        return (
            None
            if any(str(row[0]).rstrip() == value for row in rows)
            else f"did not return the row it should ({value!r})"
        )

    return check


def _empty(rows: Rows, description: Sequence[Any]) -> str | None:
    return None if not rows else f"returned {len(rows)} row(s) where no row matches"


def _at_most_one(rows: Rows, description: Sequence[Any]) -> str | None:
    return None if len(rows) <= 1 else f"returned {len(rows)} rows for a one-row limit"


def _exact(rows: Rows, description: Sequence[Any]) -> str | None:
    kind = description[0][1] if description else None
    return None if kind is Decimal else f"returned the sum as {getattr(kind, '__name__', kind)}, not an exact decimal"


# (expression with {date} / {date2} / {key} placeholders, expected value or None for "any")
FUNCTION_CHECKS: dict[str, tuple[str, Any]] = {
    "UCASE": ("UCASE('abc')", "ABC"),
    "LCASE": ("LCASE('ABC')", "abc"),
    "LEFT": ("LEFT('abcd', 2)", "ab"),
    "RIGHT": ("RIGHT('abcd', 2)", "cd"),
    "LENGTH": ("LENGTH('abc')", 3),
    "LOCATE": ("LOCATE('c', 'abcd')", 3),
    "LTRIM": ("LTRIM('  ab')", "ab"),
    "RTRIM": ("RTRIM('ab  ')", "ab"),
    "SUBSTRING": ("SUBSTRING('abcd', 2, 2)", "bc"),
    "CONCAT": ("CONCAT('ab', 'cd')", "abcd"),
    "REPLACE": ("REPLACE('abcd', 'b', 'x')", "axcd"),
    "ABS": ("ABS(2)", 2),
    "ROUND": ("ROUND(2.567, 1)", Decimal("2.6")),
    "FLOOR": ("FLOOR(2.5)", 2),
    "CEILING": ("CEILING(2.5)", 3),
    "MOD": ("MOD(7, 3)", 1),
    "TRUNCATE": ("TRUNCATE(2.567, 1)", Decimal("2.5")),
    "YEAR": ("YEAR({date})", 2024),
    "MONTH": ("MONTH({date})", 3),
    "QUARTER": ("QUARTER({date})", 1),
    "WEEK": ("WEEK({date})", None),  # week numbering differs by convention; any answer passes
    "DAYOFWEEK": ("DAYOFWEEK({date})", 6),  # 2024-03-15 is a Friday; ODBC counts Sunday as 1
    "DAYOFMONTH": ("DAYOFMONTH({date})", 15),
    "CURDATE": ("CURDATE()", None),
    "NOW": ("NOW()", None),
    "TIMESTAMPADD": ("TIMESTAMPADD(SQL_TSI_DAY, 1, {date})", date(2024, 3, 16)),
    "TIMESTAMPDIFF": ("TIMESTAMPDIFF(SQL_TSI_DAY, {date}, {date2})", 1),
    "IFNULL": ("IFNULL({key}, 'x')", None),
}
assert set(FUNCTION_CHECKS) == set(FUNCTIONS)

# Every feature the probe settles; a unit test keeps this equal to capabilities.FEATURES.
CHECKED_FEATURES = (
    "limit", "limit_offset", "top", "order_by_column", "order_by_position", "order_by_desc", "or", "compound_and",
    "union", "union_all", "comments", "parameters", "distinct", "group_by", "group_by_expression", "having",
    "in_list", "between", "like", "is_null", "arithmetic", "column_alias", "table_alias", "comma_join", "oj_left",
    "oj_left_chain", "oj_inner", "ij", "ij_nested", "case", "cast", "count_distinct", "subquery_in", "exists",
    "derived_table", "date_literal", "iso_date_string", "negative_literal", "aggregate_exact", "primary_keys",
    "query_timeout",
)  # fmt: skip


def find_target(session: Session) -> Target:
    for table, key in probe_table_candidates():
        try:
            columns = session.columns(table)
        except ProbeFailure:
            continue
        names = {name.lower(): name for name, _ in columns}
        if key.lower() not in names:
            continue
        key = names[key.lower()]
        text = next((n for n, t in columns if "CHAR" in t.upper() and n.lower() != key.lower()), key)
        found_date = next((n for n, t in columns if t.upper().startswith("DATE") and "TIME" not in t.upper()), None)
        try:
            rows, _ = session.rows(f"SELECT {key}, {text} FROM {table}")
        except ProbeFailure:
            continue
        if rows:
            first = rows[0]
            return Target(table, key, text, found_date, str(first[0]).rstrip(), str(first[1] or "").rstrip())
    raise NoProbeTable("none of the probe tables has a row")


class _Probe:
    def __init__(self, session: Session, target: Target) -> None:
        self.session = session
        self.target = target
        self.features: dict[str, bool] = {}
        self.functions: dict[str, FunctionSupport] = {}
        self.keywords: dict[str, bool] = {}
        self.notes: dict[str, str] = {}

    def attempt(
        self, name: str, sql: str, params: Sequence[Any] = (), expect: Expect | None = None, limit: int = 1
    ) -> bool:
        try:
            rows, description = self.session.rows(sql, params, limit=limit)
        except ProbeFailure as failure:
            self.notes[name] = str(failure)
            return False
        problem = expect(rows, description) if expect is not None else None
        if problem is not None:
            self.notes[name] = problem
            return False
        return True

    def feature(self, name: str, *statements: tuple[str, Expect | None] | str, limit: int = 1) -> bool:
        """``name`` passes when every statement does."""
        passed = True
        for statement in statements:
            sql, expect = (statement, None) if isinstance(statement, str) else statement
            if not self.attempt(name, sql, expect=expect, limit=limit):
                passed = False
                break
        self.features[name] = passed
        return passed

    def run(self) -> None:
        t = self.target
        table, key, text = t.table, t.key, t.text
        k0, c0 = _literal(t.k0), _literal(t.c0)
        one = f"SELECT {key} FROM {table}"
        self.feature("limit", (f"{one} LIMIT 1", _at_most_one), limit=2)
        self.feature("limit_offset", (f"{one} LIMIT 1 OFFSET 0", _at_most_one), limit=2)
        self.feature("top", (f"SELECT TOP 1 {key} FROM {table}", _at_most_one), limit=2)
        by_column = self.feature("order_by_column", f"{one} ORDER BY {key}")
        by_position = self.feature("order_by_position", f"{one} ORDER BY 1")
        if by_column or by_position:
            self.feature("order_by_desc", f"{one} ORDER BY {key if by_column else 1} DESC")
        else:
            self.features["order_by_desc"] = False
            self.notes["order_by_desc"] = "ORDER BY itself failed"
        self.feature("or", (f"{one} WHERE {key} = {k0} OR {text} = '{_NONE}'", _has(t.k0)), limit=50)
        self.feature(
            "compound_and",
            (f"{one} WHERE {key} = {k0} AND {text} = {c0}", _has(t.k0)),
            (f"{one} WHERE {key} = {k0} AND {text} = '{_NONE}'", _empty),
            limit=50,
        )
        self.feature("union", f"{one} UNION {one}")
        self.feature("union_all", f"{one} UNION ALL {one}")
        self.feature("comments", f"SELECT {key} /* sage100-mcp probe */ FROM {table}")
        self.features["parameters"] = self.attempt("parameters", f"{one} WHERE {key} = ?", (t.k0,), _has(t.k0), 50)
        self.feature("distinct", f"SELECT DISTINCT {key} FROM {table}")
        self.feature("group_by", f"SELECT {key}, COUNT(*) FROM {table} GROUP BY {key}")
        self.feature("having", f"SELECT {key}, COUNT(*) FROM {table} GROUP BY {key} HAVING COUNT(*) > 0")
        self.feature("in_list", (f"{one} WHERE {key} IN ({k0}, '{_NONE}')", _has(t.k0)), limit=50)
        self.feature("between", (f"{one} WHERE {key} BETWEEN {k0} AND {k0}", _has(t.k0)), limit=50)
        self.feature("like", (f"{one} WHERE {key} LIKE {_literal(t.k0 + '%')}", _has(t.k0)), limit=50)
        self.feature("is_null", (f"{one} WHERE {key} IS NOT NULL", _has(t.k0)), limit=50)
        self.feature("arithmetic", (f"SELECT (1 + 2) * 3 FROM {table}", _equals(9)))
        self.feature("column_alias", f"SELECT {key} AS probe_value FROM {table}")
        self.feature("table_alias", f"SELECT t.{key} FROM {table} t")
        self.feature("comma_join", f"SELECT a.{key} FROM {table} a, {table} b WHERE a.{key} = b.{key}")
        pair = f"{table} a LEFT OUTER JOIN {table} b ON (a.{key} = b.{key})"
        self.feature("oj_left", f"SELECT a.{key} FROM {{oj {pair}}}")
        self.feature(
            "oj_left_chain", f"SELECT a.{key} FROM {{oj {pair} LEFT OUTER JOIN {table} c ON (a.{key} = c.{key})}}"
        )
        self.feature("oj_inner", f"SELECT a.{key} FROM {{oj {table} a INNER JOIN {table} b ON (a.{key} = b.{key})}}")
        inner = f"{table} a INNER JOIN {table} b ON a.{key} = b.{key}"
        self.feature("ij", f"SELECT a.{key} FROM {{IJ {inner}}}")
        self.feature("ij_nested", f"SELECT a.{key} FROM {{IJ ({inner}) INNER JOIN {table} c ON a.{key} = c.{key}}}")
        self.feature(
            "case", (f"SELECT CASE WHEN {key} = {k0} THEN 1 ELSE 0 END FROM {table} WHERE {key} = {k0}", _equals(1))
        )
        self.feature("cast", f"SELECT CAST({key} AS VARCHAR(30)) FROM {table}")
        self.feature("count_distinct", f"SELECT COUNT(DISTINCT {key}) FROM {table}")
        self.feature("subquery_in", f"{one} WHERE {key} IN (SELECT {key} FROM {table})")
        self.feature("exists", f"{one} WHERE EXISTS (SELECT {key} FROM {table})")
        self.feature("derived_table", f"SELECT d.{key} FROM (SELECT {key} FROM {table}) d")
        if t.date is not None:
            self.feature("date_literal", f"{one} WHERE {t.date} >= {{d '1900-01-01'}}")
            self.feature("iso_date_string", f"{one} WHERE {t.date} >= '1900-01-01'")
        else:
            self.feature("date_literal", f"SELECT {{d '2024-01-01'}} FROM {table}")
            self.features["iso_date_string"] = False
            self.notes["iso_date_string"] = f"{table} has no date column to compare"
        self.feature("negative_literal", (f"SELECT -1 FROM {table}", _equals(-1)))
        self.functions = self.run_functions()
        if self.functions["UCASE"].escaped:
            expression = "{fn UCASE(" + key + ")}"
        elif self.functions["UCASE"].bare:
            expression = f"UCASE({key})"
        else:
            expression = "(1 + 1)"
        self.feature("group_by_expression", f"SELECT {expression}, COUNT(*) FROM {table} GROUP BY {expression}")
        self.features["aggregate_exact"] = self.aggregate_exact()
        self.features["primary_keys"] = self.primary_keys()
        self.features["query_timeout"] = self.timeout()
        self.keywords = {keyword: self.connects(keyword) for keyword in KEYWORDS}

    def run_functions(self) -> dict[str, FunctionSupport]:
        # Date functions get {d} literals where those work, else the table's date column, unchecked.
        dated = self.features.get("date_literal", False)
        first, second = ("{d '2024-03-15'}", "{d '2024-03-16'}") if dated else (self.target.date or "NULL",) * 2
        found = {}
        for name, (template, expected) in FUNCTION_CHECKS.items():
            call = template.format(date=first, date2=second, key=self.target.key)
            check = _equals(expected) if dated or "{date" not in template else None
            escaped = self.attempt(f"fn:{name}", f"SELECT {{fn {call}}} FROM {self.target.table}", expect=check)
            bare = self.attempt(f"fn:{name} (bare)", f"SELECT {call} FROM {self.target.table}", expect=check)
            found[name] = FunctionSupport(escaped=escaped, bare=bare)
        return found

    def aggregate_exact(self) -> bool:
        tables = [self.target.table] + [f"{module}_Options" for module in ("CI", "AR", "AP", "GL")]
        for table in tables:
            try:
                columns = self.session.columns(table)
            except ProbeFailure:
                continue
            column = next((n for n, t in columns if t.upper().startswith(_DECIMAL_TYPES)), None)
            if column is not None:
                return self.attempt("aggregate_exact", f"SELECT SUM({column}) FROM {table}", expect=_exact)
        self.notes["aggregate_exact"] = "no decimal column to sum"
        return False

    def primary_keys(self) -> bool:
        try:
            keys = self.session.primary_keys(self.target.table)
        except ProbeFailure as failure:
            self.notes["primary_keys"] = str(failure)
            return False
        if not keys:
            self.notes["primary_keys"] = "SQLPrimaryKeys returned no columns"
        return bool(keys)

    def timeout(self) -> bool:
        try:
            self.session.timeout_accepted(PROBE_TIMEOUT_S)
        except ProbeFailure as failure:
            self.notes["query_timeout"] = str(failure)
            return False
        return True

    def connects(self, keyword: str) -> bool:
        # Only ReadOnly=1 and EnforceNullDate=1 are ever sent; the others are probed switched off.
        value = "1" if keyword in ("ReadOnly", "EnforceNullDate") else "0"
        try:
            self.session.connects_with(keyword, value, f"SELECT {self.target.key} FROM {self.target.table}")
        except ProbeFailure as failure:
            self.notes[f"keyword:{keyword}"] = str(failure)
            return False
        return True


def probe(session: Session, driver_version: str) -> Capabilities:
    """Run every check through ``session``; raises NoProbeTable when there is nothing to read."""
    target = find_target(session)
    checks = _Probe(session, target)
    checks.run()
    missing = set(FEATURES) - set(checks.features)
    assert not missing, f"features without a check: {sorted(missing)}"
    return Capabilities(
        driver_version=driver_version,
        source="probe",
        probed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        table=target.table,
        keywords=checks.keywords,
        features={name: checks.features[name] for name in FEATURES},
        functions=checks.functions,
        notes=dict(sorted(checks.notes.items())),
    )
