"""What ``sage_status`` reports: server, host, config, backends and setup notes (SPEC.md §11.1)."""

from __future__ import annotations

import os
import platform
import sys
from importlib.metadata import version
from typing import TYPE_CHECKING, Literal

from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, Field

from sage100_mcp import __version__, proc
from sage100_mcp.backends.drivers import DriverRegistration, check_driver, process_bitness, registered_drivers
from sage100_mcp.bridge import providex_check
from sage100_mcp.capabilities import CapabilitySummary, summarize
from sage100_mcp.config import LoadedConfig, ProvideXBackend, SqlServerBackend
from sage100_mcp.errors import ErrorEnvelope
from sage100_mcp.formatting import text_fields, text_table

if TYPE_CHECKING:
    from sage100_mcp.runtime import Runtime

MILESTONE = "M6"
PROVIDEX_REMEDY = "run the Sage 100 Workstation Setup from the Sage server share on this machine (SPEC.md §16.5)"
SQLSERVER_REMEDY = "install the Microsoft ODBC Driver for SQL Server named in the backend's driver setting"


class ServerInfo(BaseModel):
    name: str
    version: str
    milestone: str
    mcp_sdk: str
    protocol_versions: list[str]


class HostInfo(BaseModel):
    platform: str
    os: str
    python: str
    bitness: Literal[32, 64]


class ConfigInfo(BaseModel):
    path: str
    found: bool
    valid: bool
    error: str | None = None
    env_overrides: list[str]


class DriverStatus(BaseModel):
    name: str
    registered: bool | None
    loadable: bool | None
    registered_bitness: list[Literal[32, 64]]
    odbc_version: str | None
    message: str
    helper: str | None = Field(
        default=None,
        description="The 32-bit helper that loads a 32-bit-only Sage driver for this 64-bit server (D67)",
    )


class PoolInfo(BaseModel):
    """One connection pool (§13.5): a soak run checks in_use + idle never passes cap."""

    name: str = Field(description="The database (Premium) or company (ProvideX)")
    cap: int
    in_use: int
    idle: int


class ProcessInfo(BaseModel):
    pid: int
    memory_mb: float | None = Field(description="Resident memory: the working set on Windows")
    threads: int
    uptime_s: int


class BackendStatus(BaseModel):
    name: str
    type: Literal["providex", "sqlserver"]
    transport: Literal["local", "cs"] | None
    target: str
    state: Literal["not_connected", "connected", "error", "unsupported_platform"]
    driver: DriverStatus
    driver_version: str | None = None  # SQL_DRIVER_VER, known after the first connection
    server_version: str | None = None
    capabilities: Literal["not_probed", "cached", "probed", "not_applicable"]
    capability_summary: CapabilitySummary | None = None  # ProvideX, once connected (§5.3)
    error: ErrorEnvelope | None = None  # why the backend could not be reached
    pools: list[PoolInfo] = Field(default_factory=list)


class CompaniesStatus(BaseModel):
    default: str | None
    allow: list[str] | None
    hide: list[str]
    discovered: list[str] | None = None  # None until discovery has run
    active: str | None = None


class SageStatus(BaseModel):
    server: ServerInfo
    host: HostInfo
    process: ProcessInfo
    config: ConfigInfo
    backends: list[BackendStatus]
    companies: CompaniesStatus
    catalog_status: Literal["not_built", "building", "ready", "stale"]
    notes: list[str]


def _backend_status(
    name: str,
    backend: ProvideXBackend | SqlServerBackend,
    drivers: list[DriverRegistration] | None,
    working: bool = False,
) -> tuple[BackendStatus, list[str]]:
    """``working``: the running server has already listed companies through this backend."""
    notes: list[str] = []
    transport: Literal["local", "cs"] | None = None
    state: Literal["not_connected", "unsupported_platform"] = "not_connected"
    capabilities: Literal["not_probed", "not_applicable"] = "not_applicable"  # Premium needs no probe (§5.5)
    if isinstance(backend, ProvideXBackend):
        check = providex_check(backend, drivers, remedy=PROVIDEX_REMEDY)
        target, transport, capabilities = backend.directory, backend.transport, "not_probed"
        if backend.transport == "cs" and backend.cs is not None:
            target += f" (Client/Server ODBC via {backend.cs.host}:{backend.cs.port})"
        if sys.platform != "win32" and not working:
            state = "unsupported_platform"
            notes.append(
                f"Backend {name}: the ProvideX driver is a Windows DLL, so run this backend on Windows (§10.2)."
            )
    else:
        check = check_driver(backend.driver, drivers, remedy=SQLSERVER_REMEDY)
        target = backend.server
    if check.loadable is False:
        notes.append(f"Backend {name}: {check.name} is {check.message}.")
    driver = DriverStatus(
        name=check.name,
        registered=check.registered,
        loadable=check.loadable,
        registered_bitness=list(check.registered_bitness),
        odbc_version=check.odbc_version,
        message=check.message,
        helper=check.helper,
    )
    status = BackendStatus(
        name=name,
        type=backend.type,
        transport=transport,
        target=target,
        state=state,
        driver=driver,
        capabilities=capabilities,
    )
    return status, notes


def _live(status: BackendStatus, runtime: Runtime) -> None:
    """Fill in what the running server knows: connection state, versions, the last error."""
    state = runtime.backends.get(status.name)
    if state is None or status.state == "unsupported_platform":
        return
    if state.error is not None:
        status.state, status.error = "error", state.error.envelope()
    elif state.discovered:
        status.state = "connected"
    if state.executor is not None:
        host = getattr(state.executor, "driver_host", None)
        status.driver.helper = (host() if callable(host) else None) or status.driver.helper
        status.driver_version = state.executor.driver_version
        status.server_version = state.executor.server_version
        status.pools = [PoolInfo(**vars(pool)) for pool in state.executor.pool_stats()]
        caps = state.executor.capabilities
        origin = getattr(state.executor, "capabilities_origin", None)
        if caps is not None and origin in ("not_probed", "cached", "probed"):
            status.capabilities = origin
            status.capability_summary = summarize(caps) if status.driver_version else None


def _catalog_summary(runtime: Runtime) -> tuple[Literal["not_built", "building", "ready", "stale"], list[str]]:
    visible = runtime.visible_companies()
    statuses = {code: runtime.status_of(code) for code in visible}
    notes = []
    for code, state in runtime.catalogs.items():
        if code in visible and state.error is not None:
            notes.append(f"Catalog for {code}: {state.error.code} {state.error.message}")
    values = set(statuses.values())
    if "building" in values:
        return "building", notes
    if values and values <= {"ready", "stale"}:
        return ("stale" if "stale" in values else "ready"), notes
    return ("ready" if "ready" in values else "not_built"), notes


def collect_status(
    loaded: LoadedConfig, drivers: list[DriverRegistration] | None = None, runtime: Runtime | None = None
) -> SageStatus:
    """Build the status report. ``drivers`` defaults to this host's registrations."""
    config = loaded.config
    if drivers is None:
        drivers = registered_drivers()
    notes: list[str] = []
    if loaded.error:
        notes.append("The configuration was rejected, so no backend is enabled; config.error says why.")
    elif not loaded.found:
        notes.append(f"No config file at {loaded.path}; docs/config.example.yaml in the repository shows every key.")
    backends = []
    for name, backend in config.backends.items():
        live = runtime.backends.get(name) if runtime is not None else None
        status, backend_notes = _backend_status(name, backend, drivers, working=bool(live and live.discovered))
        if runtime is not None:
            _live(status, runtime)
            if status.error is not None:
                backend_notes.append(f"Backend {name}: {status.error.code} {status.error.message}")
        backends.append(status)
        notes += backend_notes
    if not config.backends and not loaded.error:
        notes.append("No backends are configured, so no company data is reachable.")
    catalog_status: Literal["not_built", "building", "ready", "stale"] = "not_built"
    discovered, active = None, None
    if runtime is not None:
        notes += [f"Not serving: {error}" for error in runtime.errors if error != loaded.error]
        if runtime.discovery_done:
            discovered, active = list(runtime.visible_companies()), runtime.active_company
            catalog_status, catalog_notes = _catalog_summary(runtime)
            notes += catalog_notes
    notes.append(
        f"This is a pre-release build ({MILESTONE}): the discovery tools, run_query with paging and the canned "
        "views (ar_aging and the rest) work on Premium and ProvideX companies, over stdio or over HTTP with "
        "`sage100-mcp serve`."
    )
    return SageStatus(
        server=ServerInfo(
            name=config.server.name,
            version=__version__,
            milestone=MILESTONE,
            mcp_sdk=version("mcp"),
            protocol_versions=[*HANDSHAKE_PROTOCOL_VERSIONS, *MODERN_PROTOCOL_VERSIONS],
        ),
        host=HostInfo(
            platform=sys.platform,
            os=platform.platform(terse=True),
            python=platform.python_version(),
            bitness=process_bitness(),
        ),
        process=ProcessInfo(
            pid=os.getpid(), memory_mb=proc.resident_mb(), threads=proc.threads(), uptime_s=proc.uptime_s()
        ),
        config=ConfigInfo(
            path=str(loaded.path),
            found=loaded.found,
            valid=loaded.error is None,
            error=loaded.error,
            env_overrides=list(loaded.env_overrides),
        ),
        backends=backends,
        companies=CompaniesStatus(
            default=config.companies.default,
            allow=config.companies.allow,
            hide=config.companies.hide,
            discovered=discovered,
            active=active,
        ),
        catalog_status=catalog_status,
        notes=notes,
    )


def status_text(status: SageStatus) -> str:
    server, host, config = status.server, status.host, status.config
    header = text_fields(
        [
            ("server", f"{server.name} {server.version} ({server.milestone}), MCP SDK {server.mcp_sdk}"),
            ("host", f"{host.os}, Python {host.python} {host.bitness}-bit"),
            ("process", _process_text(status.process)),
            ("config", f"{config.path} ({'found' if config.found else 'not found'}, {_validity(config)})"),
            ("config error", config.error),
            ("companies", _companies_text(status.companies)),
            ("discovered", ", ".join(status.companies.discovered or []) or None),
            ("catalog", status.catalog_status),
        ]
    )
    parts = [header]
    if status.backends:
        rows = [
            [b.name, b.type, b.transport or "", b.state, f"{b.driver.name} ({_driver_state(b.driver)})", b.target]
            for b in status.backends
        ]
        parts.append(text_table(["backend", "type", "transport", "state", "driver", "target"], rows))
    if status.notes:
        parts.append("\n".join(f"- {note}" for note in status.notes))
    return "\n\n".join(parts)


def _process_text(process: ProcessInfo) -> str:
    memory = f"{process.memory_mb:.0f} MB, " if process.memory_mb is not None else ""
    return f"pid {process.pid}, {memory}{process.threads} threads, up {process.uptime_s} s"


def _validity(config: ConfigInfo) -> str:
    return "valid" if config.valid else "INVALID"


def _driver_state(driver: DriverStatus) -> str:
    if driver.loadable is None:
        return "not checked"
    if driver.loadable:
        return "ok"
    return "wrong bitness" if driver.registered else "missing"


def _companies_text(companies: CompaniesStatus) -> str:
    allow = ", ".join(companies.allow) if companies.allow is not None else "all discovered"
    hide = ", ".join(companies.hide) or "none"
    return f"default {companies.default or 'not set'}; allow {allow}; hide {hide}"
