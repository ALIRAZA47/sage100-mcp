"""Hybrid execution on DuckDB (SPEC.md §5.5 step 4, §13.5).

Each pull is read through the driver with its own row cap (``max_pushdown_rows``), loaded into an
in-memory DuckDB with column types from the catalog, and the query runs there. Decimals stay exact
(DECIMAL, never DOUBLE), strings lose their trailing spaces, and masked columns never arrive: the
planner only pulls columns the query names, and masked ones are already constants (D14).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sage100_mcp.backends.base import RawResult, ResultColumn
from sage100_mcp.catalog.model import Column
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.sql.planner import Plan, Pull

LOAD_CHUNK = 20_000  # rows per INSERT ... SELECT UNNEST(...) statement
_DECIMAL = re.compile(r"(?:decimal|numeric)\((\d+),\s*(\d+)\)", re.IGNORECASE)
_STRING_TYPES = ("varchar", "char", "nvarchar", "nchar", "text")

Reader = Callable[[Pull, int, float], RawResult]  # pull, row cap, seconds left -> rows


@dataclass(frozen=True)
class HybridResult:
    raw: RawResult
    pulled: dict[str, int]  # table -> rows read through the driver
    null_dates_coerced: bool


def duckdb_type(column: Column) -> str:
    sql_type = column.type.lower()
    found = _DECIMAL.fullmatch(sql_type)
    if found:
        precision, scale = int(found.group(1)), int(found.group(2))
        return f"DECIMAL({min(precision, 38)},{min(scale, min(precision, 38))})"
    if sql_type.startswith(_STRING_TYPES):
        return "VARCHAR"
    if sql_type.startswith(("datetime", "timestamp", "smalldatetime")):
        return "TIMESTAMP"
    if sql_type.startswith("date"):
        return "DATE"
    if sql_type.startswith("time"):
        return "TIME"
    if sql_type in ("int", "integer", "smallint", "bigint", "tinyint"):
        return "BIGINT"
    if sql_type in ("float", "real", "double"):
        return "DOUBLE"
    if sql_type in ("bit", "boolean"):
        return "BOOLEAN"
    return "VARCHAR"


def _value(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "VARCHAR":
        return str(value).rstrip() if isinstance(value, str) else str(value)
    if kind == "DATE" and isinstance(value, datetime):
        return value.date()
    if kind == "TIMESTAMP" and isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, datetime.min.time())
    return value


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def run(
    plan: Plan,
    read: Reader,
    *,
    max_pushdown_rows: int,
    row_cap: int,
    timeout_s: float,
    memory_limit: str = "1GB",
    temp_directory: Path | None = None,
) -> HybridResult:
    """Pull every table, then run ``plan.local`` in DuckDB; returns at most ``row_cap`` rows (+1 to flag more)."""
    import duckdb

    assert plan.local is not None
    started = time.perf_counter()
    deadline = time.monotonic() + timeout_s
    connection = duckdb.connect(":memory:")
    pulled: dict[str, int] = {}
    coerced = False
    try:
        connection.execute(f"SET memory_limit = '{memory_limit}'")
        if temp_directory is not None:
            temp_directory.mkdir(parents=True, exist_ok=True)
            connection.execute("SET temp_directory = ?", [str(temp_directory)])
        for pull in plan.pulls:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _timeout()
            raw = read(pull, max_pushdown_rows, left)
            if raw.truncated:
                keys = ", ".join(pull.table.key) or "a key column"
                raise SageError(
                    ErrorCode.PUSHDOWN_LIMIT,
                    f"Reading {pull.table.name} for a local query passed {max_pushdown_rows:,} rows.",
                    hint=f"Add a filter on {keys} or a date range, so fewer rows have to be read.",
                )
            coerced = coerced or raw.null_dates_coerced
            pulled[pull.table.name] = pulled.get(pull.table.name, 0) + len(raw.rows)
            _load(connection, pull, raw)
        sql = plan.local.sql(dialect="duckdb")
        timer = threading.Timer(max(deadline - time.monotonic(), 0.0), connection.interrupt)
        timer.start()
        try:
            cursor = connection.execute(sql, plan.local_params)
            description = cursor.description or []
            rows = [tuple(row) for row in cursor.fetchmany(row_cap + 1)]
        except duckdb.InterruptException:
            raise _timeout() from None
        except duckdb.Error as exc:
            raise SageError(
                ErrorCode.INTERNAL,
                "The local engine could not run the query.",
                hint="Report it with the query; explain_query shows the plan.",
                detail=type(exc).__name__ + ": " + str(exc).splitlines()[0][:300],
            ) from None
        finally:
            timer.cancel()
    finally:
        connection.close()
    columns = tuple(ResultColumn(d[0], str(d[1]).lower()) for d in description)
    elapsed = int((time.perf_counter() - started) * 1000)
    raw_result = RawResult(columns, rows[:row_cap], len(rows) > row_cap, elapsed, null_dates_coerced=coerced)
    return HybridResult(raw_result, pulled, coerced)


def _timeout() -> SageError:
    return SageError(
        ErrorCode.QUERY_TIMEOUT,
        "The query took longer than its timeout.",
        hint="Add filters on key columns or a date range, or ask for fewer rows.",
    )


def _load(connection: Any, pull: Pull, raw: RawResult) -> None:
    kinds = [duckdb_type(column) for column in pull.columns]
    columns = ", ".join(f"{_quote(c.name)} {kind}" for c, kind in zip(pull.columns, kinds, strict=True))
    connection.execute(f"CREATE TABLE {_quote(pull.local_name)} ({columns})")
    if not raw.rows:
        return
    unnest = ", ".join(f"UNNEST(?::{kind}[])" for kind in kinds)
    statement = f"INSERT INTO {_quote(pull.local_name)} SELECT {unnest}"
    for start in range(0, len(raw.rows), LOAD_CHUNK):
        chunk = raw.rows[start : start + LOAD_CHUNK]
        values = [[_value(row[i], kinds[i]) for row in chunk] for i in range(len(kinds))]
        connection.execute(statement, values)
