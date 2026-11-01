"""A short soak on the SQL Server test bed (SPEC.md §13.5, §15.7): 20 workers through scripts/soak.py, with
the server's sessions counted by SQL Server itself, so a leaked connection can't hide."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from sage100_mcp.http.app import build_http_app, token_store
from sage100_mcp.runtime import Runtime
from tests.contract.test_http import running
from tests.paths import REPO
from tests.premium.test_premium import admin

pytestmark = [pytest.mark.premium, pytest.mark.anyio]

SOAK = runpy.run_path(str(REPO / "scripts" / "soak.py"), run_name="soak")
run_soak: Callable[..., Any] = SOAK["run_soak"]
text: Callable[[dict[str, Any]], str] = SOAK["text"]
URL = "http://127.0.0.1:8765/mcp"
SESSIONS = "SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE program_name = 'sage100-mcp'"


def server_sessions() -> int:
    connection = admin("master")
    try:
        count: int = connection.cursor().execute(SESSIONS).fetchone()[0]
    finally:
        connection.close()
    return count


async def test_twenty_workers_on_sql_server(runtime: Runtime) -> None:
    tokens = [token_store(runtime.home).create(f"soak-{n}")[1] for n in range(20)]
    app = build_http_app(runtime.loaded, runtime)
    cap = runtime.config.limits.per_company_concurrency.sqlserver
    databases = len(runtime.companies) + 1  # each company's database, and MAS_SYSTEM for discovery

    def factory(headers: dict[str, str]) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers, timeout=120)

    async with running(app):
        report = await run_soak(URL, tokens, factory=factory, seconds=15, workers=20, company="EMP", year=2024,
                                sample_s=1, sessions=server_sessions, caps=cap * databases)  # fmt: skip
    print(text(report))
    structural = [failure for failure in report["failures"] if not failure.startswith("memory")]
    assert structural == [], report
    assert report["calls"] > 100
    assert report["pools_peak"] <= cap
    assert report["sessions_peak"] <= cap * databases
    assert all(figures["outcomes"].get("ok") for figures in report["tools"].values()), report["tools"]
