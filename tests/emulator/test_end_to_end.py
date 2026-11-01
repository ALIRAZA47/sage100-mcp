"""Every tool end to end on an emulated ProvideX install (SPEC.md §15.2), over the MCP client."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent, TextResourceContents

from sage100_mcp.errors import ErrorCode
from sage100_mcp.runtime import Runtime
from sage100_mcp.server import build_server
from tests import emulator
from tests.emulator.conftest import install_at, providex_runtime
from tests.providex_fixture import custom, profile

pytestmark = [pytest.mark.anyio]


@pytest.fixture(scope="module")
def pxplus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Runtime, emulator.Install]]:
    tmp = tmp_path_factory.mktemp("pxplus")
    install = install_at(tmp, "pxplus_full")
    runtime = providex_runtime(tmp / "home", install)
    yield runtime, install
    runtime.close()


@pytest.fixture(scope="module")
def calvin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Runtime, emulator.Install]]:
    tmp = tmp_path_factory.mktemp("calvin")
    install = install_at(tmp, "calvin_graham")
    runtime = providex_runtime(tmp / "home", install)
    yield runtime, install
    runtime.close()


@pytest.fixture(autouse=True)
def _keep_module_installs() -> Iterator[None]:
    """The module-scoped installs outlive the per-test INSTALLS reset in conftest.py."""
    saved = dict(emulator.INSTALLS)
    yield
    emulator.INSTALLS.update(saved)


async def call(runtime: Runtime, tool: str, arguments: dict[str, Any]) -> CallToolResult:
    for install in list(emulator.INSTALLS.values()):
        emulator.INSTALLS[install.directory.lower()] = install
    async with Client(build_server(runtime.loaded, runtime)) as client:
        return await client.call_tool(tool, arguments)


def ok(outcome: CallToolResult) -> dict[str, Any]:
    content = outcome.content[0]
    assert isinstance(content, TextContent)
    assert outcome.is_error is False, content.text
    assert outcome.structured_content is not None
    structured: dict[str, Any] = outcome.structured_content
    return structured


def failed(outcome: CallToolResult) -> dict[str, Any]:
    assert outcome.is_error is True
    assert outcome.structured_content is not None
    structured: dict[str, Any] = outcome.structured_content
    return structured


@pytest.fixture(autouse=True)
def _register(pxplus: tuple[Runtime, emulator.Install], calvin: tuple[Runtime, emulator.Install]) -> None:
    for _, install in (pxplus, calvin):
        emulator.INSTALLS[install.directory.lower()] = install


# --- discovery ---------------------------------------------------------------------------------------


async def test_status_reports_the_probed_driver(pxplus: tuple[Runtime, emulator.Install]) -> None:
    status = ok(await call(pxplus[0], "sage_status", {}))
    (backend,) = status["backends"]
    assert (backend["type"], backend["state"], backend["driver_version"]) == ("providex", "connected", "4.40.0004")
    assert backend["capabilities"] == "probed"
    assert "count_distinct" in backend["capability_summary"]["failed"]
    assert "ij_nested" in backend["capability_summary"]["passed"]
    assert status["catalog_status"] == "ready"


async def test_companies_profiles_and_catalog(pxplus: tuple[Runtime, emulator.Install]) -> None:
    listed = ok(await call(pxplus[0], "list_companies", {}))
    by_code = {c["code"]: c for c in listed["companies"]}
    assert set(by_code) == {"EMP", "PCC"}  # ABC is hidden
    assert by_code["EMP"]["name"] == "Example Emporium Inc."
    assert {"AR", "AP", "SO", "IM"} <= set(by_code["EMP"]["modules"])
    assert set(by_code["PCC"]["modules"]) == {"AP", "CI", "GL"}
    table = ok(await call(pxplus[0], "describe_table", {"company": "EMP", "table": "ar_openinvoice"}))
    assert table["key"] == ["ARDivisionNo", "CustomerNo", "InvoiceNo", "InvoiceType"]
    assert next(c for c in table["columns"] if c["name"] == "InvoiceDate")["type"] == "date"
    found = ok(await call(pxplus[0], "search_catalog", {"company": "EMP", "text": "credit limit", "limit": 5}))
    assert any(hit["column"] == "CreditLimit" for hit in found["hits"])


async def test_test_connection(pxplus: tuple[Runtime, emulator.Install]) -> None:
    result = ok(await call(pxplus[0], "test_connection", {"company": "PCC"}))
    assert result["driver_version"] == "4.40.0004"
    assert pxplus[1].statements[-1] == "SELECT ModuleCode FROM AP_Options"


# --- run_query -----------------------------------------------------------------------------------------


async def test_native_query(pxplus: tuple[Runtime, emulator.Install]) -> None:
    result = ok(
        await call(
            pxplus[0],
            "run_query",
            {
                "company": "EMP",
                "sql": "SELECT c.CustomerNo, c.CustomerName, SUM(i.Balance) AS open_balance FROM AR_Customer c "
                "JOIN AR_OpenInvoice i ON i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo "
                "WHERE i.Balance <> 0 AND i.InvoiceDate >= ? GROUP BY c.CustomerNo, c.CustomerName",
                "params": ["2024-01-01"],
            },
        )
    )
    assert result["mode"] == "native"
    assert result["sent_sql"][0].startswith(
        "SELECT c.CustomerNo, c.CustomerName, SUM(i.Balance) FROM {IJ AR_Customer c"
    )
    assert [c["name"] for c in result["columns"]] == ["CustomerNo", "CustomerName", "open_balance"]
    assert all(isinstance(row[2], str) and "." in row[2] for row in result["rows"])  # exact decimal strings
    assert result["warnings"] == []


async def test_hybrid_query_matches_native(pxplus: tuple[Runtime, emulator.Install]) -> None:
    sql = (
        "SELECT CustomerNo, COUNT(*) AS n, SUM(Balance) AS total FROM AR_OpenInvoice "
        "WHERE Balance <> 0 GROUP BY CustomerNo ORDER BY CustomerNo"
    )
    native = ok(await call(pxplus[0], "run_query", {"company": "EMP", "sql": sql}))
    local = ok(await call(pxplus[0], "run_query", {"company": "EMP", "sql": sql, "mode": "hybrid"}))
    assert (native["mode"], local["mode"]) == ("native", "hybrid")
    assert native["rows"] == local["rows"]
    assert local["sent_sql"] == ["SELECT CustomerNo, Balance FROM AR_OpenInvoice WHERE Balance <> 0"]
    assert local["warnings"][0].startswith("HYBRID_MODE: ")
    assert local["warnings"][1].startswith("ORDER_APPLIED_LOCALLY: ")


async def test_auto_goes_hybrid_for_what_the_driver_lacks(calvin: tuple[Runtime, emulator.Install]) -> None:
    result = ok(
        await call(
            calvin[0],
            "run_query",
            {
                "company": "EMP",
                "sql": "SELECT CustomerNo FROM AR_Customer WHERE CustomerType = 'R' OR CreditLimit > 9000 "
                "ORDER BY CustomerNo",
            },
        )
    )
    assert result["mode"] == "hybrid"
    assert "ORDER BY applied locally: the driver does not support it" in result["warnings"][1]
    assert result["rows"] == sorted(result["rows"])


async def test_native_mode_refuses(calvin: tuple[Runtime, emulator.Install]) -> None:
    error = failed(
        await call(
            calvin[0],
            "run_query",
            {"company": "EMP", "sql": "SELECT CustomerNo FROM AR_Customer ORDER BY CustomerNo", "mode": "native"},
        )
    )
    assert error["code"] == ErrorCode.DIALECT_UNSUPPORTED


async def test_masked_columns_never_reach_the_driver(pxplus: tuple[Runtime, emulator.Install]) -> None:
    runtime, install = pxplus
    before = len(install.statements)
    result = ok(
        await call(
            runtime,
            "run_query",
            {"company": "EMP", "sql": "SELECT VendorNo, TaxpayerIDSocialSecurityNo FROM AP_Vendor"},
        )
    )
    assert {row[1] for row in result["rows"]} == {"***"}
    assert all("Taxpayer" not in sql for sql in install.statements[before:])


async def test_history_tables_need_a_filter(pxplus: tuple[Runtime, emulator.Install]) -> None:
    error = failed(
        await call(pxplus[0], "run_query", {"company": "EMP", "sql": "SELECT InvoiceNo FROM AR_InvoiceHistoryHeader"})
    )
    assert error["code"] == ErrorCode.FILTER_REQUIRED
    assert "InvoiceDate >=" in error["hint"]
    filtered = await call(
        pxplus[0],
        "run_query",
        {"company": "EMP", "sql": "SELECT InvoiceNo FROM AR_InvoiceHistoryHeader WHERE InvoiceDate >= '2024-01-01'"},
    )
    assert ok(filtered)["mode"] == "native"


async def test_raw_providex_is_sent_as_written(pxplus: tuple[Runtime, emulator.Install]) -> None:
    sql = (
        "SELECT c.CustomerNo FROM {oj AR_Customer c LEFT OUTER JOIN AR_OpenInvoice i ON (i.CustomerNo = c.CustomerNo)} "
        "WHERE c.CustomerNo = 'C0001'"
    )
    result = ok(await call(pxplus[0], "run_query", {"company": "EMP", "sql": sql, "dialect": "providex"}))
    assert result["sent_sql"] == [sql]
    assert pxplus[1].statements[-1] == sql
    error = failed(
        await call(
            pxplus[0],
            "run_query",
            {"company": "EMP", "sql": "SELECT TaxpayerIDSocialSecurityNo FROM AP_Vendor", "dialect": "providex"},
        )
    )
    assert error["code"] == ErrorCode.PERMISSION_DENIED
    blocked = failed(
        await call(pxplus[0], "run_query", {"company": "EMP", "sql": "{call SY_Purge}", "dialect": "providex"})
    )
    assert blocked["code"] == ErrorCode.WRITE_BLOCKED


async def test_explain_shows_the_plan(calvin: tuple[Runtime, emulator.Install]) -> None:
    plan = ok(
        await call(
            calvin[0],
            "explain_query",
            {"company": "EMP", "sql": "SELECT COUNT(DISTINCT CustomerNo) FROM AR_OpenInvoice WHERE Balance <> 0"},
        )
    )
    assert plan["mode"] == "hybrid"
    assert plan["missing"] == [{"feature": "count_distinct", "detail": "COUNT(DISTINCT x)"}]
    assert plan["pulls"] == [
        {
            "table": "AR_OpenInvoice",
            "alias": "AR_OpenInvoice",
            "columns": ["CustomerNo", "Balance"],
            "pushed_conditions": 1,
            "sql": "SELECT CustomerNo, Balance FROM AR_OpenInvoice WHERE Balance <> 0",
        }
    ]
    assert "COUNT(DISTINCT" in plan["local_sql"]


# --- admin and resources ----------------------------------------------------------------------------


async def test_refresh_capabilities(calvin: tuple[Runtime, emulator.Install]) -> None:
    runtime, install = calvin
    install.profile = custom("calvin_graham", order_by_column=True)
    try:
        result = ok(await call(runtime, "refresh_capabilities", {"company": "EMP"}))
    finally:
        install.profile = profile("calvin_graham")
        ok(await call(runtime, "refresh_capabilities", {"company": "EMP"}))
    assert result["status"] == "probed"
    assert result["changed"] == ["order_by_column"]


async def test_capability_resources(calvin: tuple[Runtime, emulator.Install]) -> None:
    async with Client(build_server(calvin[0].loaded, calvin[0])) as client:
        report_contents = (await client.read_resource("sage://capabilities")).contents
        guide_contents = (await client.read_resource("sage://dialect-guide")).contents
    assert isinstance(report_contents[0], TextResourceContents)
    report = json.loads(report_contents[0].text)
    assert report["backends"][0]["capabilities"]["source"] == "probe"
    assert "or" in report["backends"][0]["capabilities"]["failed"]
    assert report["cached_profiles"][0]["driver_version"] == "4.40.0004"
    assert isinstance(guide_contents[0], TextResourceContents)
    assert "Run locally: case, cast, comments" in guide_contents[0].text


# --- failures ------------------------------------------------------------------------------------------


async def test_bad_dates_are_read_as_null(tmp_path: Path) -> None:
    install = install_at(tmp_path, "calvin_graham", bad_dates={("EMP", "AR_OpenInvoice")})
    runtime = providex_runtime(tmp_path / "home", install)
    result = ok(await call(runtime, "run_query", {"company": "EMP", "sql": "SELECT InvoiceNo FROM AR_OpenInvoice"}))
    runtime.close()
    assert result["null_dates_coerced"] is True
    assert any(w.startswith("NULL_DATES_COERCED: ") for w in result["warnings"])


async def test_pushdown_limit(tmp_path: Path) -> None:
    install = install_at(tmp_path, "calvin_graham")
    runtime = providex_runtime(tmp_path / "home", install, limits={"max_pushdown_rows": 20})
    error = failed(
        await call(
            runtime, "run_query", {"company": "EMP", "sql": "SELECT COUNT(DISTINCT CustomerNo) FROM AR_OpenInvoice"}
        )
    )
    runtime.close()
    assert error["code"] == ErrorCode.PUSHDOWN_LIMIT
    assert "AR_OpenInvoice" in error["message"]


async def test_a_driver_refusal_is_replanned_once(tmp_path: Path) -> None:
    """The probe passed ORDER BY, but this field fails on the real driver: auto re-plans as hybrid (§14.2)."""
    install = install_at(tmp_path, "pxplus_full")
    runtime = providex_runtime(tmp_path / "home", install)
    install.profile = custom("pxplus_full", order_by_position=False)
    sql = "SELECT CustomerNo, CustomerName FROM AR_Customer WHERE CustomerNo LIKE 'C00%' ORDER BY CustomerName"
    replanned = ok(await call(runtime, "run_query", {"company": "EMP", "sql": sql}))
    refused = failed(await call(runtime, "run_query", {"company": "EMP", "sql": sql, "mode": "native"}))
    runtime.close()
    assert replanned["mode"] == "hybrid"
    assert "the driver refused the native SQL" in replanned["warnings"][0]
    assert refused["code"] == ErrorCode.DIALECT_UNSUPPORTED


async def test_timeouts(tmp_path: Path) -> None:
    install = install_at(tmp_path, "calvin_graham", slow_tables={"AR_Customer": 5.0})
    runtime = providex_runtime(tmp_path / "home", install)
    error = failed(
        await call(
            runtime, "run_query", {"company": "EMP", "sql": "SELECT CustomerNo FROM AR_Customer", "timeout_s": 1}
        )
    )
    runtime.close()
    assert error["code"] == ErrorCode.QUERY_TIMEOUT


# --- paging (§13.3) ----------------------------------------------------------------------------------------


async def pages(runtime: Runtime, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    found = [ok(await call(runtime, "run_query", arguments))]
    while cursor := found[-1].get("next_cursor"):
        found.append(ok(await call(runtime, "run_query", {**arguments, "cursor": cursor})))
    return found


LOCAL_ONLY = "SELECT CustomerNo, CustomerName FROM AR_Customer WHERE CreditLimit > 0 OR CustomerType = 'R' ORDER BY 2"


@pytest.mark.parametrize(
    ("profile_name", "sql", "mode"),
    [
        ("pxplus_full", "SELECT CustomerNo, CustomerName, CreditLimit FROM AR_Customer", "native"),
        ("calvin_graham", LOCAL_ONLY, "hybrid"),
    ],
)
async def test_paging_reads_one_snapshot(tmp_path: Path, profile_name: str, sql: str, mode: str) -> None:
    install = install_at(tmp_path, profile_name)
    runtime = providex_runtime(tmp_path / "home", install)
    whole = ok(await call(runtime, "run_query", {"company": "EMP", "sql": sql}))
    statements = len(install.statements)
    found = await pages(runtime, {"company": "EMP", "sql": sql, "max_rows": 4})
    runtime.close()
    assert whole["truncated"] is False
    assert len(found) == -(-whole["row_count"] // 4) > 2
    assert [row for page in found for row in page["rows"]] == whole["rows"]
    assert {page["mode"] for page in found} == {mode}
    assert [bool(page["sent_sql"]) for page in found] == [True, True] + [False] * (len(found) - 2)
    # One statement for the first page and one for the snapshot (hybrid reads its one table each time).
    assert len(install.statements) - statements == 2
    assert (found[-1]["truncated"], found[-1].get("next_cursor")) == (False, None)


async def test_a_cursor_refuses_rows_that_changed(tmp_path: Path) -> None:
    install = install_at(tmp_path, "pxplus_full")
    runtime = providex_runtime(tmp_path / "home", install)
    sql = "SELECT CustomerNo, CustomerName FROM AR_Customer"
    first = ok(await call(runtime, "run_query", {"company": "EMP", "sql": sql, "max_rows": 4}))
    install.database("EMP").execute(
        'UPDATE "AR_Customer" SET "CustomerName" = ? WHERE "CustomerNo" = ?', ["Renamed meanwhile", first["rows"][1][0]]
    )
    changed = failed(await call(runtime, "run_query", {"sql": sql, "max_rows": 4, "cursor": first["next_cursor"]}))
    other = failed(
        await call(runtime, "run_query", {"sql": sql + " WHERE CustomerNo > ''", "cursor": first["next_cursor"]})
    )
    runtime.close()
    assert changed["code"] == other["code"] == ErrorCode.CURSOR_INVALID
    assert "changed since the first page" in changed["message"]
    assert "different query" in other["message"]
