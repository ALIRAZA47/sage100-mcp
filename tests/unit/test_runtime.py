"""The runtime (SPEC.md §7.1, §8, §13.4): discovery, company resolution, catalog cache and rebuilds."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from sage100_mcp import runtime as runtime_module
from sage100_mcp.catalog.builder import RawTable
from sage100_mcp.companies import Company, resolve_company
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.runtime import Runtime
from tests.fake_backend import FakeExecutor, fake_config, fake_runtime

AVAILABLE = {code: Company(code, None, "premium", f"MAS_{code}") for code in ("ABC", "EMP")}


# --- company resolution (§8.3) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "active", "default", "expected"),
    [
        ("emp", None, None, "EMP"),  # the call's company wins, normalised
        ("ABC", "EMP", "EMP", "ABC"),
        (None, "EMP", "ABC", "EMP"),  # then the active company
        (None, None, "ABC", "ABC"),  # then the default
    ],
)
def test_resolution_order(requested: str | None, active: str | None, default: str | None, expected: str) -> None:
    assert resolve_company(requested, active=active, default=default, available=AVAILABLE).code == expected


def test_the_only_company_needs_no_name() -> None:
    only = {"EMP": AVAILABLE["EMP"]}
    assert resolve_company(None, active=None, default=None, available=only).code == "EMP"


def test_several_companies_and_no_choice_never_guesses() -> None:
    with pytest.raises(SageError) as raised:
        resolve_company(None, active=None, default=None, available=AVAILABLE)
    assert raised.value.code == ErrorCode.COMPANY_REQUIRED
    assert raised.value.choices == ["ABC", "EMP"]


@pytest.mark.parametrize(("requested", "fragment"), [("XYZ", "XYZ is not available"), ("MAS_EMP", "use the code")])
def test_unknown_or_malformed_company(requested: str, fragment: str) -> None:
    with pytest.raises(SageError) as raised:
        resolve_company(requested, active=None, default=None, available=AVAILABLE)
    assert raised.value.code == ErrorCode.COMPANY_NOT_FOUND
    assert fragment in raised.value.message
    assert raised.value.choices == ["ABC", "EMP"]


def test_no_company_at_all() -> None:
    with pytest.raises(SageError) as raised:
        resolve_company(None, active=None, default=None, available={})
    assert raised.value.code == ErrorCode.COMPANY_REQUIRED
    assert raised.value.choices is None


# --- discovery ------------------------------------------------------------------------------------


def test_discovery_applies_allow_and_hide(tmp_path: Path) -> None:
    fake = FakeExecutor(companies={"ABC": "Demo", "EMP": "Emporium", "PCC": "Placeholder"})
    runtime, _ = fake_runtime(tmp_path, fake, companies={"hide": ["ABC"]})
    assert list(runtime.companies) == ["EMP", "PCC"]
    runtime, _ = fake_runtime(tmp_path, fake, companies={"allow": ["PCC"]})
    assert list(runtime.companies) == ["PCC"]
    assert runtime.resolve_company(None).code == "PCC"  # the only one exposed


def test_backend_map_picks_the_backend_for_a_company_on_two(tmp_path: Path) -> None:
    runtime, _ = fake_runtime(tmp_path, backends=("premium", "mirror"), companies={"backend_map": {"EMP": "mirror"}})
    assert {code: company.backend for code, company in runtime.companies.items()} == {"EMP": "mirror", "PCC": "premium"}


def test_an_unreachable_backend_is_reported_not_fatal(tmp_path: Path) -> None:
    fake = FakeExecutor(fail_with=SageError(ErrorCode.AUTH_FAILED, "No login is stored for sage100-mcp/fake."))
    runtime, _ = fake_runtime(tmp_path, fake)
    assert runtime.companies == {}
    error = runtime.backends["premium"].error
    assert error is not None
    assert error.code == ErrorCode.AUTH_FAILED
    with pytest.raises(SageError) as raised:
        runtime.resolve_company(None)
    assert raised.value.code == ErrorCode.COMPANY_REQUIRED


def test_a_bad_policy_fails_closed(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text("deny_tables: []\ndeny_tables: []\nmask_columns: x\n", encoding="utf-8")  # duplicate key
    loaded = fake_config(policy_file=str(policy))
    calls: list[str] = []

    def factory(*args: object) -> FakeExecutor:
        calls.append("connect")
        return FakeExecutor()

    runtime = Runtime(loaded, home=tmp_path, executor_factory=factory)
    runtime.start(background=False)
    assert not runtime.serving
    assert "duplicate key" in runtime.errors[0]
    assert calls == []  # no backend was touched
    assert runtime.companies == {}


def test_set_active(tmp_path: Path) -> None:
    runtime, _ = fake_runtime(tmp_path)
    with pytest.raises(SageError):
        runtime.resolve_company(None)
    assert runtime.set_active("pcc").code == "PCC"
    assert runtime.resolve_company(None).code == "PCC"
    assert runtime.resolve_company("EMP").code == "EMP"


# --- catalogs ------------------------------------------------------------------------------------------


def test_profile(tmp_path: Path) -> None:
    runtime, _ = fake_runtime(tmp_path)
    _, profile = runtime.catalog(runtime.resolve_company("EMP"))
    assert profile.modules == ("AP", "AR", "CI", "GL", "IM", "PO", "SO")
    assert profile.data_versions["AR"] == "7.3"
    assert profile.current_periods["GL"] == "2024-12"
    assert profile.divisions == {"AP": False, "AR": False}
    assert profile.ar_aging is not None
    assert (profile.ar_aging.basis, profile.ar_aging.starts) == ("due", (30, 60, 90, 120))
    assert profile.ap_aging is None  # no aging categories in the fake's AP_Options


def test_the_cache_is_reused_until_data_version_changes(tmp_path: Path) -> None:
    fake = FakeExecutor()
    runtime, _ = fake_runtime(tmp_path, fake)
    assert fake.reads == 2  # EMP and PCC, read from the database
    assert runtime.catalogs["EMP"].source == "live"

    restarted, _ = fake_runtime(tmp_path, fake)  # same home, same DataVersion: the cache answers
    assert fake.reads == 2
    assert restarted.catalogs["EMP"].source == "cache"

    fake.data_version = "7.40"  # an upgrade invalidates it
    upgraded, _ = fake_runtime(tmp_path, fake)
    assert fake.reads == 4
    assert upgraded.catalogs["EMP"].source == "live"


def test_refresh_keeps_serving_the_old_catalog(tmp_path: Path) -> None:
    fake = FakeExecutor()
    runtime, _ = fake_runtime(tmp_path, fake)
    company = runtime.resolve_company("EMP")
    before, _ = runtime.catalog(company)
    started = threading_gate(fake)
    state = runtime.refresh("EMP")
    assert state.busy
    assert runtime.status_of("EMP") == "building"
    assert runtime.catalog(company)[0] is before  # still answers during the rebuild
    started.release()
    wait_until(lambda: not runtime.catalogs["EMP"].busy)
    assert runtime.status_of("EMP") == "ready"
    assert runtime.catalogs["EMP"].source == "live"
    assert runtime.catalog(company)[0] is not before


def test_a_failed_refresh_keeps_the_old_catalog(tmp_path: Path) -> None:
    fake = FakeExecutor()
    runtime, _ = fake_runtime(tmp_path, fake)
    company = runtime.resolve_company("EMP")
    before, _ = runtime.catalog(company)
    fake.fail_with = SageError(ErrorCode.BACKEND_UNREACHABLE, "Login timeout expired")
    runtime.build("EMP", force=True)
    state = runtime.catalogs["EMP"]
    assert state.status == "ready"
    assert state.error is not None
    assert state.error.code == ErrorCode.BACKEND_UNREACHABLE
    assert runtime.catalog(company)[0] is before


def test_a_failed_first_build_is_retried_later(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeExecutor()
    runtime, _ = fake_runtime(tmp_path, fake, start=False)
    runtime.start(background=False)  # discovery works...
    company = runtime.resolve_company("EMP")
    runtime.catalogs["EMP"].catalog = None
    runtime.catalogs["EMP"].status = "not_built"
    fake.fail_with = SageError(ErrorCode.LOCK_TIMEOUT, "Lock request time out period exceeded.")
    with pytest.raises(SageError) as raised:  # ...but the first catalog build fails
        runtime.catalog(company)
    assert raised.value.code == ErrorCode.LOCK_TIMEOUT
    fake.fail_with = None
    with pytest.raises(SageError):  # within the retry window the error stands
        runtime.catalog(company)
    monkeypatch.setattr(runtime_module, "RETRY_AFTER_S", 0.0)
    catalog, _ = runtime.catalog(company)  # after it, the next call builds again
    assert catalog.table("AR_Customer") is not None


def test_an_old_profile_is_stale_and_reread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeExecutor()
    runtime, _ = fake_runtime(tmp_path, fake)
    runtime.catalogs["EMP"].profiled_at = time.monotonic() - runtime_module.PROFILE_TTL_S - 1
    assert runtime.status_of("EMP") == "stale"
    reads = fake.reads
    runtime.catalog(runtime.resolve_company("EMP"))  # serves, and re-reads the profile in the background
    wait_until(lambda: runtime.status_of("EMP") == "ready")
    assert fake.reads == reads  # the live layer came from the cache


def test_catalog_building_is_reported(tmp_path: Path) -> None:
    fake = FakeExecutor()
    runtime, _ = fake_runtime(tmp_path, fake)
    company = runtime.resolve_company("EMP")
    state = runtime.catalogs["EMP"]
    state.catalog, state.busy, state.status, state.step = None, True, "building", "reading table and column metadata"
    with pytest.raises(SageError) as raised:
        runtime.catalog(company, wait_s=0.05)
    assert raised.value.code == ErrorCode.CATALOG_BUILDING
    assert raised.value.envelope().retryable is True
    assert "reading table and column metadata" in raised.value.message


# --- helpers ----------------------------------------------------------------------------------------------


def wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def threading_gate(fake: FakeExecutor) -> threading.Semaphore:
    """Hold the next read_tables call until release() is called."""
    gate = threading.Semaphore(0)
    original = fake.read_tables

    def held(database: str) -> tuple[RawTable, ...]:
        gate.acquire(timeout=5)
        fake.read_tables = original  # type: ignore[method-assign]
        return original(database)

    fake.read_tables = held  # type: ignore[method-assign]
    return gate
