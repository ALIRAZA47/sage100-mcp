"""scripts/remote_check.py, the pre-flight a Mac runs before the eval set (SPEC.md §18 M4), against an
emulated ProvideX install served over HTTP in process, and against a server that sees no company."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from sage100_mcp.http.app import build_http_app, token_store
from tests import emulator
from tests.contract.test_http import running
from tests.emulator.conftest import install_at, providex_runtime
from tests.fake_backend import fake_runtime
from tests.paths import REPO

pytestmark = [pytest.mark.contract, pytest.mark.anyio]

CHECK = runpy.run_path(str(REPO / "scripts" / "remote_check.py"), run_name="remote_check")
run_checks: Callable[..., Any] = CHECK["run_checks"]
URL = "http://127.0.0.1:8765/mcp"


def asgi(app: Any) -> Callable[[dict[str, str]], httpx2.AsyncClient]:
    return lambda headers: httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)


@pytest.mark.parametrize("profile_name", ["pxplus_full", "calvin_graham"])
async def test_every_check_passes_on_a_working_server(tmp_path: Path, profile_name: str) -> None:
    install = install_at(tmp_path, profile_name)
    runtime = providex_runtime(tmp_path / "home", install)
    emulator.INSTALLS[install.directory.lower()] = install
    token = token_store(runtime.home).create("mac")[1]
    app = build_http_app(runtime.loaded, runtime)
    async with running(app):
        checks = await run_checks(URL, token, factory=asgi(app), company="emp")
    runtime.close()
    assert [(c.name, c.passed) for c in checks] == [
        ("no token", True),
        ("foreign Origin", True),
        ("protocol 2025-11-25", True),
        ("protocol 2026-07-28", True),
        ("companies", True),
        ("active company", True),
        ("paging", True),
        ("canned view", True),
    ], [c.line() for c in checks]
    assert "page 2 holds 2 row(s)" in checks[6].detail


async def test_a_bad_token_stops_at_the_transport_checks(tmp_path: Path) -> None:
    runtime, _ = fake_runtime(tmp_path)
    app = build_http_app(runtime.loaded, runtime)
    async with running(app):
        checks = await run_checks(URL, "s1mcp_wrong", factory=asgi(app))
    runtime.close()
    assert [(c.name, c.passed) for c in checks] == [
        ("no token", True),
        ("foreign Origin", True),
        ("protocol 2025-11-25", False),
        ("protocol 2026-07-28", False),
    ]


def test_the_token_comes_from_the_environment(capsys: pytest.CaptureFixture[str]) -> None:
    assert CHECK["main"](["https://sage-mcp.example.com/mcp"]) == 2
    assert "set SAGE100_MCP_TOKEN" in capsys.readouterr().err
