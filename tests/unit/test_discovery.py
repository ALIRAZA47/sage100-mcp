"""Discovery and admin tool logic (SPEC.md §7.4, §8, §11.1) against the fake backend."""

from __future__ import annotations

import pytest

from sage100_mcp import discovery
from sage100_mcp.catalog.model import Catalog
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.runtime import Runtime
from tests.fake_backend import FakeExecutor, fake_runtime


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory) -> tuple[Runtime, FakeExecutor]:
    return fake_runtime(tmp_path_factory.mktemp("home"))


@pytest.fixture
def emp(setup: tuple[Runtime, FakeExecutor]) -> Catalog:
    runtime = setup[0]
    return runtime.catalog(runtime.resolve_company("EMP"))[0]


def test_list_companies(setup: tuple[Runtime, FakeExecutor]) -> None:
    runtime = setup[0]
    result = discovery.list_companies(runtime)
    assert [c.code for c in result.companies] == ["EMP", "PCC"]
    emp = result.companies[0]
    assert (emp.name, emp.backend, emp.backend_type, emp.catalog_status) == (
        "Example Emporium Inc.",
        "premium",
        "sqlserver",
        "ready",
    )
    assert emp.modules == ["AP", "AR", "CI", "GL", "IM", "PO", "SO"]
    assert emp.current_periods["AR"] == "2024-12"
    assert emp.tables == 903  # 908 less the 5 credit-card tables the policy hides
    text = discovery.companies_text(result)
    assert "| EMP | Example Emporium Inc. | sqlserver |" in text


def test_list_tables_puts_curated_tables_first(emp: Catalog) -> None:
    result = discovery.list_tables(emp, limit=5)
    assert (result.total, result.truncated) == (len([t for t in emp.tables.values() if not t.work]), True)
    assert all(t.purpose for t in result.tables)
    assert result.modules["AR"] > 10
    assert "Narrow with module=" in discovery.tables_text(result)


def test_list_tables_by_module(emp: Catalog) -> None:
    result = discovery.list_tables(emp, module="ar")
    assert result.module == "AR"
    assert {t.module for t in result.tables} == {"AR"}
    assert not any(t.work for t in result.tables)
    with_work = discovery.list_tables(emp, module="AR", include_work=True)
    assert with_work.total > result.total
    no_addons = discovery.list_tables(emp, include_third_party=False, limit=2000)
    assert not any(t.third_party for t in no_addons.tables)


def test_list_tables_for_a_missing_module(setup: tuple[Runtime, FakeExecutor]) -> None:
    runtime = setup[0]
    pcc = runtime.catalog(runtime.resolve_company("PCC"))[0]
    with pytest.raises(SageError) as raised:
        discovery.list_tables(pcc, module="SO")
    assert raised.value.code == ErrorCode.MODULE_NOT_INSTALLED
    assert raised.value.choices is not None
    assert "AP" in raised.value.choices


def test_describe_table(setup: tuple[Runtime, FakeExecutor], emp: Catalog) -> None:
    runtime = setup[0]
    table = discovery.find_table(emp, "ar_openinvoice", runtime)
    detail = discovery.describe_table(emp, table)
    assert detail.table == "AR_OpenInvoice"
    assert detail.key == ["ARDivisionNo", "CustomerNo", "InvoiceNo", "InvoiceType"]
    assert detail.default_filters == ["Balance <> 0"]
    assert detail.columns_shown < detail.columns_total
    invoice_type = next(c for c in detail.columns if c.name == "InvoiceType")
    assert invoice_type.values is not None
    assert invoice_type.values["CM"] == "Credit memo"
    customer = next(j for j in detail.joins if j.name == "customer")
    assert customer.on == (
        "AR_OpenInvoice.ARDivisionNo = AR_Customer.ARDivisionNo AND AR_OpenInvoice.CustomerNo = AR_Customer.CustomerNo"
    )
    full = discovery.describe_table(emp, table, all_columns=True)
    assert full.columns_shown == full.columns_total
    text = discovery.table_text(detail)
    assert "all_columns=true lists the rest" in text
    assert "IN=Invoice" in text


def test_describe_masked_and_third_party_columns(setup: tuple[Runtime, FakeExecutor], emp: Catalog) -> None:
    vendor = discovery.describe_table(emp, discovery.find_table(emp, "AP_Vendor", setup[0]), all_columns=True)
    ssn = next(c for c in vendor.columns if c.name == "TaxpayerIDSocialSecurityNo")
    assert ssn.masked
    assert "masked" in ssn.tags


@pytest.mark.parametrize(
    ("name", "code", "choice"),
    [
        ("CI_ItemWarehouse", ErrorCode.UNKNOWN_TABLE, "IM_ItemWarehouse"),
        ("AR_Customers", ErrorCode.UNKNOWN_TABLE, "AR_Customer"),
        ("AR_CustomerCreditCard", ErrorCode.TABLE_DENIED, None),
    ],
)
def test_find_table_errors(
    setup: tuple[Runtime, FakeExecutor], emp: Catalog, name: str, code: ErrorCode, choice: str | None
) -> None:
    with pytest.raises(SageError) as raised:
        discovery.find_table(emp, name, setup[0])
    assert raised.value.code == code
    if choice:
        assert raised.value.choices is not None
        assert raised.value.choices[0] == choice
    else:
        assert "*CreditCard*" in (raised.value.hint or "")


def test_search_catalog(emp: Catalog) -> None:
    result = discovery.search_catalog(emp, "backorder quantity", limit=30)
    hits = {(h.table, h.column) for h in result.hits}
    assert ("SO_SalesOrderDetail", "QuantityBackordered") in hits
    column = next(h for h in result.hits if h.column == "QuantityBackordered")
    assert (column.type, column.semantic) == ("decimal(15,6)", "quantity")
    assert "QuantityBackordered" in discovery.search_text(result)
    assert "Nothing in company EMP matches" in discovery.search_text(discovery.search_catalog(emp, "zzqx"))


def test_set_company_and_refresh(tmp_path_factory: pytest.TempPathFactory) -> None:
    runtime, _ = fake_runtime(tmp_path_factory.mktemp("home"))
    active = discovery.set_company(runtime, "pcc")
    assert (active.active, active.name, active.previous) == ("PCC", "Placeholder Capital Co.", None)
    assert discovery.set_company(runtime, "EMP").previous == "PCC"
    refresh = discovery.refresh_catalog(runtime, runtime.resolve_company("EMP"))
    assert refresh.status in ("building", "ready")
    assert "previous catalog answers" in refresh.message


def test_check_connection(setup: tuple[Runtime, FakeExecutor]) -> None:
    runtime, fake = setup
    result = discovery.check_connection(runtime, runtime.resolve_company("EMP"))
    assert (result.connect_ms, result.query_ms, result.database) == (3, 2, "MAS_EMP")
    assert fake.sent[-1][0] == "SELECT TOP 1 1 AS [ok] FROM [dbo].[AP_Options]"
