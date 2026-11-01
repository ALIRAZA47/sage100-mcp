"""The executor protocol every backend implements, and what it returns (SPEC.md §10, §13.2)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sage100_mcp.capabilities import Capabilities
    from sage100_mcp.catalog.builder import RawTable


@dataclass(frozen=True)
class ResultColumn:
    name: str
    type: str  # varchar(20), decimal(13,2), datetime, int, ...


@dataclass(frozen=True)
class RawResult:
    """Rows as the driver returned them (Decimal, datetime, str), before formatting."""

    columns: tuple[ResultColumn, ...]
    rows: list[tuple[Any, ...]]
    truncated: bool  # more rows existed than were fetched
    elapsed_ms: int
    null_dates_coerced: bool = False  # ProvideX: re-read with EnforceNullDate after an Internal error (§5.4)


@dataclass(frozen=True)
class DiscoveredCompany:
    code: str
    database: str
    name: str | None


@dataclass(frozen=True)
class PoolStats:
    """One connection pool: its cap (§13.5), connections in use, and idle ones kept for reuse."""

    name: str  # the database (Premium) or company (ProvideX)
    cap: int
    in_use: int
    idle: int


class Executor(Protocol):
    """One configured backend: discovery, metadata and bounded reads (sqlserver.py, providex.py)."""

    kind: str
    driver_version: str | None
    server_version: str | None

    @property
    def capabilities(self) -> Capabilities | None:
        """ProvideX: the SQL this driver build accepts (§5.3). None where no probe applies."""
        ...

    def database_for(self, company: str) -> str: ...

    def discover(self) -> list[DiscoveredCompany]: ...

    def read_tables(self, database: str) -> tuple[RawTable, ...]: ...

    def read_options(self, database: str) -> dict[str, dict[str, Any] | None]:
        """Each *_Options table's settings row (None when the table is empty), for the profile (§8.2)."""
        ...

    def pool_stats(self) -> list[PoolStats]:
        """Each pool's cap, connections in use and idle connections, for sage_status and soak runs."""
        ...

    def health_query(self, modules: Sequence[str]) -> str:
        """A one-row read of a small table, for test_connection and doctor."""
        ...

    def scalar_rows(self, database: str, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]: ...

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
    ) -> RawResult: ...

    def ping(self, database: str, sql: str, timeout_s: int) -> tuple[int, int]: ...

    def close(self) -> None: ...


def description_type(description: Sequence[Any]) -> str:
    """A SQL type name from a DB-API cursor.description entry."""
    _, type_code, _, internal_size, precision, scale, _ = description
    if type_code is str:
        return f"varchar({internal_size})" if internal_size and internal_size > 0 else "varchar(max)"
    if type_code is Decimal:
        return f"decimal({precision},{scale})"
    names = {int: "int", float: "float", bool: "bit", bytes: "varbinary", bytearray: "varbinary"}
    return names.get(type_code, getattr(type_code, "__name__", str(type_code)).lower())


def odbc_value(value: str) -> str:
    """Quote a connection-string value when it contains characters ODBC treats as syntax."""
    if any(ch in value for ch in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value
