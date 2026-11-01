"""Server runtime: backends, companies, catalogs and the active company (SPEC.md §7, §8, §10).

Discovery and catalog builds run on a background thread so the server answers its client at once;
tools wait a little for a catalog that is being built, then report CATALOG_BUILDING.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sage100_mcp import logs, principals
from sage100_mcp.backends.base import Executor
from sage100_mcp.catalog.builder import CatalogStore, LiveLayer, assemble
from sage100_mcp.catalog.model import Catalog
from sage100_mcp.catalog.overlay import Overlay, load_overlay
from sage100_mcp.companies import Company, Profile, build_profile, resolve_company
from sage100_mcp.config import ConfigError, LoadedConfig, ProvideXBackend, SqlServerBackend, default_home
from sage100_mcp.credentials import resolve_credential
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.http.cursors import Cursors
from sage100_mcp.policy import Policy, load_policy

logger = logging.getLogger(__name__)

CatalogStatus = Literal["not_built", "building", "ready", "stale", "error"]
DISCOVERY_WAIT_S = 30.0
CATALOG_WAIT_S = 20.0
PROFILE_TTL_S = 3600.0  # §13.4: a company profile is re-read after an hour
RETRY_AFTER_S = 30.0  # a catalog that failed to build is tried again by the next call after this


@dataclass
class CatalogState:
    status: CatalogStatus = "not_built"
    step: str = ""
    catalog: Catalog | None = None
    profile: Profile | None = None
    error: SageError | None = None  # why the last build failed (a failed refresh keeps the old catalog)
    source: Literal["cache", "live"] | None = None
    elapsed_ms: int | None = None
    profiled_at: float = 0.0  # time.monotonic() of the last profile read
    failed_at: float = 0.0
    busy: bool = False  # a build is running


@dataclass
class BackendState:
    name: str
    kind: str
    executor: Executor | None = None
    error: SageError | None = None
    discovered: bool = False


ExecutorFactory = Callable[[str, SqlServerBackend | ProvideXBackend, LoadedConfig, Path], Executor]


def default_executor_factory(
    name: str, settings: SqlServerBackend | ProvideXBackend, loaded: LoadedConfig, home: Path
) -> Executor:
    credential = resolve_credential(settings.credential)
    limits = loaded.config.limits
    if isinstance(settings, ProvideXBackend):
        from sage100_mcp.backends.providex import ProvideXExecutor

        return ProvideXExecutor(name, settings, credential, limits, home=home, companies=loaded.config.companies)
    from sage100_mcp.backends.sqlserver import SqlServerExecutor

    return SqlServerExecutor(name, settings, credential, limits)


class Runtime:
    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        home: Path | None = None,
        executor_factory: ExecutorFactory = default_executor_factory,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.home = home or default_home()
        self.store = CatalogStore(self.home)
        self.errors: list[str] = [loaded.error] if loaded.error else []
        self.policy: Policy | None = None
        self.overlay: Overlay | None = None
        try:
            self.policy = load_policy(self.config.policy_file)
            self.overlay = load_overlay()
        except ConfigError as exc:  # fail closed: without a policy nothing is served
            self.errors.append(str(exc))
        self._factory = executor_factory
        self.backends: dict[str, BackendState] = {
            name: BackendState(name, settings.type) for name, settings in self.config.backends.items()
        }
        self.companies: dict[str, Company] = {}  # every discovered company; callers see visible_companies()
        self.catalogs: dict[str, CatalogState] = {}
        self.principals = principals.Principals(self.config.limits.rate_per_min)
        self.cursors = Cursors(self.home / "tmp")  # run_query page snapshots (§13.3)
        self._discovered = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    # --- lifecycle ---------------------------------------------------------------------------

    @property
    def serving(self) -> bool:
        """False when config or policy failed to load: then no backend is touched."""
        return not self.errors and self.policy is not None and self.overlay is not None

    @property
    def discovery_done(self) -> bool:
        return self._discovered.is_set()

    def start(self, *, background: bool = True) -> None:
        if background:
            self._thread = threading.Thread(target=self._discover_and_build, name="sage100-catalog", daemon=True)
            self._thread.start()
        else:
            self._discover_and_build()

    def close(self) -> None:
        self.cursors.close()
        for state in self.backends.values():
            if state.executor is not None:
                state.executor.close()

    def discover(self) -> None:
        """Connect each backend and list its companies; on failure the backend's error is kept."""
        try:
            if self.serving:
                self._discover()
        finally:
            self._discovered.set()

    def _discover_and_build(self) -> None:
        self.discover()
        for code in list(self.companies):
            if self.catalogs[code].catalog is None:  # a tool call may have built it already
                self.build(code)

    def _discover(self) -> None:
        companies = self.config.companies
        found: dict[str, Company] = {}
        for name, settings in self.config.backends.items():
            state = self.backends[name]
            try:
                state.executor = self._factory(name, settings, self.loaded, self.home)
                discovered = state.executor.discover()
                state.discovered = True
            except SageError as exc:
                state.error = exc
                logger.warning("Backend %s unavailable: %s %s", name, exc.code, exc.message)
                continue
            for company in discovered:
                if company.code in companies.hide or (
                    companies.allow is not None and company.code not in companies.allow
                ):
                    continue
                if companies.backend_map.get(company.code, name) != name:
                    continue
                if company.code in found:
                    logger.warning("Company %s is on two backends; using %s", company.code, found[company.code].backend)
                    continue
                found[company.code] = Company(company.code, company.name, name, company.database)
        with self._lock:
            self.companies = dict(sorted(found.items()))
            for code in self.companies:
                self.catalogs.setdefault(code, CatalogState())

    def wait_for_discovery(self, timeout_s: float = DISCOVERY_WAIT_S) -> None:
        if not self._discovered.wait(timeout_s):
            raise SageError(
                ErrorCode.CATALOG_BUILDING, "The server is still finding Sage companies.", hint="Try again shortly."
            )

    # --- companies ---------------------------------------------------------------------------

    @property
    def active_company(self) -> str | None:
        """The calling principal's active company (§10.3): per process over stdio, per token over HTTP."""
        return self.principals.active(principals.current())

    @active_company.setter
    def active_company(self, code: str | None) -> None:
        self.principals.set_active(principals.current(), code)

    def visible_companies(self) -> dict[str, Company]:
        """The discovered companies the calling principal may use; a scoped token sees no others."""
        caller = principals.current()
        return {code: company for code, company in self.companies.items() if caller.may_use(code)}

    def resolve_company(self, requested: str | None) -> Company:
        self.wait_for_discovery()
        default = self.config.companies.default
        visible = self.visible_companies()
        return resolve_company(
            requested,
            active=self.active_company,
            default=default if default in visible else None,
            available=visible,
        )

    def set_active(self, requested: str) -> Company:
        company = self.resolve_company(requested)
        self.active_company = company.code
        return company

    def require_policy(self) -> Policy:
        if self.policy is None:
            raise SageError(ErrorCode.INTERNAL, "The policy did not load, so nothing is served; see sage_status.")
        return self.policy

    def executor(self, company: Company) -> Executor:
        state = self.backends[company.backend]
        if state.executor is None:
            raise state.error or SageError(ErrorCode.INTERNAL, f"Backend {company.backend} is not connected.")
        return state.executor

    # --- catalogs ----------------------------------------------------------------------------

    def build(self, code: str, *, force: bool = False) -> CatalogState:
        """Build one company's catalog in this thread, unless a build is already running."""
        state = self._claim(code)
        if state is not None:
            self._run(code, state, force=force)
        return self.catalogs[code]

    def refresh(self, code: str, *, force: bool = True) -> CatalogState:
        """Rebuild in the background (refresh_catalog); the previous catalog keeps answering meanwhile."""
        state = self._claim(code)
        if state is not None:
            threading.Thread(target=self._run, args=(code, state), kwargs={"force": force}, daemon=True).start()
        return self.catalogs[code]

    def _claim(self, code: str) -> CatalogState | None:
        with self._lock:
            state = self.catalogs.setdefault(code, CatalogState())
            if state.busy:
                return None
            state.busy, state.step = True, "reading module options"
            if state.catalog is None:
                state.status = "building"
            return state

    def _run(self, code: str, state: CatalogState, *, force: bool) -> None:
        """Read the profile, then load the cached live layer or rebuild it, then apply overlay and policy."""
        company = self.companies[code]
        started = time.perf_counter()
        try:
            if self.overlay is None or self.policy is None:
                raise SageError(ErrorCode.INTERNAL, "The overlay or policy did not load; see sage_status.")
            executor = self.executor(company)
            profile = self._profile(executor, company)
            cached = None if force else self.store.load(code)
            source: Literal["cache", "live"] = "cache"
            if (
                cached is not None
                and cached.data_versions == profile.data_versions
                and cached.backend == company.backend
            ):
                live = cached
            else:
                state.step = "reading table and column metadata"
                tables = executor.read_tables(company.database)
                live = LiveLayer(code, company.backend, profile.data_versions, _now(), tables)
                self.store.save(live)
                source = "live"
            catalog = assemble(live, self.overlay, self.policy)
            with self._lock:
                state.catalog, state.profile, state.source, state.error = catalog, profile, source, None
                state.status, state.profiled_at = "ready", time.monotonic()
                state.elapsed_ms = int((time.perf_counter() - started) * 1000)
        except SageError as exc:
            logger.warning("Catalog for %s failed: %s %s", code, exc.code, exc.message)
            self._failed(state, exc)
        except Exception:
            logger.error("Catalog for %s failed\n%s", code, logs.failure_text())
            self._failed(
                state, SageError(ErrorCode.INTERNAL, "Building the catalog failed; the server log has details.")
            )
        finally:
            with self._lock:
                state.busy, state.step = False, ""

    def _failed(self, state: CatalogState, error: SageError) -> None:
        """A failed first build is an error; a failed rebuild keeps the previous catalog and reports why."""
        with self._lock:
            state.error, state.failed_at = error, time.monotonic()
            if state.catalog is None:
                state.status = "error"

    def _profile(self, executor: Executor, company: Company) -> Profile:
        rows = executor.read_options(company.database)
        return build_profile(company.code, company.name, company.backend, company.database, rows)

    def status_of(self, code: str) -> CatalogStatus:
        state = self.catalogs.get(code)
        if state is None:
            return "not_built"
        if state.busy:
            return "building"
        if state.status == "ready" and time.monotonic() - state.profiled_at > PROFILE_TTL_S:
            return "stale"
        return state.status

    def catalog(self, company: Company, wait_s: float = CATALOG_WAIT_S) -> tuple[Catalog, Profile]:
        """The company's catalog and profile; builds it now, or waits up to ``wait_s`` for a running build."""
        state = self.catalogs.setdefault(company.code, CatalogState())
        deadline = time.monotonic() + wait_s
        while state.catalog is None and time.monotonic() < deadline:
            if state.busy:
                time.sleep(0.05)
            elif state.status == "error" and time.monotonic() - state.failed_at < RETRY_AFTER_S:
                break
            else:  # not built yet, or it failed a while ago: try now, in this call
                self.build(company.code)
        if state.catalog is not None and state.profile is not None:
            if not state.busy and time.monotonic() - state.profiled_at > PROFILE_TTL_S:
                self.refresh(company.code, force=False)  # re-read the profile; serve this catalog meanwhile
            return state.catalog, state.profile
        if state.status == "error" and state.error is not None:
            raise state.error
        raise SageError(
            ErrorCode.CATALOG_BUILDING,
            f"The catalog for {company.code} is still being built ({state.step or 'starting'}).",
            hint="Discovery tools still answer; try again in a few seconds.",
            company=company.code,
        )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
