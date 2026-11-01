"""Backend B (SPEC.md §3): Sage 100 Premium, or a SQL mirror, through Microsoft's ODBC driver.

Connections are DSN-less, ask for ApplicationIntent=ReadOnly (layer 2 of §9) and set a lock
timeout so a read never blocks Sage users for long (§9.3). Each company database gets a small pool
whose size is the per-company concurrency limit.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from typing import Any

from sage100_mcp.backends.base import (
    DiscoveredCompany,
    PoolStats,
    RawResult,
    ResultColumn,
    description_type,
    odbc_value,
)
from sage100_mcp.capabilities import Capabilities
from sage100_mcp.catalog.builder import RawColumn, RawTable
from sage100_mcp.companies import read_tsql_options
from sage100_mcp.config import LimitsSection, SqlServerBackend
from sage100_mcp.credentials import Credential
from sage100_mcp.driver_errors import Phase, map_driver_error
from sage100_mcp.errors import ErrorCode, SageError

logger = logging.getLogger(__name__)

SESSION_SETUP = "SET NOCOUNT ON; SET LOCK_TIMEOUT 5000; SET TRANSACTION ISOLATION LEVEL READ COMMITTED;"
IDLE_SECONDS = 600  # pooled connections closed after 10 minutes idle (§8.3)
FETCH_BATCH = 500
VARCHAR_SIZE = 8000  # one size for every string parameter, so one cached plan serves values of any length

_METADATA_COLUMNS = """
SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE,
       c.IS_NULLABLE
FROM INFORMATION_SCHEMA.TABLES AS t
JOIN INFORMATION_SCHEMA.COLUMNS AS c ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
WHERE t.TABLE_TYPE = 'BASE TABLE' AND t.TABLE_SCHEMA = 'dbo'
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""
_METADATA_KEYS = """
SELECT t.name, c.name, i.is_primary_key, ic.key_ordinal
FROM sys.indexes AS i
JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
JOIN sys.tables AS t ON t.object_id = i.object_id
WHERE (i.is_primary_key = 1 OR i.type = 1) AND ic.key_ordinal > 0 AND SCHEMA_NAME(t.schema_id) = 'dbo'
ORDER BY t.name, i.is_primary_key DESC, i.index_id, ic.key_ordinal
"""
_METADATA_ROWS = """
SELECT t.name, SUM(p.rows)
FROM sys.tables AS t JOIN sys.partitions AS p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
WHERE SCHEMA_NAME(t.schema_id) = 'dbo'
GROUP BY t.name
"""


def _type_name(data_type: str, length: int | None, precision: int | None, scale: int | None) -> str:
    data_type = data_type.lower()
    if data_type in {"varchar", "nvarchar", "char", "nchar", "varbinary", "binary"}:
        return f"{data_type}({'max' if length == -1 else length})"
    if data_type in {"decimal", "numeric"}:
        return f"{data_type}({precision},{scale})"
    return data_type


def input_sizes(params: Sequence[Any], sql_varchar: int) -> list[tuple[int, int, int] | None] | None:
    """How to bind each parameter: a string that Sage's code page (cp1252) holds goes as varchar.

    pyodbc sends strings as nvarchar. Against Sage's varchar keys SQL Server then converts the column, and a
    filter such as CustomerNo = ? scans the table instead of seeking (docs/decisions.md D16, D59). A string
    the code page can't hold keeps nvarchar, so it still compares exactly. Other types keep pyodbc's binding.
    """
    sizes: list[tuple[int, int, int] | None] = []
    for value in params:
        size = None
        if isinstance(value, str) and len(value) <= VARCHAR_SIZE:
            try:
                value.encode("cp1252")
                size = (sql_varchar, VARCHAR_SIZE, 0)
            except UnicodeEncodeError:
                pass
        sizes.append(size)
    return sizes if any(sizes) else None


class _Pool:
    """Idle connections for one database, plus a gate that caps concurrent use."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.gate = threading.BoundedSemaphore(size)
        self.idle: deque[tuple[Any, float]] = deque()
        self.lock = threading.Lock()
        self.in_use = 0


class SqlServerExecutor:
    kind = "sqlserver"

    def __init__(self, name: str, settings: SqlServerBackend, credential: Credential, limits: LimitsSection) -> None:
        self.name = name
        self.settings = settings
        self.credential = credential
        self.limits = limits
        self._pools: dict[str, _Pool] = {}
        self._pools_lock = threading.Lock()
        self.driver_version: str | None = None
        self.server_version: str | None = None
        self.capabilities: Capabilities | None = None  # T-SQL needs no probe (§5.5)

    # --- connections -------------------------------------------------------------------------

    def database_for(self, company: str) -> str:
        return self.settings.database_pattern.format(company=company)

    def connection_string(self, database: str) -> str:
        parts = {
            "DRIVER": "{" + self.settings.driver + "}",
            "SERVER": odbc_value(self.settings.server),
            "DATABASE": odbc_value(database),
            "UID": odbc_value(self.credential.user),
            "PWD": odbc_value(self.credential.password),
            "Encrypt": "yes" if self.settings.encrypt else "no",
            "TrustServerCertificate": "yes" if self.settings.trust_server_certificate else "no",
            "ApplicationIntent": "ReadOnly",
            "APP": "sage100-mcp",
        }
        return ";".join(f"{key}={value}" for key, value in parts.items())

    def _error(self, exc: BaseException, phase: Phase, company: str | None = None) -> SageError:
        return map_driver_error(
            exc, backend="sqlserver", phase=phase, company=company, secrets=(self.credential.password,)
        )

    def _connect(self, database: str) -> Any:
        import pyodbc  # imported here so the package loads on hosts without unixODBC

        try:
            connection = pyodbc.connect(
                self.connection_string(database), autocommit=True, timeout=self.settings.connect_timeout_s
            )
            cursor = connection.cursor()
            cursor.execute(SESSION_SETUP)
            cursor.close()
        except pyodbc.Error as exc:
            raise self._error(exc, "connect") from None
        if self.driver_version is None:
            self.driver_version = connection.getinfo(pyodbc.SQL_DRIVER_VER)
            self.server_version = connection.getinfo(pyodbc.SQL_DBMS_VER)
        return connection

    def _pool(self, database: str) -> _Pool:
        with self._pools_lock:
            pool = self._pools.get(database)
            if pool is None:
                pool = self._pools[database] = _Pool(self.limits.per_company_concurrency.sqlserver)
            return pool

    @contextmanager
    def connection(self, database: str, wait_s: float) -> Iterator[Any]:
        """A pooled connection; waits up to ``wait_s`` for a free slot."""
        pool = self._pool(database)
        if not pool.gate.acquire(timeout=wait_s):
            raise SageError(
                ErrorCode.QUERY_TIMEOUT,
                "Too many queries are already running on this company.",
                hint="Try again in a moment.",
            )
        try:
            connection = None
            with pool.lock:
                pool.in_use += 1
                while pool.idle:
                    candidate, since = pool.idle.pop()
                    if time.monotonic() - since < IDLE_SECONDS:
                        connection = candidate
                        break
                    candidate.close()
            if connection is None:
                connection = self._connect(database)
            healthy = False
            try:
                yield connection
                healthy = True
            finally:
                if healthy:
                    with pool.lock:
                        pool.idle.append((connection, time.monotonic()))
                else:
                    with suppress(Exception):  # already broken
                        connection.close()
        finally:
            with pool.lock:
                pool.in_use -= 1
            pool.gate.release()

    def pool_stats(self) -> list[PoolStats]:
        with self._pools_lock:
            pools = sorted(self._pools.items())
        return [PoolStats(name, pool.size, pool.in_use, len(pool.idle)) for name, pool in pools]

    def close(self) -> None:
        with self._pools_lock:
            pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            with pool.lock:
                while pool.idle:
                    pool.idle.pop()[0].close()

    # --- queries ----------------------------------------------------------------------------

    def query(
        self,
        database: str,
        sql: str,
        params: Sequence[Any] = (),
        *,
        max_rows: int,
        timeout_s: int,
        company: str | None = None,
        skip: int = 0,
    ) -> RawResult:
        """Run one statement and fetch at most ``max_rows`` rows (one more is read to detect truncation)."""
        import pyodbc

        attempts = 2  # one retry for a lock timeout or a connection that dropped (§14.2)
        for attempt in range(attempts):
            try:
                with self.connection(database, wait_s=timeout_s) as connection:
                    connection.timeout = timeout_s
                    started = time.perf_counter()
                    cursor = connection.cursor()
                    try:
                        sizes = input_sizes(params, pyodbc.SQL_VARCHAR)
                        if sizes:
                            cursor.setinputsizes(sizes)
                        cursor.execute(sql, *params)
                        description = cursor.description or ()
                        if skip:
                            cursor.fetchmany(skip)
                        rows: list[tuple[Any, ...]] = []
                        while len(rows) <= max_rows:
                            batch = cursor.fetchmany(min(FETCH_BATCH, max_rows + 1 - len(rows)))
                            if not batch:
                                break
                            rows.extend(tuple(row) for row in batch)
                    finally:
                        cursor.close()
                    elapsed = int((time.perf_counter() - started) * 1000)
                    columns = tuple(ResultColumn(d[0], description_type(d)) for d in description)
                    return RawResult(columns, rows[:max_rows], len(rows) > max_rows, elapsed)
            except pyodbc.Error as exc:
                error = self._error(exc, "execute", company)
                if attempt + 1 < attempts and error.code in (ErrorCode.LOCK_TIMEOUT, ErrorCode.BACKEND_UNREACHABLE):
                    time.sleep(1)
                    continue
                raise error from None
        raise AssertionError("unreachable")

    def ping(self, database: str, sql: str, timeout_s: int) -> tuple[int, int]:
        """A new connection (not pooled) and one row of ``sql``: (connect ms, query ms)."""
        import pyodbc

        started = time.perf_counter()
        connection = self._connect(database)
        connected = time.perf_counter()
        try:
            connection.timeout = timeout_s
            cursor = connection.cursor()
            cursor.execute(sql)
            cursor.fetchone()
            cursor.close()
        except pyodbc.Error as exc:
            raise self._error(exc, "execute") from None
        finally:
            with suppress(Exception):
                connection.close()
        return int((connected - started) * 1000), int((time.perf_counter() - connected) * 1000)

    def read_options(self, database: str) -> dict[str, dict[str, Any] | None]:
        return read_tsql_options(self, database, self.limits.query_timeout_s.sqlserver)

    def health_query(self, modules: Sequence[str]) -> str:
        if modules:
            return f"SELECT TOP 1 1 AS [ok] FROM [dbo].[{modules[0]}_Options]"
        return "SELECT TOP 1 TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"  # before the profile is read

    def scalar_rows(self, database: str, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Internal metadata reads: all rows, the backend's own timeout."""
        result = self.query(database, sql, params, max_rows=1_000_000, timeout_s=self.limits.query_timeout_s.sqlserver)
        return result.rows

    # --- discovery and metadata --------------------------------------------------------------

    def discover(self) -> list[DiscoveredCompany]:
        """Company databases this login can open (§8.1 step 3), with names from SY_Company."""
        pattern = self.settings.database_pattern
        like = pattern.replace("[", "[[]").replace("_", "[_]").replace("{company}", "%")
        code_re = re.compile("^" + re.escape(pattern).replace(r"\{company\}", "(?P<code>[A-Za-z0-9]{3})") + "$", re.I)
        system = self.settings.system_database
        rows = self.scalar_rows(
            system,
            "SELECT name FROM sys.databases WHERE name LIKE ? AND name <> ? AND HAS_DBACCESS(name) = 1 ORDER BY name",
            (like, system),
        )
        names: dict[str, str] = {}
        try:
            for code, company_name in self.scalar_rows(system, "SELECT CompanyCode, CompanyName FROM dbo.SY_Company"):
                if code:
                    names[str(code).strip().upper()] = str(company_name or "").strip()
        except SageError as exc:
            logger.warning("Company names unavailable from %s.SY_Company: %s", system, exc.message)
        found = []
        for (database,) in rows:
            match = code_re.match(str(database))
            if match:
                code = match.group("code").upper()
                found.append(DiscoveredCompany(code, str(database), names.get(code) or None))
        return found

    def read_tables(self, database: str) -> tuple[RawTable, ...]:
        """Tables, columns, keys and row counts in three queries (§7.1)."""
        columns: dict[str, list[RawColumn]] = {}
        for table, column, data_type, length, precision, scale, nullable in self.scalar_rows(
            database, _METADATA_COLUMNS
        ):
            entry = RawColumn(column, _type_name(data_type, length, precision, scale), nullable == "YES")
            columns.setdefault(table, []).append(entry)
        keys: dict[str, list[str]] = {}
        key_source: dict[str, bool] = {}
        for table, column, is_primary, _ in self.scalar_rows(database, _METADATA_KEYS):
            primary = bool(is_primary)
            if table in key_source and key_source[table] != primary:
                continue  # the primary key won; skip the clustered index's columns
            key_source[table] = primary
            keys.setdefault(table, []).append(column)
        counts = {table: int(rows or 0) for table, rows in self.scalar_rows(database, _METADATA_ROWS)}
        return tuple(
            RawTable(name, tuple(cols), tuple(keys.get(name, ())), counts.get(name))
            for name, cols in sorted(columns.items(), key=lambda item: item[0].lower())
        )
