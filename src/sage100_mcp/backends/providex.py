"""Backend A (SPEC.md §4): Sage 100 Standard and Advanced through Sage's ProvideX ODBC driver.

Connections are DSN-less, built from the install's MAS90 folder with the minimal keyword set of
§4.3; an optional keyword is added only after the probe has connected with it. ProvideX fixes the
company at connect time, so each company has its own small pool (§8.3). The driver build's SQL is
probed once per driver version and cached (§5.3). Every driver call runs on a worker thread so a
watchdog can cancel it at the deadline, and give up on it when even the cancel hangs (§13.2).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Literal, TypeVar

from sage100_mcp.backends.base import DiscoveredCompany, PoolStats, RawResult, ResultColumn, description_type
from sage100_mcp.backends.drivers import PROVIDEX_DRIVER
from sage100_mcp.backends.providex_probe import PROBE_TIMEOUT_S, NoProbeTable, ProbeFailure, probe, trim
from sage100_mcp.capabilities import Capabilities, baseline, cache_path, load, save
from sage100_mcp.catalog.builder import RawColumn, RawTable
from sage100_mcp.companies import OPTION_SETTINGS
from sage100_mcp.config import CompaniesSection, LimitsSection, ProvideXBackend
from sage100_mcp.credentials import Credential
from sage100_mcp.driver_errors import Phase, map_driver_error
from sage100_mcp.errors import ErrorCode, SageError

logger = logging.getLogger(__name__)

T = TypeVar("T")

FETCH_BATCH = 500
IDLE_SECONDS = 600  # pooled connections closed after 10 minutes idle (§8.3)
CONNECT_TIMEOUT_S = 15  # §13.2
CANCEL_GRACE_S = 5.0  # after a cancel, how long to wait before giving up on the call (§13.2)
CS_RETRY_DELAY_S = 2.0  # §14.2: CS_ODBC_UNREACHABLE is retried once, after 2 s
METADATA_TIMEOUT_S = 600  # SQLColumns over ~900 tables can be slow across a share
SQL_DRIVER_VER, SQL_DBMS_VER = 7, 18  # SQLGetInfo codes, as pyodbc names them
_COMPANY = re.compile(r"[A-Z0-9]{3}")
_FOLDER = re.compile(r"MAS_([A-Za-z0-9]{3})")
# A backend that fails these ways fails for every company; trying the next company would not help.
_BACKEND_FAILURES = {
    ErrorCode.AUTH_FAILED, ErrorCode.DRIVER_NOT_INSTALLED, ErrorCode.BITNESS_MISMATCH, ErrorCode.CS_ODBC_UNREACHABLE,
    ErrorCode.BACKEND_UNREACHABLE, ErrorCode.DRIVER_NOT_CAPABLE,
}  # fmt: skip


def _value(value: str) -> str:
    """Brace a value only where ODBC needs it: Sage's own strings carry Prefix=...\\==\\ unbraced."""
    if any(ch in value for ch in ";{}") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def _within(window: str, now: clock_time) -> bool:
    """``now`` inside "HH:MM-HH:MM", which may wrap past midnight."""
    start, end = (clock_time.fromisoformat(part) for part in window.split("-"))
    return start <= now < end if start <= end else now >= start or now < end


def _column_type(type_name: str, size: int | None, digits: int | None) -> str:
    """A catalog type from SQLColumns' TYPE_NAME, COLUMN_SIZE and DECIMAL_DIGITS."""
    name = (type_name or "").upper()
    if "CHAR" in name:
        return f"varchar({size or 0})"
    if name.startswith(("DEC", "NUMERIC")):
        return f"decimal({size or 15},{digits or 0})"
    if name.startswith(("TIMESTAMP", "DATETIME")):
        return "datetime"
    if name.startswith("DATE"):
        return "date"
    if name.startswith("TIME"):
        return "time"
    if "INT" in name:
        return "int"
    if name in ("DOUBLE", "FLOAT", "REAL"):
        return "float"
    if name == "BIT":
        return "bit"
    return name.lower() or "varchar(0)"


@dataclass
class _Watch:
    """One call on a worker thread: its outcome, and whether the caller gave up on it."""

    cursor: Any = None
    result: Any = None
    error: BaseException | None = None
    abandoned: bool = False
    finished: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self) -> None:
        if self.cursor is not None:
            with suppress(Exception):  # the call may finish while we cancel
                self.cursor.cancel()


class _Pool:
    """Idle connections for one company, plus a gate that caps concurrent use (§8.3, §13.5)."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.gate = threading.BoundedSemaphore(size)
        self.idle: deque[tuple[Any, float, str]] = deque()  # connection, idle since, connection string
        self.lock = threading.Lock()
        self.in_use = 0

    def take(self, text: str) -> Any:
        """An idle connection opened with ``text``; stale ones and ones opened otherwise are closed."""
        with self.lock:
            while self.idle:
                connection, since, opened_with = self.idle.pop()
                if getattr(connection, "closed", False):  # its 32-bit helper stopped (D67)
                    continue
                if opened_with == text and time.monotonic() - since < IDLE_SECONDS:
                    return connection
                with suppress(Exception):
                    connection.close()
        return None

    def give(self, connection: Any, text: str) -> None:
        with self.lock:
            self.idle.append((connection, time.monotonic(), text))

    def drain(self) -> None:
        with self.lock:
            while self.idle:
                with suppress(Exception):
                    self.idle.pop()[0].close()


class ProvideXExecutor:
    kind = "providex"

    def __init__(
        self,
        name: str,
        settings: ProvideXBackend,
        credential: Credential,
        limits: LimitsSection,
        *,
        home: Path,
        companies: CompaniesSection,
        odbc: Any = None,
        clock: Callable[[], datetime] = datetime.now,
        cancel_grace_s: float = CANCEL_GRACE_S,
        cs_retry_delay_s: float = CS_RETRY_DELAY_S,
    ) -> None:
        self.name = name
        self.settings = settings
        self.credential = credential
        self.limits = limits
        self.home = home
        self.companies = companies
        self._odbc = odbc
        self.clock = clock
        self.cancel_grace_s = cancel_grace_s
        self.cs_retry_delay_s = cs_retry_delay_s
        self.capabilities: Capabilities = baseline()
        self.capabilities_origin: Literal["not_probed", "cached", "probed"] = "not_probed"
        self.driver_version: str | None = None
        self.server_version: str | None = None
        self.connect_attempts = 0
        self._resolved = False  # capabilities known for this driver version (probed, cached or given up)
        self._capabilities_lock = threading.Lock()
        self._odbc_lock = threading.Lock()
        self._pools: dict[str, _Pool] = {}
        self._pools_lock = threading.Lock()

    @property
    def odbc(self) -> Any:
        """pyodbc, or the bridge to the 32-bit helper (D67).

        It is chosen on first use, so the package loads on hosts without the driver manager.
        """
        with self._odbc_lock:
            if self._odbc is None:
                from sage100_mcp.bridge import driver_module

                self._odbc = driver_module(self.settings)
            return self._odbc

    def driver_host(self) -> str | None:
        """Where Sage's driver runs when not in this process: the 32-bit helper (D67)."""
        describe = getattr(self._odbc, "describe", None)
        return str(describe()) if callable(describe) else None

    def _cache_path(self, version: str) -> Path:
        """The cached profile for ``version``; the 32-bit build's is kept apart, as the builds differ (§4.1)."""
        return cache_path(self.home, version, "-x86" if getattr(self._odbc, "bits", None) == 32 else "")

    def database_for(self, company: str) -> str:
        return company  # ProvideX connects to a company, not a database

    # --- connection strings (§4.3) ------------------------------------------------------------

    def _driver_options(self) -> dict[str, int]:
        options = dict(self.settings.driver_options)
        profile = self.settings.performance_profile
        if profile is not None and profile.after_hours and _within(profile.after_hours, self.clock().time()):
            options.update(DirtyReads=1, BurstMode=1)  # §13.1: faster reads while Sage users are away
        return options

    def connection_string(self, company: str, *, extra: Mapping[str, str] | None = None) -> str:
        directory = self.settings.directory
        root = directory.rstrip("\\/")
        sep = "/" if "/" in root and "\\" not in root else "\\"
        prefix = self.settings.prefix if self.settings.prefix != "auto" else f"{root}{sep}SY{sep}, {root}{sep}=={sep}"
        viewdll = self.settings.viewdll if self.settings.viewdll != "auto" else f"{root}{sep}Home"
        parts = {
            "Driver": "{" + PROVIDEX_DRIVER + "}",
            "UID": _value(self.credential.user),
            "PWD": _value(self.credential.password),
            "Company": company,
            "Directory": _value(directory),
            "Prefix": _value(prefix),
            "ViewDLL": _value(viewdll),
            "Silent": "1",  # never a logon dialog: a headless server would hang
            "SERVER": "NotTheServer",
            "StripTrailingSpaces": "1",
            **{keyword: str(setting) for keyword, setting in self._driver_options().items()},
        }
        if self.settings.transport == "cs" and self.settings.cs is not None:
            parts["RemotePVKIOHost"] = _value(self.settings.cs.host)
            parts["RemotePVKIOPort"] = str(self.settings.cs.port)
        if self.capabilities.accepts("ReadOnly"):
            parts["ReadOnly"] = "1"  # layer 2 of §9; Sage's driver is read-only anyway
        parts.update(extra or {})
        return ";".join(f"{keyword}={setting}" for keyword, setting in parts.items())

    # --- connections ------------------------------------------------------------------------------

    def _error(self, exc: BaseException, phase: Phase, company: str | None) -> SageError:
        return map_driver_error(
            exc, backend="providex", phase=phase, company=company, secrets=(self.credential.password,)
        )

    def _connect(self, company: str, *, extra: Mapping[str, str] | None = None, resolve: bool = True) -> Any:
        """A new connection; the first one for a driver version also settles its capabilities."""
        login_timeout = CONNECT_TIMEOUT_S
        for attempt in range(2):
            self.connect_attempts += 1
            text = self.connection_string(company, extra=extra)
            try:
                if login_timeout:
                    connection = self.odbc.connect(text, autocommit=True, timeout=login_timeout)
                else:
                    connection = self.odbc.connect(text, autocommit=True)
                break
            except self.odbc.Error as exc:
                error = self._error(exc, "connect", company)
                if attempt == 0 and error.code == ErrorCode.CS_ODBC_UNREACHABLE:
                    time.sleep(self.cs_retry_delay_s)
                    continue
                if attempt == 0 and error.code == ErrorCode.DRIVER_NOT_CAPABLE and login_timeout:
                    login_timeout = 0  # §14.2: once more, without the login timeout attribute
                    continue
                raise error from None
        if resolve and not self._resolved:
            self._resolve_capabilities(connection, company)
        return connection

    def _resolve_capabilities(self, connection: Any, company: str) -> None:
        with self._capabilities_lock:
            if self._resolved:
                return
            version = self._info(connection, "SQL_DRIVER_VER", SQL_DRIVER_VER) or "unknown"
            self.driver_version = version
            self.server_version = self._info(connection, "SQL_DBMS_VER", SQL_DBMS_VER) or None
            path = self._cache_path(version)
            cached = load(path)
            if cached is not None and cached.driver_version == version:
                self.capabilities, self.capabilities_origin = cached, "cached"
            else:
                found = self._probe(connection, company, version)
                self.capabilities = found if found is not None else baseline(version)
                if found is not None:
                    save(path, found)
                    self.capabilities_origin = "probed"
            self._resolved = True

    def _info(self, connection: Any, name: str, code: int) -> str:
        """SQLGetInfo, or "" where the driver does not answer that question."""
        try:
            return str(connection.getinfo(getattr(self.odbc, name, code)) or "").strip()
        except self.odbc.Error:
            return ""

    def _probe(self, connection: Any, company: str, version: str) -> Capabilities | None:
        started = time.perf_counter()
        try:
            found = probe(_ProbeSession(self, connection, company), version)
        except NoProbeTable:
            logger.warning(
                "No rows to probe driver %s with in company %s; using the baseline profile", version, company
            )
            return None
        logger.info("Probed ProvideX driver %s in %.1f s", version, time.perf_counter() - started)
        return found

    def refresh_capabilities(self, company: str) -> Capabilities:
        """Probe again (the refresh_capabilities tool), replacing the cached profile."""
        connection = self._connect(company)
        try:
            version = self.driver_version or "unknown"
            found = self._probe(connection, company, version)
        finally:
            with suppress(Exception):
                connection.close()
        if found is None:
            raise SageError(
                ErrorCode.INTERNAL,
                f"No table in company {company} has a row to probe the driver with.",
                hint="Probe through a company with data.",
                company=company,
            )
        with self._capabilities_lock:
            save(self._cache_path(found.driver_version), found)
            self.capabilities, self.capabilities_origin = found, "probed"
        return found

    def _pool(self, company: str) -> _Pool:
        with self._pools_lock:
            pool = self._pools.get(company)
            if pool is None:
                pool = self._pools[company] = _Pool(self.limits.per_company_concurrency.providex)
            return pool

    def pool_stats(self) -> list[PoolStats]:
        with self._pools_lock:
            pools = sorted(self._pools.items())
        return [PoolStats(name, pool.size, pool.in_use, len(pool.idle)) for name, pool in pools]

    def close(self) -> None:
        with self._pools_lock:
            pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            pool.drain()
        shutdown = getattr(self._odbc, "shutdown", None)
        if callable(shutdown):
            shutdown()  # the 32-bit helper, where this backend has one

    # --- calls with a deadline (§13.2) -------------------------------------------------------------

    def _call(
        self,
        company: str,
        work: Callable[[Any, _Watch], T],
        *,
        timeout_s: float,
        extra: Mapping[str, str] | None = None,
        pooled: bool = True,
    ) -> T:
        """Run ``work(connection, watch)`` on a worker thread, cancelling it at the deadline."""
        pool = self._pool(company)
        if not pool.gate.acquire(timeout=timeout_s):
            raise SageError(
                ErrorCode.QUERY_TIMEOUT,
                f"Too many queries are already running on company {company}.",
                hint="Try again in a moment.",
                company=company,
            )
        with pool.lock:
            pool.in_use += 1
        try:
            text = self.connection_string(company, extra=extra)  # pooled connections are tagged with it
            connection = (pool.take(text) if pooled else None) or self._connect(company, extra=extra)
            watch = _Watch()

            def run() -> None:
                try:
                    watch.result = work(connection, watch)
                except BaseException as exc:  # handed to the caller's thread
                    watch.error = exc
                finally:
                    with watch.lock:
                        watch.finished = True
                        abandoned = watch.abandoned
                    watch.done.set()
                    if abandoned:  # the caller gave up: nobody else will close it
                        with suppress(Exception):
                            connection.close()

            threading.Thread(target=run, name=f"sage100-providex-{company}", daemon=True).start()
            # A driver that enforces the query timeout gets the first chance to stop the call.
            slack = 2.0 if self.capabilities.supports("query_timeout") else 0.0
            if not watch.done.wait(timeout_s + slack):
                watch.cancel()
                if not watch.done.wait(self.cancel_grace_s):
                    with watch.lock:
                        watch.abandoned = not watch.finished
                    if watch.abandoned:
                        logger.warning("A driver call on %s ignored cancel; its connection is discarded", company)
                        raise SageError(
                            ErrorCode.QUERY_TIMEOUT,
                            "The query took longer than its timeout, and the driver did not stop it.",
                            hint="Add filters on key columns or a date range, or ask for fewer rows.",
                            company=company,
                        )
            if watch.error is not None:
                with suppress(Exception):
                    connection.close()
                if isinstance(watch.error, self.odbc.Error):
                    raise self._error(watch.error, "execute", company) from None
                raise watch.error
            if pooled:
                pool.give(connection, text)
            else:
                with suppress(Exception):
                    connection.close()
            result: T = watch.result
            return result
        finally:
            with pool.lock:
                pool.in_use -= 1
            pool.gate.release()  # an abandoned call's slot too: the pool replaces its connection (§13.2)

    # --- queries ---------------------------------------------------------------------------------------

    def _fetch(
        self,
        connection: Any,
        watch: _Watch,
        sql: str,
        params: Sequence[Any],
        *,
        max_rows: int,
        skip: int,
        timeout_s: int,
    ) -> RawResult:
        if self.capabilities.supports("query_timeout"):
            connection.timeout = timeout_s  # pyodbc sets SQL_ATTR_QUERY_TIMEOUT on each new statement
        started = time.perf_counter()
        cursor = connection.cursor()
        watch.cursor = cursor
        try:
            cursor.execute(sql, *params)
            description = cursor.description or ()
            skipped = 0
            while skipped < skip:  # §13.3: an OFFSET the driver can't take
                batch = cursor.fetchmany(min(FETCH_BATCH, skip - skipped))
                if not batch:
                    break
                skipped += len(batch)
            rows: list[tuple[Any, ...]] = []
            while len(rows) <= max_rows:
                batch = cursor.fetchmany(min(FETCH_BATCH, max_rows + 1 - len(rows)))
                if not batch:
                    break
                rows.extend(tuple(row) for row in batch)
        finally:
            with suppress(Exception):
                cursor.close()
        elapsed = int((time.perf_counter() - started) * 1000)
        columns = tuple(ResultColumn(d[0], description_type(d)) for d in description)
        return RawResult(columns, rows[:max_rows], len(rows) > max_rows, elapsed)

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
        """One statement; at most ``max_rows`` rows (one more is read to detect truncation)."""

        def work(connection: Any, watch: _Watch) -> RawResult:
            return self._fetch(connection, watch, sql, params, max_rows=max_rows, skip=skip, timeout_s=timeout_s)

        try:
            return self._call(database, work, timeout_s=timeout_s)
        except SageError as error:
            if error.code != ErrorCode.DRIVER_INTERNAL or not self.capabilities.accepts("EnforceNullDate"):
                raise
        # §5.4: invalid stored dates surface as Internal error; with Null Date on they read as NULL.
        logger.info("Internal error on %s; retrying with EnforceNullDate=1", database)
        retried = self._call(database, work, timeout_s=timeout_s, extra={"EnforceNullDate": "1"}, pooled=False)
        return replace(retried, null_dates_coerced=True)

    def scalar_rows(self, database: str, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return self.query(
            database, sql, params, max_rows=1_000_000, timeout_s=self.limits.query_timeout_s.providex
        ).rows

    def health_query(self, modules: Sequence[str]) -> str:
        """One row of a small table: never SELECT without FROM, which the grammar lacks (§5.2)."""
        if modules:
            return f"SELECT ModuleCode FROM {modules[0]}_Options"
        if self.capabilities.table:
            return f"SELECT * FROM {self.capabilities.table}"
        return "SELECT ModuleCode FROM CI_Options"

    def ping(self, database: str, sql: str, timeout_s: int) -> tuple[int, int]:
        """A new connection (not pooled) and one row of ``sql``: (connect ms, query ms)."""
        started = time.perf_counter()
        connection = self._connect(database)
        connected = time.perf_counter()
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            cursor.fetchone()
            cursor.close()
        except self.odbc.Error as exc:
            raise self._error(exc, "execute", database) from None
        finally:
            with suppress(Exception):
                connection.close()
        return int((connected - started) * 1000), int((time.perf_counter() - connected) * 1000)

    # --- discovery and metadata (§7.1, §8.1) --------------------------------------------------------

    def _folder_codes(self) -> list[str]:
        """Company codes from the MAS90\\MAS_xxx data folders (§8.1 step 2)."""
        try:
            names = [entry.name for entry in Path(self.settings.directory).iterdir() if entry.is_dir()]
        except OSError:
            return []
        return sorted({found.group(1).upper() for name in names if (found := _FOLDER.fullmatch(name))})

    def discover(self) -> list[DiscoveredCompany]:
        """SY_Company read through a company connection, else the MAS_xxx folders."""
        folders = self._folder_codes()
        settings = self.companies
        candidates = [settings.default, *(settings.allow or ()), *settings.backend_map, *folders]
        mapped = [c for c in dict.fromkeys(candidates) if c and settings.backend_map.get(c, self.name) == self.name]
        usable = [c for c in mapped if c not in settings.hide]
        if not usable:
            raise SageError(
                ErrorCode.COMPANY_REQUIRED,
                "The ProvideX driver connects to one company, and none is known to connect with.",
                hint="Set companies.default in config.yaml, or check that the backend's directory is the MAS90 folder.",
            )
        failure: SageError | None = None
        for code in usable:
            try:
                rows = self.scalar_rows(code, "SELECT CompanyCode, CompanyName FROM SY_Company")
            except SageError as exc:
                if exc.code in _BACKEND_FAILURES:
                    raise
                if exc.code == ErrorCode.COMPANY_NOT_FOUND:
                    failure = exc
                    continue
                logger.warning("SY_Company unreadable through %s (%s); listing MAS_ folders", code, exc.message)
                return [DiscoveredCompany(c, c, None) for c in folders or [code]]
            names: dict[str, str | None] = {}
            for company_code, company_name in rows:
                text = str(company_code or "").strip().upper()
                if _COMPANY.fullmatch(text):
                    names[text] = str(company_name or "").strip() or None
            return [DiscoveredCompany(c, c, name) for c, name in sorted(names.items())]
        assert failure is not None
        raise failure

    def read_tables(self, database: str) -> tuple[RawTable, ...]:
        """SQLTables, SQLColumns and, where the probe passed, SQLPrimaryKeys; never a row count (§7.1)."""

        def work(connection: Any, watch: _Watch) -> tuple[RawTable, ...]:
            with_keys = self.capabilities.supports("primary_keys")  # known once connected
            cursor = connection.cursor()
            watch.cursor = cursor
            try:
                names = [
                    str(row.table_name)
                    for row in cursor.tables().fetchall()
                    if str(row.table_type or "TABLE").upper() == "TABLE"
                    and not str(row.table_name).upper().startswith("SY_")
                ]
                columns = self._columns(cursor, connection, names)
                keys = {name: self._keys(cursor, name) for name in names} if with_keys else {}
            finally:
                with suppress(Exception):
                    cursor.close()
            return tuple(
                RawTable(name, tuple(columns.get(name, ())), keys.get(name, ()), None)
                for name in sorted(names, key=str.lower)
                if columns.get(name)
            )

        return self._call(database, work, timeout_s=METADATA_TIMEOUT_S)

    def _columns(self, cursor: Any, connection: Any, names: list[str]) -> dict[str, list[RawColumn]]:
        wanted = set(names)
        found: dict[str, list[RawColumn]] = {}
        try:
            rows = cursor.columns().fetchall()
        except self.odbc.Error:
            rows = None
        if rows is not None:
            for row in sorted(rows, key=lambda r: (str(r.table_name), int(r.ordinal_position or 0))):
                if str(row.table_name) in wanted:
                    found.setdefault(str(row.table_name), []).append(_raw_column(row))
            return found
        for name in names:  # one table at a time, then from a result's description (§7.1 fallback)
            try:
                found[name] = [_raw_column(row) for row in cursor.columns(table=name).fetchall()]
            except self.odbc.Error:
                found[name] = self._described(connection, name)
        return found

    def _described(self, connection: Any, table: str) -> list[RawColumn]:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SELECT * FROM {table}")
            return [RawColumn(str(d[0]), description_type(d), True) for d in cursor.description or ()]
        except self.odbc.Error:
            return []
        finally:
            with suppress(Exception):
                cursor.cancel()
                cursor.close()

    def _keys(self, cursor: Any, table: str) -> tuple[str, ...]:
        try:
            rows = cursor.primaryKeys(table).fetchall()
        except self.odbc.Error:
            return ()
        return tuple(str(row.column_name) for row in sorted(rows, key=lambda r: int(r.key_seq or 0)))

    def read_options(self, database: str) -> dict[str, dict[str, Any] | None]:
        """Each module's settings row, reading only the columns a profile uses (§8.2)."""

        def work(connection: Any, watch: _Watch) -> dict[str, list[str]]:
            cursor = connection.cursor()
            watch.cursor = cursor
            try:
                tables = [
                    str(row.table_name)
                    for row in cursor.tables(table="%_Options").fetchall()
                    if str(row.table_name).upper().endswith("_OPTIONS")
                    and not str(row.table_name).upper().startswith("SY_")
                ]
                found = {}
                for table in tables:
                    names = [str(row.column_name) for row in cursor.columns(table=table).fetchall()]
                    found[table] = [n for n in names if n in OPTION_SETTINGS] or names[:1]
                return found
            finally:
                with suppress(Exception):
                    cursor.close()

        timeout = self.limits.query_timeout_s.providex
        options: dict[str, dict[str, Any] | None] = {}
        for table, columns in self._call(database, work, timeout_s=timeout).items():
            if not columns:
                options[table] = None
                continue
            result = self.query(database, f"SELECT {', '.join(columns)} FROM {table}", max_rows=1, timeout_s=timeout)
            options[table] = dict(zip(columns, result.rows[0], strict=True)) if result.rows else None
        return options


def _raw_column(row: Any) -> RawColumn:
    return RawColumn(
        str(row.column_name),
        _column_type(str(row.type_name or ""), row.column_size, row.decimal_digits),
        bool(row.nullable),
    )


class _ProbeSession:
    """The probe's view of one connection (backends/providex_probe.py)."""

    def __init__(self, executor: ProvideXExecutor, connection: Any, company: str) -> None:
        self.executor = executor
        self.connection = connection
        self.company = company

    def _deadline(self, cursor: Any, work: Callable[[], T]) -> T:
        """``work`` on a worker thread, cancelled after the probe's 5 s (§5.3)."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["value"] = work()
            except BaseException as exc:  # handed back below
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=run, name="sage100-probe", daemon=True).start()
        if not done.wait(PROBE_TIMEOUT_S):
            with suppress(Exception):
                cursor.cancel()
            if not done.wait(self.executor.cancel_grace_s):
                raise ProbeFailure("did not finish within the probe's timeout")
        if "error" in box:
            error = box["error"]
            if isinstance(error, self.executor.odbc.Error):
                raise ProbeFailure(trim(str(error.args[1] if len(error.args) > 1 else error))) from None
            raise error
        value: T = box["value"]
        return value

    def rows(
        self, sql: str, params: Sequence[Any] = (), *, limit: int = 1
    ) -> tuple[list[tuple[Any, ...]], Sequence[Any]]:
        self.connection.timeout = 0
        cursor = self.connection.cursor()

        def work() -> tuple[list[tuple[Any, ...]], Sequence[Any]]:
            cursor.execute(sql, *params)
            return [tuple(row) for row in cursor.fetchmany(limit)], list(cursor.description or ())

        try:
            return self._deadline(cursor, work)
        finally:
            with suppress(Exception):
                cursor.close()

    def columns(self, table: str) -> list[tuple[str, str]]:
        cursor = self.connection.cursor()
        try:
            rows = self._deadline(cursor, lambda: cursor.columns(table=table).fetchall())
        finally:
            with suppress(Exception):
                cursor.close()
        return [(str(row.column_name), str(row.type_name or "")) for row in rows]

    def primary_keys(self, table: str) -> list[str]:
        cursor = self.connection.cursor()
        try:
            rows = self._deadline(cursor, lambda: cursor.primaryKeys(table).fetchall())
        finally:
            with suppress(Exception):
                cursor.close()
        return [str(row.column_name) for row in rows]

    def connects_with(self, keyword: str, value: str, sql: str) -> None:
        text = self.executor.connection_string(self.company, extra={keyword: value})
        try:
            connection = self.executor.odbc.connect(text, autocommit=True, timeout=CONNECT_TIMEOUT_S)
        except self.executor.odbc.Error as exc:
            raise ProbeFailure(trim(str(exc.args[1] if len(exc.args) > 1 else exc))) from None
        try:
            _ProbeSession(self.executor, connection, self.company).rows(sql)
        finally:
            with suppress(Exception):
                connection.close()

    def timeout_accepted(self, seconds: float) -> None:
        self.connection.timeout = int(seconds)
        try:
            cursor = self.connection.cursor()
        except self.executor.odbc.Error as exc:
            raise ProbeFailure(trim(str(exc.args[1] if len(exc.args) > 1 else exc))) from None
        finally:
            self.connection.timeout = 0
        with suppress(Exception):
            cursor.close()
