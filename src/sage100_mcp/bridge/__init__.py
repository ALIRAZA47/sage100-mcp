"""Sage's 32-bit ODBC driver for the 64-bit server (docs/decisions.md D67): when the 32-bit helper loads it.

A ProvideX backend's ``driver_bitness`` says which build of Sage's driver to use:
- ``64`` loads it in this process.
- ``32`` loads it through the 32-bit helper (helper.py).
- ``auto``, the default, uses the helper only when Sage's driver is registered for 32-bit alone.

The helper is ``odbc32\\sage100-mcp-odbc32.exe``, which the MSI installs beside sage100-mcp.exe. An install
from PyPI has no frozen helper. There the backend's ``helper`` names a 32-bit python.exe that has pyodbc,
and that Python runs helper.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any

from sage100_mcp.backends.drivers import (
    PROVIDEX_DRIVER,
    Bitness,
    DriverCheck,
    DriverRegistration,
    check_driver,
    process_bitness,
    registered_drivers,
)
from sage100_mcp.config import ProvideXBackend

HELPER_DIR = "odbc32"
HELPER_EXE = "sage100-mcp-odbc32.exe"
SCRIPT = Path(__file__).with_name("helper.py")
NO_HELPER = (
    "this server's 32-bit helper is not installed: reinstall sage100-mcp from its MSI, or set the backend's "
    "helper to a 32-bit python.exe that has pyodbc"
)


@dataclass(frozen=True)
class Route:
    helper: bool  # Sage's driver runs in the 32-bit helper, not in this process
    command: tuple[str, ...]  # how to start the helper: () when none is installed
    reason: str


def helper_command(
    setting: str | None, *, executable: str | None = None, frozen: bool | None = None
) -> tuple[str, ...]:
    """The helper's command line: the program the backend names, else the one the MSI installs."""
    if setting:
        name = PureWindowsPath(setting).name.lower()  # either separator, whatever the platform
        if name.startswith("python"):  # a 32-bit Python with pyodbc runs the script
            return (setting, "-E", str(SCRIPT))  # -E: no PYTHONPATH or PYTHONHOME from the 64-bit side
        return (setting,)
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        bundled = Path(executable or sys.executable).parent / HELPER_DIR / HELPER_EXE
        if bundled.is_file():
            return (str(bundled),)
    return ()


def route(settings: ProvideXBackend, drivers: list[DriverRegistration] | None, process: Bitness | None = None) -> Route:
    """Whether this backend's driver runs in the 32-bit helper, and why."""
    process = process or process_bitness()
    if process == 32 or settings.driver_bitness == "64":
        return Route(False, (), "in this process")
    if settings.driver_bitness == "32":
        return Route(True, helper_command(settings.helper), "driver_bitness is 32")
    registered = {d.bitness for d in drivers or () if d.name.lower() == PROVIDEX_DRIVER.lower()}
    if registered == {32}:
        return Route(True, helper_command(settings.helper), "Sage's driver is registered for 32-bit only")
    return Route(False, (), "in this process")


def driver_module(settings: ProvideXBackend) -> Any:
    """What the ProvideX executor calls pyodbc: pyodbc itself, or the bridge to the 32-bit helper."""
    found = route(settings, registered_drivers())
    if found.helper:
        from sage100_mcp.bridge.client import Bridge

        return Bridge(found.command)
    import pyodbc

    return pyodbc


def providex_check(
    settings: ProvideXBackend,
    drivers: list[DriverRegistration] | None,
    *,
    remedy: str,
    process: Bitness | None = None,
) -> DriverCheck:
    """check_driver for a ProvideX backend, where the 32-bit helper serves a 32-bit-only driver."""
    process = process or process_bitness()
    check = check_driver(PROVIDEX_DRIVER, drivers, remedy=remedy, process=process)
    found = route(settings, drivers, process)
    if not found.helper or not check.registered:
        return check
    if 32 not in check.registered_bitness:
        message = (
            "registered for 64-bit only, but the backend's driver_bitness is 32: "
            "remove driver_bitness, or install Sage's 32-bit driver"
        )
        return replace(check, loadable=False, message=message)
    if not found.command:
        message = f"registered for 32-bit only, which a {process}-bit process can't load (IM014), and {NO_HELPER}"
        return replace(check, loadable=False, message=message)
    builds = " and ".join(f"{bits}-bit" for bits in check.registered_bitness)
    message = f"registered for {builds}; this {process}-bit server loads the 32-bit build through its helper"
    return replace(check, loadable=True, helper=" ".join(found.command), message=message)
