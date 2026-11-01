"""A stand-in SQL Server backend for unit and contract tests, so they run on any OS without a database.

Discovery, profiles and table metadata come from the pinned MAS_EMP / MAS_PCC schemas; a query
returns whatever result the test queued, and every statement sent is recorded.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sage100_mcp.backends.base import DiscoveredCompany, PoolStats, RawResult, ResultColumn
from sage100_mcp.capabilities import Capabilities
from sage100_mcp.catalog.builder import RawTable
from sage100_mcp.companies import read_tsql_options
from sage100_mcp.config import Config, LoadedConfig, ProvideXBackend, SqlServerBackend
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.runtime import Runtime
from tests.premium_fixture import live_layer, schema

COMPANIES = {"EMP": "Example Emporium Inc.", "PCC": "Placeholder Capital Co."}
# Tests make far more than 30 calls a minute through one runtime; only the rate-limit tests set a real limit.
UNLIMITED = {"rate_per_min": 1_000_000}
OPTIONS: dict[str, dict[str, Any]] = {
    "AR_Options": {
        "DataVersion": Decimal("7.30"),
        "CurrentFiscalYr": "2024",
        "CurrentPeriod": "12",
        "Divisions": "N",
        "AgeInvoicesBy": "D",
        "AgingCategory1": Decimal(30),
        "AgingCategory2": Decimal(60),
        "AgingCategory3": Decimal(90),
        "AgingCategory4": Decimal(120),
    },
    "AP_Options": {"DataVersion": Decimal("7.30"), "CurrentFiscalYr": "2024", "CurrentPeriod": "12", "Divisions": "N"},
    "GL_Options": {"DataVersion": Decimal("7.30"), "CurrentFiscalYr": "2024", "CurrentPeriod": "12"},
    "CI_Options": {"DataVersion": Decimal("7.30")},
    "SO_Options": {"DataVersion": Decimal("7.30"), "CurrentFiscalYr": "2024", "CurrentPeriod": "12"},
    "PO_Options": {"DataVersion": Decimal("7.30"), "CurrentFiscalYr": "2024", "CurrentPeriod": "12"},
    "IM_Options": {"DataVersion": Decimal("7.30"), "CurrentFiscalYr": "2024", "CurrentPeriod": "12"},
}


@dataclass
class FakeExecutor:
    """Implements backends.base.Executor without a database."""

    companies: dict[str, str] = field(default_factory=lambda: dict(COMPANIES))
    data_version: str = "7.30"
    kind: str = "sqlserver"
    capabilities: Capabilities | None = None
    driver_version: str | None = "18.05.0001"
    server_version: str | None = "16.00.4135"
    sent: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    results: list[RawResult | SageError] = field(default_factory=list)
    reads: int = 0  # read_tables calls, to see whether a build used the cache
    fail_with: SageError | None = None  # raised by discover, read_tables and query when set
    on_query: Callable[[str], None] | None = None

    def database_for(self, company: str) -> str:
        return f"MAS_{company}"

    def discover(self) -> list[DiscoveredCompany]:
        if self.fail_with:
            raise self.fail_with
        return [DiscoveredCompany(code, f"MAS_{code}", name) for code, name in sorted(self.companies.items())]

    def read_tables(self, database: str) -> tuple[RawTable, ...]:
        if self.fail_with:
            raise self.fail_with
        self.reads += 1
        return live_layer(database).tables

    def read_options(self, database: str) -> dict[str, dict[str, Any] | None]:
        return read_tsql_options(self, database, 30)

    def pool_stats(self) -> list[PoolStats]:
        return [PoolStats(f"MAS_{code}", 4, 0, 1) for code in sorted(self.companies)]

    def health_query(self, modules: Sequence[str]) -> str:
        return f"SELECT TOP 1 1 AS [ok] FROM [dbo].[{modules[0]}_Options]" if modules else "SELECT 1"

    def scalar_rows(self, database: str, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        if "INFORMATION_SCHEMA.COLUMNS" in sql:  # the *_Options columns a profile reads
            rows = []
            for table in schema(database)["tables"]:
                if table["name"].endswith("_Options"):
                    rows += [(table["name"], column[0]) for column in table.get("columns", [])]
            return rows
        return self.query(database, sql, params, max_rows=1_000_000, timeout_s=30).rows

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
        if self.fail_with:
            raise self.fail_with
        if sql.startswith("SELECT TOP 1 ") and "_Options]" in sql:
            table = sql.rsplit("[", 1)[1].rstrip("]")
            values = dict(OPTIONS.get(table, {}))
            if "DataVersion" in values:
                values["DataVersion"] = Decimal(self.data_version)
            if table not in OPTIONS or not values:
                return RawResult((), [], False, 1)
            selected = [part.strip("[] ") for part in sql[len("SELECT TOP 1 ") : sql.index(" FROM ")].split(",")]
            row = tuple(values.get(name) for name in selected)
            return RawResult(tuple(ResultColumn(name, "varchar(10)") for name in selected), [row], False, 1)
        self.sent.append((sql, tuple(params)))
        if self.on_query:
            self.on_query(sql)
        if not self.results:
            return RawResult((ResultColumn("x", "int"),), [(1,)], False, 1)
        result = self.results.pop(0)
        if isinstance(result, SageError):
            raise result
        return RawResult(result.columns, result.rows[:max_rows], len(result.rows) > max_rows, result.elapsed_ms)

    def ping(self, database: str, sql: str, timeout_s: int) -> tuple[int, int]:
        if self.fail_with:
            raise self.fail_with
        self.sent.append((sql, ()))
        return 3, 2

    def close(self) -> None:
        pass


def result(columns: list[tuple[str, str]], rows: list[tuple[Any, ...]]) -> RawResult:
    return RawResult(tuple(ResultColumn(name, sql_type) for name, sql_type in columns), rows, False, 4)


def fake_config(
    companies: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    backends: tuple[str, ...] = ("premium",),
    **top: Any,
) -> LoadedConfig:
    """A loaded config with fake SQL Server backends; ``top`` sets other top-level keys, such as policy_file."""
    backend = {"type": "sqlserver", "server": "fake", "credential": "sage100-mcp/fake"}
    data: dict[str, Any] = {"backends": dict.fromkeys(backends, backend), "companies": companies or {}, **top}
    data["limits"] = UNLIMITED | (limits or {})
    return LoadedConfig(config=Config.model_validate(data), path=Path("fake.yaml"), found=True)


def fake_runtime(
    home: Path,
    executor: FakeExecutor | None = None,
    *,
    start: bool = True,
    companies: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    backends: tuple[str, ...] = ("premium",),
    **top: Any,
) -> tuple[Runtime, FakeExecutor]:
    """A runtime whose every backend is ``executor`` (a new FakeExecutor by default)."""
    fake = executor or FakeExecutor()

    def factory(
        name: str, settings: SqlServerBackend | ProvideXBackend, loaded: LoadedConfig, home: Path
    ) -> FakeExecutor:
        if fake.fail_with and fake.fail_with.code == ErrorCode.AUTH_FAILED:
            raise fake.fail_with
        return fake

    runtime = Runtime(fake_config(companies, limits, backends, **top), home=home, executor_factory=factory)
    if start:
        runtime.start(background=False)
    return runtime, fake
