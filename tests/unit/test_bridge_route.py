"""When a ProvideX backend's driver runs in the 32-bit helper, and how it is started (docs/decisions.md D67)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sage100_mcp import bridge
from sage100_mcp.backends.drivers import PROVIDEX_DRIVER, Bitness, DriverRegistration
from sage100_mcp.bridge import NO_HELPER, SCRIPT, helper_command, providex_check, route
from sage100_mcp.bridge.client import Bridge
from sage100_mcp.config import Config, LoadedConfig, ProvideXBackend
from sage100_mcp.status import PROVIDEX_REMEDY, collect_status

PYTHON32 = r"C:\Python312-32\python.exe"


def backend(**settings: Any) -> ProvideXBackend:
    return ProvideXBackend.model_validate(
        {"type": "providex", "directory": r"\\SAGESRV\Sage\MAS90", "credential": "sage100-mcp/sage", **settings}
    )


def registered(*bitnesses: Bitness) -> list[DriverRegistration]:
    others = [DriverRegistration("SQL Server", 64, "sqlsrv32.dll", "03.50")]
    return others + [DriverRegistration(PROVIDEX_DRIVER, bits, "PVXODBC.DLL", "03.00") for bits in bitnesses]


# --- configuration ------------------------------------------------------------------------------------------


def test_driver_bitness_defaults_to_auto() -> None:
    assert backend().driver_bitness == "auto"
    assert backend().helper is None


@pytest.mark.parametrize(("given", "kept"), [("32", "32"), (32, "32"), ("64", "64"), (64, "64"), ("auto", "auto")])
def test_driver_bitness_is_32_64_or_auto(given: object, kept: str) -> None:
    """YAML scalars arrive as text (D5); a JSON or code caller may give a number."""
    assert backend(driver_bitness=given).driver_bitness == kept


@pytest.mark.parametrize("given", ["31", 16, True, "x86"])
def test_other_bitnesses_are_refused(given: object) -> None:
    with pytest.raises(ValidationError):
        backend(driver_bitness=given)


# --- the route --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bitness", "drivers", "process", "helper"),
    [
        ("auto", registered(64), 64, False),
        ("auto", registered(64, 32), 64, False),  # both builds: the 64-bit one, in process
        ("auto", registered(32), 64, True),  # 32-bit only: the helper
        ("auto", registered(), 64, False),  # none: in process, which reports DRIVER_NOT_INSTALLED
        ("auto", None, 64, False),  # no registry (not Windows): the emulator, or a test's odbc module
        ("32", registered(64, 32), 64, True),
        ("32", None, 64, True),
        ("64", registered(32), 64, False),  # as told: IM014 explains itself
        ("auto", registered(32), 32, False),  # a 32-bit process loads it itself
        ("32", registered(32), 32, False),
    ],
)
def test_the_route(bitness: str, drivers: list[DriverRegistration] | None, process: Bitness, helper: bool) -> None:
    assert route(backend(driver_bitness=bitness), drivers, process).helper is helper


def test_a_helper_route_says_why() -> None:
    assert "32-bit only" in route(backend(), registered(32), 64).reason
    assert route(backend(driver_bitness="32"), registered(64, 32), 64).reason == "driver_bitness is 32"


# --- the helper's command line ----------------------------------------------------------------------------


def test_a_python_runs_the_bundled_script_ignoring_python_variables() -> None:
    assert helper_command(PYTHON32) == (PYTHON32, "-E", str(SCRIPT))
    assert SCRIPT.is_file()


def test_any_other_program_runs_as_it_is() -> None:
    assert helper_command(r"D:\tools\sage100-mcp-odbc32.exe") == (r"D:\tools\sage100-mcp-odbc32.exe",)


def test_the_msi_helper_is_found_beside_the_frozen_program(tmp_path: Path) -> None:
    program = tmp_path / "sage100-mcp.exe"
    assert helper_command(None, executable=str(program), frozen=True) == ()
    bundled = tmp_path / "odbc32" / "sage100-mcp-odbc32.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"MZ")
    assert helper_command(None, executable=str(program), frozen=True) == (str(bundled),)
    assert helper_command(None, executable=str(program), frozen=False) == ()  # a source install names its Python


def test_the_setting_wins_over_the_bundled_helper(tmp_path: Path) -> None:
    (tmp_path / "odbc32").mkdir()
    (tmp_path / "odbc32" / "sage100-mcp-odbc32.exe").write_bytes(b"MZ")
    found = helper_command(PYTHON32, executable=str(tmp_path / "sage100-mcp.exe"), frozen=True)
    assert found[0] == PYTHON32


def test_the_executor_gets_a_bridge_on_the_helper_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "registered_drivers", lambda: registered(32))
    monkeypatch.setattr(bridge, "process_bitness", lambda: 64)
    module = bridge.driver_module(backend(helper=PYTHON32))
    assert isinstance(module, Bridge)
    assert module.command == (PYTHON32, "-E", str(SCRIPT))
    assert module.describe() == f"{PYTHON32} (not running)"


def test_the_executor_gets_pyodbc_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    pyodbc = pytest.importorskip("pyodbc")  # Linux needs unixODBC to import it
    monkeypatch.setattr(bridge, "registered_drivers", lambda: registered(64))
    assert bridge.driver_module(backend()) is pyodbc


# --- what doctor and sage_status say ---------------------------------------------------------------------------


def check(drivers: list[DriverRegistration] | None, **settings: Any) -> Any:
    return providex_check(backend(**settings), drivers, remedy=PROVIDEX_REMEDY, process=64)


def test_a_32_bit_only_driver_is_loadable_through_the_helper() -> None:
    found = check(registered(32), helper=PYTHON32)
    assert (found.registered, found.loadable) == (True, True)
    assert found.message == "registered for 32-bit; this 64-bit server loads the 32-bit build through its helper"
    assert found.helper == f"{PYTHON32} -E {SCRIPT}"


def test_without_a_helper_a_32_bit_only_driver_is_not_loadable() -> None:
    found = check(registered(32))
    assert found.loadable is False
    assert found.message == f"registered for 32-bit only, which a 64-bit process can't load (IM014), and {NO_HELPER}"
    assert found.helper is None


def test_asking_for_32_bit_without_that_build_fails() -> None:
    found = check(registered(64), driver_bitness="32", helper=PYTHON32)
    assert found.loadable is False
    assert "driver_bitness is 32" in found.message


def test_the_in_process_checks_are_unchanged() -> None:
    for drivers in (registered(64), registered(64, 32), registered(), None):
        found = check(drivers)
        assert found.helper is None
    assert check(registered()).message.startswith("not registered: run the Sage 100 Workstation Setup")


def test_sage_status_names_the_helper() -> None:
    data = {"backends": {"sage": {"type": "providex", "directory": r"\\SAGESRV\Sage\MAS90",
                                  "credential": "sage100-mcp/sage", "helper": PYTHON32}}}  # fmt: skip
    loaded = LoadedConfig(config=Config.model_validate(data), path=Path("x.yaml"), found=True)
    status = collect_status(loaded, drivers=registered(32))
    driver = status.backends[0].driver
    assert (driver.loadable, driver.helper) == (True, f"{PYTHON32} -E {SCRIPT}")
    assert not any("IM014" in note for note in status.notes)
