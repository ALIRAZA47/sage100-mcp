"""The ProvideX executor (SPEC.md §4, §8.1, §13.2, §14.2) against the driver emulator."""

from __future__ import annotations

import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sage100_mcp.capabilities import cache_path, load
from sage100_mcp.errors import ErrorCode, SageError
from tests import emulator
from tests.emulator.conftest import Installer, Maker
from tests.premium_fixture import schema
from tests.providex_fixture import custom, profile

UNC = r"\\SAGESRV\Sage\Sage 100\MAS90"


# --- connection strings (§4.3) --------------------------------------------------------------------------


def test_the_connection_string_is_the_minimal_one(make_install: Installer, make_executor: Maker) -> None:
    """Byte for byte the example in SPEC.md §4.3, built from the directory alone."""
    install = make_install()
    install.directory = UNC
    executor = make_executor(install, password="***")
    assert executor.connection_string("ABC") == (
        r"Driver={MAS 90 4.0 ODBC Driver};UID=mcp_reader;PWD=***;Company=ABC;Directory=\\SAGESRV\Sage\Sage 100\MAS90;"
        r"Prefix=\\SAGESRV\Sage\Sage 100\MAS90\SY\, \\SAGESRV\Sage\Sage 100\MAS90\==\;"
        r"ViewDLL=\\SAGESRV\Sage\Sage 100\MAS90\Home;Silent=1;SERVER=NotTheServer;StripTrailingSpaces=1;"
        "DirtyReads=0;BurstMode=0;CacheSize=4"
    )


def test_client_server_adds_the_remote_keywords(make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install(), settings={"transport": "cs", "cs": {"host": "SAGESRV", "port": 20333}})
    assert executor.connection_string("ABC").endswith(";RemotePVKIOHost=SAGESRV;RemotePVKIOPort=20333")


def test_explicit_prefix_and_viewdll_win(make_install: Installer, make_executor: Maker) -> None:
    prefix = "D:\\Sage\\SY\\, D:\\Sage\\==\\"
    executor = make_executor(make_install(), settings={"prefix": prefix, "viewdll": "D:\\Home"})
    assert f";Prefix={prefix};ViewDLL=D:\\Home;" in executor.connection_string("ABC")


def test_secrets_with_syntax_characters_are_braced(make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install(), password="pa;ss}word")
    assert ";PWD={pa;ss}}word};" in executor.connection_string("ABC")


@pytest.mark.parametrize(
    ("hour", "dirty"),
    [(12, "DirtyReads=0;BurstMode=0"), (22, "DirtyReads=1;BurstMode=1"), (3, "DirtyReads=1;BurstMode=1")],
)
def test_the_after_hours_profile(make_install: Installer, make_executor: Maker, hour: int, dirty: str) -> None:
    executor = make_executor(
        make_install(),
        settings={"performance_profile": {"after_hours": "19:00-06:00"}},
        clock=lambda: datetime(2026, 10, 29, hour, 30),
    )
    assert dirty in executor.connection_string("ABC")


def test_optional_keywords_need_the_probe(make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install("all_pass"))
    assert "ReadOnly" not in executor.connection_string("EMP")  # not probed yet
    executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert executor.connection_string("EMP").endswith(";ReadOnly=1")
    assert "EnforceNullDate" not in executor.connection_string("EMP")  # sent only on the retry (decisions D24)


# --- the probe runs once per driver version (§5.3) ---------------------------------------------------------


def test_the_first_connect_probes_and_caches(
    make_install: Installer, make_executor: Maker, isolated_home: Path
) -> None:
    install = make_install("calvin_graham")
    executor = make_executor(install)
    before = executor.capabilities
    executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert executor.driver_version == "4.40.0004"
    assert (before.source, executor.capabilities.source) == ("baseline", "probe")
    cached = load(cache_path(isolated_home, "4.40.0004"))
    assert cached == executor.capabilities

    sent = len(install.statements)
    again = make_executor(install)
    again.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert len(install.statements) == sent + 1  # the cache answered; no probe ran
    assert again.capabilities == cached


def test_a_new_driver_version_is_probed_again(make_install: Installer, make_executor: Maker) -> None:
    install = make_install("calvin_graham")
    make_executor(install).query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    install.driver_version, install.profile = "4.50.0001", profile("pxplus_full")
    executor = make_executor(install)
    executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert executor.capabilities.driver_version == "4.50.0001"
    assert executor.capabilities.supports("or")


# --- companies and catalog (§7.1, §8.1) ---------------------------------------------------------------


def test_companies_come_from_sy_company(make_install: Installer, make_executor: Maker) -> None:
    found = make_executor(make_install()).discover()
    assert [(c.code, c.database, c.name) for c in found] == [
        ("ABC", "ABC", "ABC Demo Distribution"),
        ("EMP", "EMP", "Example Emporium Inc."),
        ("PCC", "PCC", "Placeholder Capital Co."),
    ]


def test_companies_fall_back_to_the_mas_folders(make_install: Installer, make_executor: Maker) -> None:
    found = make_executor(make_install(system_tables_visible=False)).discover()
    assert [(c.code, c.name) for c in found] == [("ABC", None), ("EMP", None), ("PCC", None)]


def test_without_a_default_company_the_folders_give_one_to_connect_with(
    make_install: Installer, make_executor: Maker
) -> None:
    found = make_executor(make_install(), companies={}).discover()
    assert [c.code for c in found] == ["ABC", "EMP", "PCC"]


def test_discovery_needs_some_company(make_install: Installer, make_executor: Maker, tmp_path: Path) -> None:
    install = make_install()
    install.directory = str(tmp_path / "elsewhere")  # no MAS_ folders there
    emulator.INSTALLS[install.directory.lower()] = install
    with pytest.raises(SageError) as caught:
        make_executor(install, companies={}).discover()
    assert caught.value.code == ErrorCode.COMPANY_REQUIRED


def test_tables_and_columns_come_from_the_catalog_functions(make_install: Installer, make_executor: Maker) -> None:
    tables = {t.name: t for t in make_executor(make_install("pxplus_full")).read_tables("EMP")}
    assert len(tables) == len(schema("MAS_EMP")["tables"])  # SY_Company is a system table, not a company one
    invoice = tables["AR_OpenInvoice"]
    types = {c.name: c.type for c in invoice.columns}
    assert types["Balance"] == "decimal(13,2)"
    assert types["InvoiceDate"] == "date"
    assert types["CustomerNo"] == "varchar(20)"
    assert invoice.key == ("ARDivisionNo", "CustomerNo", "InvoiceNo", "InvoiceType")  # SQLPrimaryKeys
    assert invoice.row_hint is None  # a count on ProvideX is a full scan (§7.1)


def test_keys_are_left_to_the_overlay_without_primary_keys(make_install: Installer, make_executor: Maker) -> None:
    tables = {t.name: t for t in make_executor(make_install("calvin_graham")).read_tables("EMP")}
    assert tables["AR_OpenInvoice"].key == ()


def test_module_options_are_read_for_the_profile(make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install())
    options = executor.read_options("EMP")
    assert options["AR_Options"] is not None
    assert options["AR_Options"]["AgingCategory1"] == Decimal(1)  # MAS_EMP's categories start at 1, 16, 31, 61
    assert options["AR_Options"]["CurrentFiscalYr"] == "2024"
    assert {name for name, row in executor.read_options("PCC").items() if row is not None} == {
        "AP_Options",
        "GL_Options",
        "CI_Options",
    }


# --- queries (§13.2) ----------------------------------------------------------------------------------


def test_rows_types_and_truncation(make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install())
    result = executor.query(
        "EMP", "SELECT InvoiceNo, InvoiceDate, Balance FROM AR_OpenInvoice WHERE CustomerNo = ?", ["C0001"],
        max_rows=3, timeout_s=10,
    )  # fmt: skip
    assert result.truncated
    assert len(result.rows) == 3
    assert isinstance(result.rows[0][1], date)
    assert isinstance(result.rows[0][2], Decimal)
    assert [(c.name, c.type) for c in result.columns][2] == ("Balance", "decimal(13,2)")


def test_rows_can_be_skipped_while_fetching(make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install())
    every = executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=100, timeout_s=10).rows
    page = executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=5, timeout_s=10, skip=10).rows
    assert page == every[10:15]


def test_a_watchdog_cancels_where_the_driver_has_no_timeout(make_install: Installer, make_executor: Maker) -> None:
    install = make_install("calvin_graham", slow_tables={"AR_Customer": 5.0})
    executor = make_executor(install)
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)  # probe first
    started = time.monotonic()
    with pytest.raises(SageError) as caught:
        executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=1)
    assert caught.value.code == ErrorCode.QUERY_TIMEOUT
    assert time.monotonic() - started < 3


def test_the_drivers_own_timeout_is_used_when_it_works(make_install: Installer, make_executor: Maker) -> None:
    install = make_install("pxplus_full", slow_tables={"AR_Customer": 5.0})
    executor = make_executor(install)
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    with pytest.raises(SageError) as caught:
        executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=1)
    assert caught.value.code == ErrorCode.QUERY_TIMEOUT
    assert "Timeout expired" in (caught.value.detail or "")


def test_a_stuck_call_is_abandoned_and_its_slot_freed(make_install: Installer, make_executor: Maker) -> None:
    install = make_install("calvin_graham", slow_tables={"AR_Customer": 4.0}, honors_cancel=False)
    executor = make_executor(install, limits={"per_company_concurrency": {"providex": 1}}, cancel_grace_s=0.2)
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    started = time.monotonic()
    with pytest.raises(SageError) as caught:
        executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=1)
    assert caught.value.code == ErrorCode.QUERY_TIMEOUT
    assert time.monotonic() - started < 2.5
    # The only slot was handed back, so the next query runs while the stuck call is still in the driver.
    assert executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=2).rows


def test_bad_dates_are_retried_with_null_date_on(make_install: Installer, make_executor: Maker) -> None:
    install = make_install("calvin_graham", bad_dates={("EMP", "AR_OpenInvoice")})
    executor = make_executor(install)
    result = executor.query("EMP", "SELECT InvoiceNo FROM AR_OpenInvoice", max_rows=5, timeout_s=10)
    assert result.null_dates_coerced
    assert result.rows
    clean = executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=5, timeout_s=10)
    assert not clean.null_dates_coerced


def test_bad_dates_without_the_null_date_keyword(make_install: Installer, make_executor: Maker) -> None:
    caps = profile("calvin_graham")
    install = make_install(caps.model_copy(update={"keywords": {}}), bad_dates={("EMP", "AR_OpenInvoice")})
    with pytest.raises(SageError) as caught:
        make_executor(install).query("EMP", "SELECT InvoiceNo FROM AR_OpenInvoice", max_rows=5, timeout_s=10)
    assert caught.value.code == ErrorCode.DRIVER_INTERNAL
    assert "date" in (caught.value.hint or "")


def test_client_server_failures_are_retried_once(make_install: Installer, make_executor: Maker) -> None:
    install = make_install(cs_available=False)
    executor = make_executor(install, settings={"transport": "cs", "cs": {"host": "SAGESRV"}}, cs_retry_delay_s=0.05)
    with pytest.raises(SageError) as caught:
        executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert caught.value.code == ErrorCode.CS_ODBC_UNREACHABLE
    assert "SAGESRV:20222" in (caught.value.detail or "")
    assert executor.connect_attempts == 2


def test_a_wrong_password_is_auth_failed_and_never_echoed(make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install(), password="Wr0ng;pass")
    with pytest.raises(SageError) as caught:
        executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert caught.value.code == ErrorCode.AUTH_FAILED
    assert "Wr0ng" not in str(caught.value.envelope())


def test_an_unknown_company(make_install: Installer, make_executor: Maker) -> None:
    with pytest.raises(SageError) as caught:
        make_executor(make_install()).query("ZZZ", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert caught.value.code == ErrorCode.COMPANY_NOT_FOUND


def test_a_missing_driver(make_install: Installer, make_executor: Maker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emulator, "DRIVER", "Some Other Driver")
    with pytest.raises(SageError) as caught:
        make_executor(make_install()).query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert caught.value.code == ErrorCode.DRIVER_NOT_INSTALLED


def test_a_company_gets_at_most_two_connections(make_install: Installer, make_executor: Maker) -> None:
    install = make_install("calvin_graham", slow_tables={"AR_Customer": 0.3})
    executor = make_executor(install)
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    before = install.connections
    threads = [
        threading.Thread(
            target=executor.query,
            args=("EMP", "SELECT CustomerNo FROM AR_Customer"),
            kwargs={"max_rows": 1, "timeout_s": 10},
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert install.connections - before <= 1  # one was pooled already; the second opens once
    assert install.peak_open <= 2


def test_ping_opens_a_fresh_connection(make_install: Installer, make_executor: Maker) -> None:
    install = make_install()
    executor = make_executor(install)
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    before = install.connections
    connect_ms, query_ms = executor.ping("EMP", executor.health_query(("AR", "AP")), 10)
    assert install.connections == before + 1
    assert connect_ms >= 0
    assert query_ms >= 0
    assert install.statements[-1] == "SELECT ModuleCode FROM AR_Options"


def test_close_drops_pooled_connections(make_install: Installer, make_executor: Maker) -> None:
    install = make_install()
    executor = make_executor(install)
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    executor.close()
    assert install.open_connections == 0


def test_refreshing_capabilities_reprobes(make_install: Installer, make_executor: Maker) -> None:
    install = make_install("calvin_graham")
    executor = make_executor(install)
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    install.profile = custom("calvin_graham", order_by_column=True)  # a patched driver, same version
    refreshed = executor.refresh_capabilities("EMP")
    assert refreshed.supports("order_by_column")
    assert executor.capabilities == refreshed
