"""A short soak on an emulated ProvideX install (SPEC.md §13.5, §15.7): scripts/soak.py's run, its report,
and connections counted outside the server. The long run against ABC belongs to the Windows runner."""

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
from tests.paths import REPO

pytestmark = [pytest.mark.anyio]

SOAK = runpy.run_path(str(REPO / "scripts" / "soak.py"), run_name="soak")
run_soak: Callable[..., Any] = SOAK["run_soak"]
URL = "http://127.0.0.1:8765/mcp"


async def test_twenty_workers_on_the_emulator(tmp_path: Path) -> None:
    install = install_at(tmp_path, "calvin_graham")
    runtime = providex_runtime(tmp_path / "home", install)
    emulator.INSTALLS[install.directory.lower()] = install
    tokens = [token_store(runtime.home).create(f"soak-{n}")[1] for n in range(20)]
    app = build_http_app(runtime.loaded, runtime)

    def factory(headers: dict[str, str]) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers, timeout=120)

    async with running(app):
        report = await run_soak(URL, tokens, factory=factory, seconds=12, workers=20, company="EMP", year=2024,
                                sample_s=1)  # fmt: skip
    cap = runtime.config.limits.per_company_concurrency.providex
    runtime.close()
    assert report["calls"] > 100
    assert {tool for tool in report["tools"]} >= {"ar_aging", "run_query", "open_sales_orders", "describe_table"}
    structural = [failure for failure in report["failures"] if not failure.startswith("memory")]
    assert structural == [], report
    assert report["pools_peak"] <= cap
    assert all(figures["outcomes"].get("ok") for figures in report["tools"].values()), report["tools"]
    assert install.open_connections == 0, "closing the runtime closes every connection the soak opened"
    assert install.peak_open <= cap + 1, f"{install.peak_open} connections at once (cap {cap}, plus discovery)"
    assert report["memory_mb"]["first"] is not None
