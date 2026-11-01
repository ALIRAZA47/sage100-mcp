"""Shared fixtures for the Premium tests: the SQL Server test bed and a runtime over it (SPEC.md §15).

They run only when SAGE100_MCP_TEST_SQLSERVER and SAGE100_MCP_TEST_SA_PASSWORD name a SQL Server the
tests may rebuild databases on (CI uses a service container).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sage100_mcp.backends.sqlserver import SqlServerExecutor
from sage100_mcp.config import Config, LoadedConfig, ProvideXBackend, SqlServerBackend
from sage100_mcp.credentials import Credential
from sage100_mcp.runtime import Runtime
from tests import premium_testbed as testbed
from tests.fake_backend import UNLIMITED

SERVER = os.environ.get(testbed.SERVER_ENV)
SA_PASSWORD = os.environ.get(testbed.SA_PASSWORD_ENV)


def premium_config(server: str, **companies: Any) -> LoadedConfig:
    backend = {
        "type": "sqlserver",
        "server": server,
        "trust_server_certificate": True,
        "credential": "sage100-mcp/test_premium",
    }
    config = Config.model_validate({"backends": {"premium": backend}, "companies": companies, "limits": UNLIMITED})
    return LoadedConfig(config=config, path=Path("premium-test.yaml"), found=True)


def reader_factory(password: str = testbed.READER_PASSWORD) -> Any:
    def factory(
        name: str, settings: SqlServerBackend | ProvideXBackend, loaded: LoadedConfig, home: Path
    ) -> SqlServerExecutor:
        assert isinstance(settings, SqlServerBackend)
        return SqlServerExecutor(name, settings, Credential(testbed.READER, password), loaded.config.limits)

    return factory


@pytest.fixture(scope="session")
def server() -> str:
    if not SERVER or not SA_PASSWORD:
        pytest.skip(f"set {testbed.SERVER_ENV} and {testbed.SA_PASSWORD_ENV} to run the Premium tests")
    testbed.build(SERVER, SA_PASSWORD)
    return SERVER


@pytest.fixture(scope="session")
def runtime(server: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    started = time.perf_counter()
    runtime = Runtime(
        premium_config(server, hide=["ABC"]), home=tmp_path_factory.mktemp("premium"), executor_factory=reader_factory()
    )
    runtime.start(background=False)
    print(f"\ndiscovery and both catalogs: {time.perf_counter() - started:.2f} s")
    yield runtime
    runtime.close()
