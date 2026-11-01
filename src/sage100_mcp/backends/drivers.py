"""Which ODBC drivers this host registers, and whether this process can load them (SPEC.md §4.1).

A process loads only drivers of its own bitness. Windows keeps 64-bit registrations under
``HKLM\\SOFTWARE\\ODBC\\ODBCINST.INI`` and 32-bit ones in that key's WOW6432Node view. Reading both
lets ``doctor`` and ``sage_status`` explain an IM014 (architecture mismatch) before it happens.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Bitness = Literal[32, 64]
PROVIDEX_DRIVER = "MAS 90 4.0 ODBC Driver"

_ODBCINST = r"SOFTWARE\ODBC\ODBCINST.INI"

RegistryView = Callable[[Bitness], dict[str, dict[str, str]]]
"""Returns ``{driver name: {value name: data}}`` for one registry view, ``{}`` if the key is missing."""


@dataclass(frozen=True)
class DriverRegistration:
    name: str
    bitness: Bitness
    dll: str | None
    odbc_version: str | None  # DriverODBCVer (e.g. 03.00), not the build; SQL_DRIVER_VER needs a connection


@dataclass(frozen=True)
class DriverCheck:
    name: str
    registered: bool | None  # None: this platform has no registry to read
    loadable: bool | None  # registered for this process's bitness
    registered_bitness: tuple[Bitness, ...]
    odbc_version: str | None
    message: str
    helper: str | None = None  # the 32-bit helper that loads a 32-bit-only driver for this process (D67)


def process_bitness() -> Bitness:
    return 64 if sys.maxsize > 2**32 else 32


def read_windows_registry(bitness: Bitness) -> dict[str, dict[str, str]]:
    if sys.platform != "win32":  # also tells mypy the winreg code below is Windows-only
        return {}
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_WOW64_64KEY if bitness == 64 else winreg.KEY_WOW64_32KEY)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ODBCINST + r"\ODBC Drivers", 0, access) as key:
            names = [winreg.EnumValue(key, i)[0] for i in range(winreg.QueryInfoKey(key)[1])]
    except OSError:
        return {}
    drivers: dict[str, dict[str, str]] = {}
    for name in names:
        values: dict[str, str] = {}
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{_ODBCINST}\\{name}", 0, access) as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    value_name, data, _ = winreg.EnumValue(key, i)
                    values[value_name] = str(data)
        except OSError:
            pass
        drivers[name] = values
    return drivers


def registered_drivers(read: RegistryView | None = None) -> list[DriverRegistration] | None:
    """Drivers from both registry views, or None where there is no registry (not Windows)."""
    if read is None:
        if sys.platform != "win32":
            return None
        read = read_windows_registry
    bitnesses: tuple[Bitness, ...] = (64, 32)
    return [
        DriverRegistration(name, bitness, values.get("Driver"), values.get("DriverODBCVer"))
        for bitness in bitnesses
        for name, values in sorted(read(bitness).items())
    ]


def check_driver(
    name: str, drivers: list[DriverRegistration] | None, *, remedy: str, process: Bitness | None = None
) -> DriverCheck:
    """Whether ``name`` is registered for this process's bitness; ``remedy`` says how to install it."""
    process = process or process_bitness()
    if drivers is None:
        return DriverCheck(
            name,
            None,
            None,
            (),
            None,
            "not checked: driver detection reads the Windows registry, and this host is not Windows",
        )
    matches = [d for d in drivers if d.name.lower() == name.lower()]
    found = tuple(sorted({d.bitness for d in matches}))
    own = next((d for d in matches if d.bitness == process), None)
    if own is not None:
        message = f"registered for {process}-bit, matching this process"
        return DriverCheck(name, True, True, found, own.odbc_version, message)
    if matches:
        other = found[0]
        message = (
            f"registered only for {other}-bit, but this process is {process}-bit and cannot load it (IM014): "
            f"run a {other}-bit build of sage100-mcp, or install the {process}-bit driver"
        )
        return DriverCheck(name, True, False, found, matches[0].odbc_version, message)
    return DriverCheck(name, False, False, (), None, f"not registered: {remedy}")
