"""Emulated Sage installs for the tier-2 tests (SPEC.md §15.2)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from sage100_mcp.backends.providex import ProvideXExecutor
from sage100_mcp.bridge.client import Bridge
from sage100_mcp.capabilities import Capabilities
from sage100_mcp.config import CompaniesSection, Config, LimitsSection, LoadedConfig, ProvideXBackend, SqlServerBackend
from sage100_mcp.credentials import Credential
from sage100_mcp.runtime import Runtime
from tests import emulator
from tests.bridge_fixture import thread_bridge
from tests.fake_backend import UNLIMITED
from tests.providex_fixture import profile

Installer = Callable[..., emulator.Install]
Maker = Callable[..., ProvideXExecutor]


@pytest.fixture(autouse=True)
def _fresh_installs() -> Iterator[None]:
    emulator.INSTALLS.clear()
    yield
    emulator.INSTALLS.clear()


@pytest.fixture
def make_install(tmp_path: Path) -> Installer:
    """A standard install (ABC, EMP, PCC) under a capability profile, with its MAS_ folders on disk."""

    def make(caps: Capabilities | str = "pxplus_full", **options: Any) -> emulator.Install:
        capabilities = profile(caps) if isinstance(caps, str) else caps
        install = emulator.standard_install(str(tmp_path / "MAS90"), capabilities, **options)
        install.make_folders()
        return install

    return make


@pytest.fixture(params=["direct", "helper"])
def driver(request: pytest.FixtureRequest) -> Iterator[Callable[[], Any]]:
    """What the executor calls pyodbc: the emulator itself, or the 32-bit helper serving it (D67)."""
    if request.param == "direct":
        yield lambda: emulator
        return
    bridges: list[Bridge] = []

    def through_the_helper() -> Bridge:
        bridges.append(thread_bridge())
        return bridges[-1]

    yield through_the_helper
    for bridge in bridges:
        bridge.shutdown()


@pytest.fixture
def make_executor(isolated_home: Path, driver: Callable[[], Any]) -> Maker:
    def make(
        install: emulator.Install,
        *,
        settings: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        companies: dict[str, Any] | None = None,
        password: str = "reader-pass",
        **options: Any,
    ) -> ProvideXExecutor:
        backend = ProvideXBackend.model_validate(
            {"type": "providex", "directory": install.directory, "credential": "sage100-mcp/sage", **(settings or {})}
        )
        return ProvideXExecutor(
            "sage",
            backend,
            Credential("mcp_reader", password),
            LimitsSection.model_validate(limits or {}),
            home=isolated_home,
            companies=CompaniesSection.model_validate(companies if companies is not None else {"default": "EMP"}),
            odbc=options.pop("odbc", None) or driver(),
            **options,
        )

    return make


def providex_runtime(
    home: Path,
    install: emulator.Install,
    *,
    limits: dict[str, Any] | None = None,
    start: bool = True,
    odbc: Callable[[], Any] = lambda: emulator,
) -> Runtime:
    data: dict[str, Any] = {
        "backends": {"sage": {"type": "providex", "directory": install.directory, "credential": "sage100-mcp/sage"}},
        "companies": {"default": "EMP", "hide": ["ABC"]},
        "limits": UNLIMITED | (limits or {}),
    }
    loaded = LoadedConfig(config=Config.model_validate(data), path=Path("emulated.yaml"), found=True)

    def factory(name: str, settings: SqlServerBackend | ProvideXBackend, loaded: LoadedConfig, home: Path) -> Any:
        assert isinstance(settings, ProvideXBackend)
        credential = Credential("mcp_reader", "reader-pass")
        return ProvideXExecutor(
            name, settings, credential, loaded.config.limits, home=home, companies=loaded.config.companies,
            odbc=odbc(),
        )  # fmt: skip

    runtime = Runtime(loaded, home=home, executor_factory=factory)
    if start:
        runtime.start(background=False)
    return runtime


def install_at(tmp: Path, caps: Capabilities | str, **options: Any) -> emulator.Install:
    install = emulator.standard_install(str(tmp / "MAS90"), profile(caps) if isinstance(caps, str) else caps, **options)
    install.make_folders()
    return install
