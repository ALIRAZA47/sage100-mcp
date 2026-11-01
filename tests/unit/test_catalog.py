from __future__ import annotations

from pathlib import Path

import pytest

from sage100_mcp.catalog.builder import CatalogStore, LiveLayer, RawColumn, RawTable, assemble, is_work_table
from sage100_mcp.catalog.model import Catalog, infer_semantic
from sage100_mcp.catalog.overlay import load_overlay
from sage100_mcp.catalog.search import search, words
from sage100_mcp.config import ConfigError
from sage100_mcp.policy import load_policy
from tests.premium_fixture import column, live_layer, schema, table

OVERLAY = load_overlay()
POLICY = load_policy()


@pytest.fixture(scope="module")
def emp() -> Catalog:
    return assemble(live_layer("MAS_EMP"), OVERLAY, POLICY)


@pytest.fixture(scope="module")
def pcc() -> Catalog:
    return assemble(live_layer("MAS_PCC"), OVERLAY, POLICY)


# --- the overlay against the pinned MAS_EMP schema -----------------------------------------


def test_overlay_names_only_real_tables_and_columns() -> None:
    """Every key, core column, join and semantic in the overlay exists in MAS_EMP."""
    tables = {t["name"].lower(): t for t in schema("MAS_EMP")["tables"]}
    problems = []
    for name, note in OVERLAY.tables.items():
        entry = tables.get(name.lower())
        if entry is None:
            problems.append(f"{name}: not in MAS_EMP")
            continue
        if entry["name"] != name:
            problems.append(f"{name}: MAS_EMP spells it {entry['name']}")
        columns = {c[0].lower() for c in entry.get("columns", [])}
        if not columns:
            problems.append(f"{name}: no pinned columns to check against")
            continue
        for pinned in [*note.key, *note.core, *note.semantics]:
            if pinned.lower() not in columns:
                problems.append(f"{name}.{pinned}: no such column")
        for join_name, join in note.joins.items():
            other = tables.get(join.table.lower())
            other_columns = {c[0].lower() for c in other.get("columns", [])} if other else set()
            for here, there in join.pairs():
                if here.lower() not in columns:
                    problems.append(f"{name} join {join_name}: no column {here}")
                if other is None or there.lower() not in other_columns:
                    problems.append(f"{name} join {join_name}: no column {join.table}.{there}")
    assert not problems, "\n".join(problems)


def test_overlay_covers_the_core_tables() -> None:
    assert len(OVERLAY.tables) >= 40


def test_bad_overlay_is_reported() -> None:
    with pytest.raises(ConfigError, match=r"tables\.AR_Customer\.purpose"):
        load_overlay("version: 1\ntables:\n  AR_Customer:\n    key: [CustomerNo]\n")


# --- assembling MAS_EMP ----------------------------------------------------------------------


def test_policy_hides_credit_card_tables(emp: Catalog) -> None:
    assert len(emp.tables) == 908 - 5
    assert emp.table("AR_CustomerCreditCard") is None
    assert emp.table("ar_customer") is not None


def test_work_and_third_party_tables(emp: Catalog) -> None:
    work = [t for t in emp.tables.values() if t.work]
    assert len(work) > 300
    assert is_work_table("AR_InvoiceWrk")
    assert is_work_table("AR_CashReceiptsRestart")
    assert not is_work_table("AR_Customer")
    third = {t.name for t in emp.tables.values() if t.third_party}
    assert {"AR234_AVA_UseCode", "BC_140_WA_PickingWrk", "CI_519LabelEntryDetail", "MB_BinLocation"} <= third
    assert "AR_Customer" not in third


def test_columns_are_tagged(emp: Catalog) -> None:
    assert column(emp, "AP_Vendor", "TaxpayerIDSocialSecurityNo").masked
    assert not column(emp, "AP_Vendor", "VendorName").masked
    assert "udf" in column(emp, "SO_SalesOrderHeader", "UDF_CARIER_NOTIFY_BEFORE_DEL").tags
    assert "third_party" in column(emp, "SO_SalesOrderDetail", "D519SF_QuantityPicked").tags
    assert "third_party" in column(emp, "SO_SalesOrderDetail", "AVA234_ShipToCode").tags
    assert "third_party" in column(emp, "AR_Customer", "AVA_234_CustomerID").tags
    assert "audit" in column(emp, "AR_Customer", "DateUpdated").tags
    assert not column(emp, "CI_Item", "RoutingNo").masked  # mask exception


def test_overlay_is_applied(emp: Catalog) -> None:
    open_ar = table(emp, "AR_OpenInvoice")
    assert open_ar.purpose
    assert open_ar.key == ("ARDivisionNo", "CustomerNo", "InvoiceNo", "InvoiceType")
    assert open_ar.key_source == "overlay"
    assert open_ar.default_filters == ("Balance <> 0",)
    assert (column(emp, "AR_OpenInvoice", "InvoiceType").enum or {})["PP"] == "Prepayment"
    assert column(emp, "AR_OpenInvoice", "Balance").semantic == "money"
    assert column(emp, "AR_OpenInvoice", "InvoiceDate").semantic == "date"
    customer_join = next(j for j in open_ar.joins if j.name == "customer")
    assert customer_join.on == (("ARDivisionNo", "ARDivisionNo"), ("CustomerNo", "CustomerNo"))
    assert table(emp, "AR_InvoiceHistoryHeader").requires_filter
    assert emp.drift == []


def test_default_columns_are_the_core_ones(emp: Catalog) -> None:
    header = table(emp, "SO_SalesOrderHeader")
    assert len(header.columns) == 143
    shown = [c.name for c in header.default_columns()]
    assert "SalesOrderNo" in shown
    assert "UDF_CARIER_NOTIFY_BEFORE_DEL" not in shown
    assert len(shown) < 30


def test_financials_only_company(pcc: Catalog) -> None:
    modules = {t.module for t in pcc.tables.values()}
    assert {"AP", "GL", "CI", "FL"} <= modules
    assert "SO" not in modules
    assert "IM" not in modules
    assert table(pcc, "AP_Vendor").purpose  # overlay applies where the table exists
    assert pcc.drift == []


# --- search and suggestions --------------------------------------------------------------------


def test_backorder_quantity_finds_all_three_modules(emp: Catalog) -> None:
    found = {(h.table, h.column) for h in search(emp, "backorder quantity", limit=30)}
    assert ("SO_SalesOrderDetail", "QuantityBackordered") in found
    assert ("PO_PurchaseOrderDetail", "QuantityBackordered") in found
    assert ("IM_ItemWarehouse", "QuantityOnBackOrder") in found


def test_search_ranks_curated_tables_first(emp: Catalog) -> None:
    hits = search(emp, "customer name")
    assert (hits[0].table, hits[0].column) == ("AR_Customer", "CustomerName")
    assert search(emp, "") == []
    assert all(not table(emp, h.table).work for h in search(emp, "invoice"))
    assert (search(emp, "vendor")[0].table, search(emp, "vendor")[0].column) == ("AP_Vendor", None)
    top = {(h.table, h.column) for h in search(emp, "open invoice balance", limit=5)}
    assert {("AR_OpenInvoice", "Balance"), ("AP_OpenInvoice", "Balance")} <= top


def test_did_you_mean(emp: Catalog) -> None:
    assert emp.suggest("CI_ItemWarehouse")[0] == "IM_ItemWarehouse"
    assert emp.suggest("AR_Customers")[0] == "AR_Customer"
    assert emp.suggest_column(table(emp, "AR_Customer"), "CustName")  # some suggestion


def test_words() -> None:
    assert words("QuantityOnBackOrder") == ["quantity", "on", "back", "order"]
    assert words("AR_OpenInvoice") == ["ar", "open", "invoice"]
    assert words("D519SF_QuantityPicked") == ["d", "519", "sf", "quantity", "picked"]


@pytest.mark.parametrize(
    ("name", "sql_type", "semantic"),
    [
        ("InvoiceDate", "datetime", "date"),
        ("Balance", "decimal(13,2)", "money"),
        ("QuantityOnHand", "decimal(15,6)", "quantity"),
        ("CommissionRate", "decimal(8,3)", "rate"),
        ("AgingCategory1", "decimal(3,0)", "number"),
        ("AgingCategory1", "decimal(13,2)", "money"),
        ("TimeCreated", "varchar(8)", "time"),
        ("AccountKey", "varchar(9)", "key"),
        ("CustomerNo", "varchar(20)", "code"),
        ("CustomerName", "varchar(50)", "text"),
    ],
)
def test_infer_semantic(name: str, sql_type: str, semantic: str) -> None:
    assert infer_semantic(name, sql_type) == semantic


# --- cache -----------------------------------------------------------------------------------------


def test_live_layer_round_trips(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path)
    live = LiveLayer(
        "EMP",
        "premium",
        {"AR": "7.3"},
        "2026-10-29T00:00:00Z",
        (RawTable("AR_Customer", (RawColumn("CustomerNo", "varchar(20)", False),), ("CustomerNo",), 12),),
    )
    store.save(live)
    assert store.load("EMP") == live
    assert store.load("PCC") is None
    store.path("EMP").write_text('{"format": 999}', encoding="utf-8")
    assert store.load("EMP") is None


# --- policy ------------------------------------------------------------------------------------------


def test_policy_rules() -> None:
    rule = POLICY.denied_by("PR_EmployeeMaster")
    assert rule is not None
    assert rule.module == "PR"
    assert POLICY.denied_by("SY_UserSecurity") is not None
    assert POLICY.denied_by("AR_Customer") is None
    payroll = POLICY.model_copy(update={"allow_modules": ["PR"]})
    assert payroll.denied_by("PR_EmployeeMaster") is None
    assert payroll.denied_by("AR_CustomerCreditCard") is not None  # card data stays denied


def test_payroll_opt_in_reaches_the_catalog() -> None:
    live = LiveLayer(
        "ABC", "test", {}, "", (RawTable("PR_Employee", (RawColumn("SocialSecurityNo", "varchar(9)", False),)),)
    )
    assert assemble(live, OVERLAY, POLICY).table("PR_Employee") is None
    payroll = POLICY.model_copy(update={"allow_modules": ["PR"]})
    assert column(assemble(live, OVERLAY, payroll), "PR_Employee", "SocialSecurityNo").masked  # still masked


def test_bad_policy_file(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("deny_tables: []\nmask_columns: '(unclosed'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mask_columns"):
        load_policy(path)
    with pytest.raises(ConfigError, match="cannot read"):
        load_policy(tmp_path / "missing.yaml")
