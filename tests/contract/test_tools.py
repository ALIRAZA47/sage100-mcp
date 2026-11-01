"""Tier 6 contract tests for the tools and resources (SPEC.md §11, §15.5) over the fake backend.

Every tool gets a happy-path call whose structuredContent must validate against its outputSchema, on
both protocol revisions, in process and over Streamable HTTP, and the error codes each tool can raise
come back as tool errors with the §14.1 envelope, never as protocol errors, on both transports.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx2
import jsonschema
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, TextResourceContents

from sage100_mcp.backends.base import RawResult
from sage100_mcp.errors import ErrorCode, ErrorEnvelope, SageError
from sage100_mcp.http.app import build_http_app, token_store
from sage100_mcp.runtime import Runtime
from sage100_mcp.server import build_server
from sage100_mcp.views.engine import load_views
from sage100_mcp.views.lookups import clear_cache
from sage100_mcp.views.model import OutputColumn
from tests.contract.test_http import LOCAL, running
from tests.fake_backend import FakeExecutor, fake_runtime, result

pytestmark = [pytest.mark.contract, pytest.mark.anyio]
MODES = ["legacy", "auto"]
TRANSPORTS = ["memory", "http"]

HAPPY: dict[str, dict[str, Any]] = {
    "sage_status": {},
    "list_companies": {},
    "set_company": {"company": "pcc"},
    "list_tables": {"company": "EMP", "module": "AR", "limit": 5},
    "describe_table": {"company": "EMP", "table": "AR_OpenInvoice"},
    "search_catalog": {"company": "EMP", "text": "customer name", "limit": 5},
    "run_query": {
        "company": "EMP",
        "sql": "SELECT CustomerNo, CreditLimit FROM AR_Customer WHERE State = ?",
        "params": ["WA"],
        "max_rows": 10,
    },
    "explain_query": {"company": "EMP", "sql": "SELECT CustomerNo FROM AR_Customer WHERE State = ?", "params": ["WA"]},
    "refresh_catalog": {"company": "PCC"},
    "refresh_capabilities": {"company": "EMP"},
    "test_connection": {"company": "EMP"},
    "ar_aging": {"company": "EMP", "as_of": "2024-12-31"},
    "ap_aging": {"company": "EMP", "detail": "summary", "include_on_hold": False},
    "open_sales_orders": {"company": "EMP", "order_types": ["S", "B", "Q"]},
    "open_purchase_orders": {"company": "EMP", "vendor": "01-AIRWAY"},
    "inventory_availability": {"company": "EMP", "item": "ITEM-0", "only_short": True},
    "gl_trial_balance": {"company": "EMP", "fiscal_year": 2024, "period": "12"},
    "sales_history": {"company": "EMP", "from_date": "2024-01-01", "to_date": "2024-12-31", "group_by": "month"},
    "customer_lookup": {"company": "EMP", "query": "acme"},
    "vendor_lookup": {"company": "EMP", "query": "V000001"},
    "item_lookup": {"company": "EMP", "query": "item-001"},
    "compare_companies": {
        "view": "gl_trial_balance",
        "companies": ["EMP", "PCC"],
        "args": {"fiscal_year": "2024", "period": 12},
    },
}


def _sample(column: OutputColumn) -> Any:
    if column.type.startswith("decimal"):
        return Decimal("12.5")
    return {"date": date(2024, 12, 31), "int": 3, "bool": True}.get(column.type, "C0001")


def view_rows(view: str, variant: str) -> RawResult:
    """One row shaped like the view's declared output, as the backend would return it."""
    columns = load_views()[view].variants[variant].columns
    return result([(c.name, c.type) for c in columns], [tuple(_sample(c) for c in columns)])


def one_row(**values: Any) -> RawResult:
    return result([(name, "varchar(30)") for name in values], [tuple(values.values())])


AGING = {f"AgingCategory{i}": Decimal(days) for i, days in enumerate((30, 60, 90, 120), start=1)}
CONTACT = {"EmailAddress": "ap@example.com", "TelephoneNo": "555-0100", "City": "Tacoma", "State": "WA"}
OPEN = {"InvoiceDate": datetime(2024, 12, 1), "Balance": Decimal("10.00")}
# What the backend returns, in order, for each view tool's happy path: the options rows its rules read,
# then the view's own statement (or a lookup's master read and its figures).
QUEUED: dict[str, list[RawResult]] = {
    "ar_aging": [
        one_row(AgeInvoicesBy="D", AgingCategory="D", Divisions="N", **AGING),
        view_rows("ar_aging", "summary"),
    ],
    "ap_aging": [one_row(AgingCategoriesToUseForInv="D", Divisions="N", **AGING), view_rows("ap_aging", "summary")],
    "open_sales_orders": [view_rows("open_sales_orders", "true")],
    "open_purchase_orders": [view_rows("open_purchase_orders", "true")],
    "inventory_availability": [
        one_row(QuantityAvailableToInclude="P"),
        view_rows("inventory_availability", "default"),
    ],
    "gl_trial_balance": [view_rows("gl_trial_balance", "default")],
    "sales_history": [view_rows("sales_history", "default")],
    "customer_lookup": [
        one_row(ARDivisionNo="00", CustomerNo="C0001", CustomerName="Acme Supply", **CONTACT),
        one_row(ARDivisionNo="00", CustomerNo="C0001", **OPEN),
    ],
    "vendor_lookup": [
        one_row(APDivisionNo="00", VendorNo="V000001", VendorName="Acme Freight", **CONTACT),
        one_row(APDivisionNo="00", VendorNo="V000001", **OPEN),
    ],
    "item_lookup": [
        one_row(ItemCode="ITEM-001", ItemCodeDesc="Chair", ProductLine="FURN", ItemType="1"),
        one_row(QuantityAvailableToInclude="N"),
        one_row(ItemCode="ITEM-001", QuantityOnHand=Decimal(5), free=Decimal(3), QuantityOnPurchaseOrder=Decimal(2)),
    ],
    "compare_companies": [view_rows("gl_trial_balance", "default"), view_rows("gl_trial_balance", "default")],
}


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> tuple[Runtime, FakeExecutor]:
    return fake_runtime(tmp_path_factory.mktemp("home"))


@pytest.fixture
def setup(shared: tuple[Runtime, FakeExecutor]) -> tuple[MCPServer, FakeExecutor]:
    runtime, fake = shared
    runtime.active_company = None
    fake.sent.clear()
    fake.results.clear()
    fake.fail_with = None
    clear_cache()
    return build_server(runtime.loaded, runtime), fake


def text_of(outcome: CallToolResult) -> str:
    assert len(outcome.content) == 1
    content = outcome.content[0]
    assert isinstance(content, TextContent)
    return content.text


def test_every_tool_has_a_happy_path() -> None:
    import tests.contract.test_mcp_contract as contract

    names = [tool["name"] for tool in json.loads(contract.SNAPSHOT.read_text(encoding="utf-8"))]
    assert sorted(HAPPY) == names


@asynccontextmanager
async def connect(
    shared: tuple[Runtime, FakeExecutor], server: MCPServer, transport: str, mode: str = "auto"
) -> AsyncIterator[Client]:
    """A client in process, or over Streamable HTTP with a token of its own (§15.5: both transports)."""
    if transport == "memory":
        async with Client(server, mode=mode) as client:
            yield client
        return
    runtime, _ = shared
    app = build_http_app(runtime.loaded, runtime)
    headers = {"authorization": f"Bearer {token_store(runtime.home).create('contract')[1]}"}
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)
    async with running(app), Client(streamable_http_client(f"{LOCAL}/mcp", http_client=http), mode=mode) as client:
        yield client


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", list(HAPPY))
async def test_happy_path(
    shared: tuple[Runtime, FakeExecutor], setup: tuple[MCPServer, FakeExecutor], tool: str, mode: str, transport: str
) -> None:
    server, fake = setup
    fake.results += QUEUED.get(
        tool,
        [result([("CustomerNo", "varchar(20)"), ("CreditLimit", "decimal(13,2)")], [("C0001", Decimal("1500.00"))])],
    )
    async with connect(shared, server, transport, mode) as client:
        schema = next(t.output_schema for t in (await client.list_tools()).tools if t.name == tool)
        outcome = await client.call_tool(tool, HAPPY[tool])
    assert outcome.is_error is False, text_of(outcome)
    assert outcome.structured_content is not None
    assert schema is not None
    jsonschema.validate(outcome.structured_content, schema)
    assert text_of(outcome)
    if tool in QUEUED:
        assert fake.results == []  # every queued result was read: the view sent what it was expected to


async def test_view_envelope(setup: tuple[MCPServer, FakeExecutor]) -> None:
    """A view's rows come back typed by its declared columns, buckets named from the company's options."""
    server, fake = setup
    fake.results += QUEUED["ar_aging"]
    async with Client(server) as client:
        outcome = await client.call_tool("ar_aging", HAPPY["ar_aging"])
    envelope = outcome.structured_content
    assert envelope is not None
    assert [c["name"] for c in envelope["columns"]][4:] == [
        "current",
        "days_30_59",
        "days_60_89",
        "days_90_119",
        "days_120_plus",
        "total",
    ]
    assert envelope["rows"] == [["C0001", "C0001", "C0001", "C0001", *["12.50"] * 6]]
    assert envelope["totals"]["total"] == "12.50"
    assert (envelope["view"], envelope["version"], envelope["parameters"]["as_of"]) == ("ar_aging", 1, "2024-12-31")
    assert "Totals: current 12.50" in text_of(outcome)
    sent = fake.sent[-1]
    assert sent[0].startswith("WITH [aged] AS (")
    assert "DATEDIFF(DAY" in sent[0]
    assert sent[1][:2] == ("due", date(2024, 12, 31))  # the basis, then as_of, bound as parameters


async def test_run_query_envelope(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, fake = setup
    fake.results.append(
        result(
            [("CustomerNo", "varchar(20)"), ("CreditLimit", "decimal(13,2)")],
            [("C0001", Decimal("1500.00")), ("C0002", Decimal("0.10"))],
        )
    )
    async with Client(server) as client:
        outcome = await client.call_tool("run_query", HAPPY["run_query"])
    envelope = outcome.structured_content
    assert envelope is not None
    assert envelope["rows"] == [["C0001", "1500.00"], ["C0002", "0.10"]]
    assert envelope["columns"][1] == {
        "name": "CreditLimit",
        "type": "decimal(13,2)",
        "semantic": "money",
        "masked": False,
        "source": "AR_Customer.CreditLimit",
    }
    assert (envelope["row_count"], envelope["truncated"], envelope["mode"]) == (2, False, "native")
    assert "| C0001 | 1500.00 |" in text_of(outcome)
    assert fake.sent[0][1] == ("WA",)


ERRORS: list[tuple[str, dict[str, Any], ErrorCode]] = [
    ("run_query", {"company": "EMP", "sql": "UPDATE AR_Customer SET CreditLimit = 0"}, ErrorCode.WRITE_BLOCKED),
    (
        "run_query",
        {"company": "EMP", "sql": "SELECT 1 FROM AR_Customer; SELECT 2 FROM AR_Customer"},
        ErrorCode.MULTI_STATEMENT,
    ),
    ("run_query", {"company": "EMP", "sql": "SELECT * FROM OPENROWSET('a', 'b', 'c')"}, ErrorCode.FORBIDDEN_FUNCTION),
    ("run_query", {"company": "EMP", "sql": "SELEC CustomerNo FROM AR_Customer"}, ErrorCode.PARSE_ERROR),
    ("run_query", {"company": "EMP", "sql": "SELECT * FROM CI_ItemWarehouse"}, ErrorCode.UNKNOWN_TABLE),
    ("run_query", {"company": "EMP", "sql": "SELECT Nope FROM AR_Customer"}, ErrorCode.UNKNOWN_COLUMN),
    ("run_query", {"company": "EMP", "sql": "SELECT * FROM AR_CustomerCreditCard"}, ErrorCode.TABLE_DENIED),
    (
        "run_query",
        {"company": "EMP", "sql": "SELECT CustomerNo FROM AR_Customer", "dialect": "providex"},
        ErrorCode.DIALECT_UNSUPPORTED,
    ),
    (
        "run_query",
        {"company": "EMP", "sql": "SELECT VendorNo FROM AP_Vendor ORDER BY TaxpayerIDSocialSecurityNo"},
        ErrorCode.PERMISSION_DENIED,
    ),
    ("run_query", {"company": "XYZ", "sql": "SELECT CustomerNo FROM AR_Customer"}, ErrorCode.COMPANY_NOT_FOUND),
    ("run_query", {"sql": "SELECT CustomerNo FROM AR_Customer"}, ErrorCode.COMPANY_REQUIRED),
    ("describe_table", {"company": "EMP", "table": "AR_Customers"}, ErrorCode.UNKNOWN_TABLE),
    ("describe_table", {"company": "EMP", "table": "AR_CustomerCreditCard"}, ErrorCode.TABLE_DENIED),
    ("list_tables", {"company": "PCC", "module": "SO"}, ErrorCode.MODULE_NOT_INSTALLED),
    ("set_company", {"company": "MAS_EMP"}, ErrorCode.COMPANY_NOT_FOUND),
    ("search_catalog", {"text": "customer"}, ErrorCode.COMPANY_REQUIRED),
    ("explain_query", {"company": "EMP", "sql": "DELETE FROM AR_Customer"}, ErrorCode.WRITE_BLOCKED),
    ("explain_query", {"company": "EMP", "sql": "SELECT Nope FROM AR_Customer"}, ErrorCode.UNKNOWN_COLUMN),
    ("refresh_capabilities", {"company": "XYZ"}, ErrorCode.COMPANY_NOT_FOUND),
    ("ar_aging", {"company": "EMP", "as_of": "31/12/2024"}, ErrorCode.PARSE_ERROR),
    ("ar_aging", {"company": "PCC"}, ErrorCode.MODULE_NOT_INSTALLED),
    ("ap_aging", {"company": "XYZ"}, ErrorCode.COMPANY_NOT_FOUND),
    ("open_sales_orders", {"company": "EMP", "customer": "a;b"}, ErrorCode.PARSE_ERROR),
    ("open_purchase_orders", {"company": "PCC"}, ErrorCode.MODULE_NOT_INSTALLED),
    ("inventory_availability", {"item": "ITEM"}, ErrorCode.COMPANY_REQUIRED),
    ("gl_trial_balance", {"company": "EMP", "fiscal_year": "24", "period": 1}, ErrorCode.PARSE_ERROR),
    (
        "sales_history",
        {"company": "EMP", "from_date": "2024-12-31", "to_date": "2024-01-01"},
        ErrorCode.PARSE_ERROR,
    ),
    ("customer_lookup", {"company": "EMP", "query": " "}, ErrorCode.PARSE_ERROR),
    ("vendor_lookup", {"company": "XYZ", "query": "acme"}, ErrorCode.COMPANY_NOT_FOUND),
    ("item_lookup", {"company": "EMP", "query": "x" * 101}, ErrorCode.PARSE_ERROR),
    ("compare_companies", {"view": "ar_aging", "companies": ["EMP"]}, ErrorCode.PARSE_ERROR),
    ("compare_companies", {"view": "nope", "companies": ["EMP", "PCC"]}, ErrorCode.PARSE_ERROR),
    ("compare_companies", {"view": "ar_aging", "companies": ["EMP", "XYZ"]}, ErrorCode.COMPANY_NOT_FOUND),
    ("compare_companies", {"view": "ar_aging", "companies": ["EMP", "PCC"]}, ErrorCode.MODULE_NOT_INSTALLED),
]


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize(("tool", "arguments", "code"), ERRORS, ids=[f"{t}-{c.value}" for t, _, c in ERRORS])
async def test_error_results(
    shared: tuple[Runtime, FakeExecutor],
    setup: tuple[MCPServer, FakeExecutor],
    tool: str,
    arguments: dict[str, Any],
    code: ErrorCode,
    transport: str,
) -> None:
    server, fake = setup
    async with connect(shared, server, transport) as client:
        outcome = await client.call_tool(tool, arguments)
    assert outcome.is_error is True
    envelope = ErrorEnvelope.model_validate(outcome.structured_content)
    assert envelope.code == code
    assert text_of(outcome).startswith(f"{code.value}: ")
    assert fake.sent == []  # refused before anything reached the database


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (SageError(ErrorCode.LOCK_TIMEOUT, "Lock request time out period exceeded."), True),
        (SageError(ErrorCode.QUERY_TIMEOUT, "Query timeout expired"), False),
        (SageError(ErrorCode.PERMISSION_DENIED, "The SELECT permission was denied."), False),
    ],
)
async def test_database_errors(setup: tuple[MCPServer, FakeExecutor], error: SageError, retryable: bool) -> None:
    server, fake = setup
    fake.results.append(error)
    async with Client(server) as client:
        outcome = await client.call_tool("run_query", HAPPY["run_query"])
    assert outcome.is_error is True
    envelope = ErrorEnvelope.model_validate(outcome.structured_content)
    assert (envelope.code, envelope.retryable) == (error.code, retryable)


async def test_unreachable_backend(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, fake = setup
    fake.fail_with = SageError(ErrorCode.BACKEND_UNREACHABLE, "Login timeout expired")
    async with Client(server) as client:
        outcome = await client.call_tool("test_connection", {"company": "EMP"})
    assert ErrorEnvelope.model_validate(outcome.structured_content).code == ErrorCode.BACKEND_UNREACHABLE


async def test_invalid_arguments_are_tool_errors(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, _ = setup
    async with Client(server) as client:
        outcome = await client.call_tool("run_query", {"company": "EMP", "sql": "SELECT 1", "max_rows": 0})
    assert outcome.is_error is True


async def test_set_company_is_remembered(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, _ = setup
    async with Client(server) as client:
        await client.call_tool("set_company", {"company": "EMP"})
        outcome = await client.call_tool("list_tables", {"module": "SO", "limit": 3})
        companies = await client.call_tool("list_companies", {})
    assert outcome.is_error is False
    assert outcome.structured_content is not None
    assert outcome.structured_content["company"] == "EMP"
    assert companies.structured_content is not None
    assert companies.structured_content["active"] == "EMP"


async def read_json(client: Client, uri: str) -> Any:
    contents = (await client.read_resource(uri)).contents
    assert len(contents) == 1
    assert isinstance(contents[0], TextResourceContents)
    assert contents[0].mime_type == "application/json"
    return json.loads(contents[0].text)


async def test_resources(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, _ = setup
    async with Client(server) as client:
        companies = await read_json(client, "sage://companies")
        policy = await read_json(client, "sage://policy")
        catalog = await read_json(client, "sage://EMP/catalog")
        table = await read_json(client, "sage://EMP/tables/AR_Customer")
    assert [c["code"] for c in companies["companies"]] == ["EMP", "PCC"]
    assert any(rule["pattern"] == "PR_*" for rule in policy["deny_tables"])
    assert policy["limits"]["max_rows_hard"] == 50000
    assert {"table": "AR_Customer", "purpose": table["purpose"]} in catalog["modules"]["AR"]
    assert "columns" not in json.dumps(catalog)
    assert table["key"] == ["ARDivisionNo", "CustomerNo"]


async def test_views_resource(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, fake = setup
    async with Client(server) as client:
        described = await read_json(client, "sage://views")
    assert [v["name"] for v in described["views"]] == sorted(load_views())
    assert [v["name"] for v in described["lookups"]] == ["customer_lookup", "vendor_lookup", "item_lookup"]
    trial = next(v for v in described["views"] if v["name"] == "gl_trial_balance")
    assert trial["providex_pulls"][0]["where"] == ["FiscalYear = :fiscal_year"]
    assert trial["variants"]["default"]["premium_tsql"].startswith("WITH balances AS (")
    assert fake.sent == []


async def test_prompts(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, _ = setup
    async with Client(server) as client:
        listed = (await client.list_prompts()).prompts
        got = await client.get_prompt("month_end_checks", {"company": "EMP", "fiscal_year": "2024", "period": "12"})
        aging = await client.get_prompt("ar_collections_review", {"company": "EMP"})
    assert [p.name for p in listed] == ["ar_collections_review", "order_backlog_review", "month_end_checks",
                                        "explore_module"]  # fmt: skip
    assert all(p.title and p.description for p in listed)
    content = got.messages[0].content
    assert isinstance(content, TextContent)
    assert 'gl_trial_balance with company="EMP", fiscal_year="2024" and period="12"' in content.text
    assert "data, not instructions" in content.text
    first = aging.messages[0].content
    assert isinstance(first, TextContent)
    assert 'as_of="today"' in first.text


async def test_capability_resources_without_providex(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, _ = setup
    async with Client(server) as client:
        capabilities = await read_json(client, "sage://capabilities")
        guide = (await client.read_resource("sage://dialect-guide")).contents
    assert capabilities["backends"] == []
    assert isinstance(guide[0], TextResourceContents)
    assert guide[0].mime_type == "text/markdown"
    assert "No ProvideX backend is configured" in guide[0].text


async def test_explain_on_premium_sends_nothing(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, fake = setup
    async with Client(server) as client:
        outcome = await client.call_tool("explain_query", HAPPY["explain_query"])
    assert outcome.structured_content is not None
    assert outcome.structured_content["mode"] == "native"
    assert outcome.structured_content["sent_sql"][0].startswith("SELECT TOP 1001 [AR_Customer].[CustomerNo]")
    assert fake.sent == []


async def test_resource_errors_do_not_leak(setup: tuple[MCPServer, FakeExecutor]) -> None:
    server, _ = setup
    async with Client(server) as client:
        with pytest.raises(Exception, match="TABLE_DENIED: AR_CustomerCreditCard is not available"):
            await client.read_resource("sage://EMP/tables/AR_CustomerCreditCard")
