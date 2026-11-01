from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from sage100_mcp.backends.drivers import PROVIDEX_DRIVER, DriverRegistration
from sage100_mcp.config import load_config, load_config_or_defaults
from sage100_mcp.status import collect_status, status_text
from tests.paths import EXAMPLE_CONFIG

SQL18 = "ODBC Driver 18 for SQL Server"


def test_no_config_file(isolated_home: Path) -> None:
    status = collect_status(load_config_or_defaults())
    assert (status.config.found, status.config.valid) == (False, True)
    assert status.backends == []
    assert status.catalog_status == "not_built"
    assert status.server.milestone == "M6"
    assert status.server.protocol_versions[-1] == "2026-07-28"
    assert "2025-11-25" in status.server.protocol_versions
    assert any("No config file" in note for note in status.notes)
    assert any("No backends are configured" in note for note in status.notes)


def test_rejected_config(write_config: Callable[..., Path]) -> None:
    status = collect_status(load_config_or_defaults(write_config("limits: {max_rows: 0}\n"), env={}))
    assert (status.config.found, status.config.valid) == (True, False)
    assert status.config.error is not None
    assert "limits.max_rows" in status.config.error
    assert any("rejected" in note for note in status.notes)
    assert not any("No backends are configured" in note for note in status.notes)
    assert "INVALID" in status_text(status)


def test_example_config_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    status = collect_status(load_config(EXAMPLE_CONFIG, env={}), drivers=None)
    prod, premium = status.backends
    assert (prod.name, prod.state, prod.capabilities, prod.transport) == (
        "sage_prod",
        "unsupported_platform",
        "not_probed",
        "local",
    )
    assert (premium.name, premium.state, premium.capabilities, premium.transport) == (
        "sage_premium",
        "not_connected",
        "not_applicable",
        None,
    )
    assert prod.driver.registered is None
    assert any("Windows DLL" in note for note in status.notes)
    assert status.companies.model_dump() == {
        "default": "ABC",
        "allow": ["ABC", "EMP"],
        "hide": ["DEF"],
        "discovered": None,
        "active": None,
    }


def test_driver_of_the_wrong_bitness_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sage100_mcp.backends.drivers.process_bitness", lambda: 64)
    drivers = [DriverRegistration(PROVIDEX_DRIVER, 32, None, "03.00"), DriverRegistration(SQL18, 64, None, "03.80")]
    status = collect_status(load_config(EXAMPLE_CONFIG, env={}), drivers=drivers)
    prod, premium = status.backends
    assert prod.state == "not_connected"
    assert (prod.driver.registered, prod.driver.loadable, prod.driver.registered_bitness) == (True, False, [32])
    assert (premium.driver.loadable, premium.driver.odbc_version) == (True, "03.80")
    assert any("IM014" in note for note in status.notes)
    text = status_text(status)
    assert "MAS 90 4.0 ODBC Driver (wrong bitness)" in text
    assert "ODBC Driver 18 for SQL Server (ok)" in text


def test_text_has_a_backend_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    text = status_text(collect_status(load_config(EXAMPLE_CONFIG, env={}), drivers=None))
    assert "| backend | type | transport | state | driver | target |" in text
    assert "| sage_prod | providex | local | unsupported_platform | MAS 90 4.0 ODBC Driver (not checked) |" in text
    assert "companies: default ABC; allow ABC, EMP; hide DEF" in text


def test_process_memory_and_pools_are_reported(tmp_path: Path) -> None:
    """§13.5, §15.7: what a soak run samples, and an operator reads when calls queue."""
    import os
    import sys

    from tests.fake_backend import fake_runtime

    runtime, _ = fake_runtime(tmp_path)
    status = collect_status(runtime.loaded, runtime=runtime)
    assert (status.process.pid, status.process.threads >= 1) == (os.getpid(), True)
    if sys.platform.startswith("linux"):
        assert status.process.memory_mb is not None
        assert status.process.memory_mb > 10
    assert [(pool.name, pool.cap, pool.in_use, pool.idle) for pool in status.backends[0].pools] == [
        ("MAS_EMP", 4, 0, 1),
        ("MAS_PCC", 4, 0, 1),
    ]
    assert f"process: pid {os.getpid()}, " in status_text(status)
