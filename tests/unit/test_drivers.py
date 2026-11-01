from __future__ import annotations

import sys

import pytest

from sage100_mcp.backends.drivers import (
    PROVIDEX_DRIVER,
    Bitness,
    DriverRegistration,
    check_driver,
    registered_drivers,
)

SQL18 = "ODBC Driver 18 for SQL Server"


def registry(**views: dict[str, dict[str, str]]):  # type: ignore[no-untyped-def]
    """A fake registry: registry(x64={...}, x86={...})."""

    def read(bitness: Bitness) -> dict[str, dict[str, str]]:
        return views.get("x64" if bitness == 64 else "x86", {})

    return read


PVX = {"Driver": r"C:\Windows\SysWOW64\PVXODBC.DLL", "DriverODBCVer": "03.00", "APILevel": "1", "SQLLevel": "0"}


def test_both_views_are_read() -> None:
    drivers = registered_drivers(registry(x64={SQL18: {"DriverODBCVer": "03.80"}}, x86={PROVIDEX_DRIVER: PVX}))
    assert drivers == [
        DriverRegistration(SQL18, 64, None, "03.80"),
        DriverRegistration(PROVIDEX_DRIVER, 32, PVX["Driver"], "03.00"),
    ]


def test_no_registry_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    assert registered_drivers() is None
    check = check_driver(PROVIDEX_DRIVER, None, remedy="install it")
    assert (check.registered, check.loadable) == (None, None)
    assert "not checked" in check.message


def test_driver_matching_this_process() -> None:
    drivers = registered_drivers(registry(x64={PROVIDEX_DRIVER: PVX}, x86={PROVIDEX_DRIVER: PVX}))
    check = check_driver(PROVIDEX_DRIVER.upper(), drivers, remedy="install it", process=64)
    assert (check.registered, check.loadable, check.registered_bitness) == (True, True, (32, 64))
    assert check.odbc_version == "03.00"


def test_driver_of_the_other_bitness_explains_im014() -> None:
    """The classic pre-2025 setup: only the 32-bit Sage driver, and 64-bit Python."""
    drivers = registered_drivers(registry(x86={PROVIDEX_DRIVER: PVX}))
    check = check_driver(PROVIDEX_DRIVER, drivers, remedy="install it", process=64)
    assert (check.registered, check.loadable, check.registered_bitness) == (True, False, (32,))
    assert "IM014" in check.message
    assert "32-bit build" in check.message


def test_missing_driver_gives_the_remedy() -> None:
    check = check_driver(PROVIDEX_DRIVER, registered_drivers(registry()), remedy="run Workstation Setup", process=64)
    assert (check.registered, check.loadable) == (False, False)
    assert check.message == "not registered: run Workstation Setup"


@pytest.mark.skipif(sys.platform != "win32", reason="reads the real Windows registry")
def test_real_registry_is_readable() -> None:
    """Runs on the GitHub-hosted Windows runner: exercises the winreg code without needing Sage."""
    drivers = registered_drivers()
    assert drivers, "expected Windows to register at least one ODBC driver"
    assert {d.bitness for d in drivers} <= {32, 64}
