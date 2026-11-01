"""Premium/ProvideX parity for every canned view (SPEC.md §12, §15.4): the diff the M3 exit gate needs empty.

The SQL Server test bed and the emulator serve the same synthetic rows (tests/premium_testbed.py), so a
view's T-SQL plan and its ProvideX plan (single-table pulls through the planner, then DuckDB) must give
the same columns, rows, totals, parameters and warning codes. Two ProvideX profiles run: one whose driver
takes most SQL natively and one that sends most of the work to the hybrid planner.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sage100_mcp.errors import SageError
from sage100_mcp.runtime import Runtime
from sage100_mcp.views.api import compare_companies, run_named
from sage100_mcp.views.engine import ViewResult
from tests import emulator
from tests.emulator.conftest import install_at, providex_runtime

pytestmark = pytest.mark.premium
DAY = {"as_of": "2024-12-31"}
YEAR = {"from_date": "2024-01-01", "to_date": "2024-12-31"}

# (company, view, arguments): every view, every variant, every filter, and the edge cases of the data.
CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("EMP", "ar_aging", DAY),
    ("EMP", "ar_aging", {**DAY, "detail": "invoice"}),
    ("EMP", "ar_aging", {**DAY, "detail": "invoice", "age_by": "invoice"}),
    ("EMP", "ar_aging", {**DAY, "customer": "C0004", "detail": "invoice"}),
    ("EMP", "ar_aging", {**DAY, "customer": "00-C0013"}),
    ("EMP", "ar_aging", {**DAY, "division": "00", "min_balance": "1000"}),
    ("EMP", "ar_aging", {"as_of": "2024-07-01", "detail": "invoice", "min_balance": "2500"}),
    ("EMP", "ap_aging", DAY),
    ("EMP", "ap_aging", {**DAY, "detail": "invoice"}),
    ("EMP", "ap_aging", {**DAY, "detail": "invoice", "include_on_hold": False}),
    ("EMP", "ap_aging", {**DAY, "age_by": "invoice", "vendor": "V000002"}),
    ("PCC", "ap_aging", DAY),
    ("PCC", "ap_aging", {**DAY, "detail": "invoice"}),
    ("EMP", "open_sales_orders", {}),
    ("EMP", "open_sales_orders", {"include_misc": True}),
    ("EMP", "open_sales_orders", {"lines": False, "order_types": ["S", "B", "Q", "M", "R"]}),
    ("EMP", "open_sales_orders", {"promise_before": "2024-11-15", "warehouse": "000"}),
    ("EMP", "open_sales_orders", {"customer": "C0002", "lines": False}),
    ("EMP", "open_purchase_orders", {}),
    ("EMP", "open_purchase_orders", {"lines": False}),
    ("EMP", "open_purchase_orders", {"required_before": "2024-12-31", "warehouse": "000"}),
    ("EMP", "inventory_availability", {}),
    ("EMP", "inventory_availability", {"only_short": True}),
    ("EMP", "inventory_availability", {"item": "ITEM-00"}),
    ("EMP", "inventory_availability", {"item": "ITEM_00", "warehouse": "000"}),  # _ is not a wildcard
    ("EMP", "gl_trial_balance", {"fiscal_year": "2024", "period": "12"}),
    ("EMP", "gl_trial_balance", {"fiscal_year": "2024", "period": "1"}),
    ("EMP", "gl_trial_balance", {"fiscal_year": "2023", "period": "06", "include_zero": True}),
    ("EMP", "gl_trial_balance", {"fiscal_year": "2030", "period": "3"}),
    ("EMP", "sales_history", YEAR),
    ("EMP", "sales_history", {**YEAR, "group_by": "item"}),
    ("EMP", "sales_history", {**YEAR, "group_by": "salesperson"}),
    ("EMP", "sales_history", {**YEAR, "group_by": "product_line"}),
    ("EMP", "sales_history", {"from_date": "2023-01-01", "to_date": "2024-12-31", "group_by": "month"}),
    ("EMP", "sales_history", {"from_date": "2024-10-01", "to_date": "2024-10-31"}),
    ("EMP", "customer_lookup", {"query": "cafe"}),
    ("EMP", "customer_lookup", {"query": "C00"}),
    ("EMP", "customer_lookup", {"query": "o'brien"}),
    ("EMP", "vendor_lookup", {"query": "V0000"}),
    ("EMP", "item_lookup", {"query": "a"}),
    ("PCC", "ar_aging", DAY),  # no AR module: both refuse the same way
]
EMPTY = [  # cases whose right answer is no rows
    ("EMP", "inventory_availability", {"item": "ITEM_00", "warehouse": "000"}),
    ("EMP", "gl_trial_balance", {"fiscal_year": "2030", "period": "3"}),
]
COMPARED: list[tuple[str, dict[str, Any]]] = [
    ("ap_aging", DAY),
    ("gl_trial_balance", {"fiscal_year": "2024", "period": "12"}),
    ("vendor_lookup", {"query": "vendor"}),
]


@pytest.fixture(scope="module", params=["pxplus_full", "calvin_graham"])
def providex(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    tmp = tmp_path_factory.mktemp(request.param)
    install = install_at(tmp, request.param)
    rt = providex_runtime(tmp / "home", install)
    rt.install = install  # type: ignore[attr-defined]
    yield rt
    rt.close()


@pytest.fixture(autouse=True)
def _registered(providex: Runtime) -> None:
    install = providex.install  # type: ignore[attr-defined]
    emulator.INSTALLS[install.directory.lower()] = install


def shape(produce: Any) -> dict[str, Any]:
    """What must match: everything but timings, the SQL each plan sends and the backend's name."""
    try:
        result: ViewResult = produce()
    except SageError as error:
        return {"error": error.code, "choices": error.choices}
    return {
        "columns": [c.model_dump() for c in result.columns],
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "totals": result.totals,
        "parameters": result.parameters,
        "warnings": [w.split(":")[0] for w in result.warnings],
    }


@pytest.mark.parametrize(("company", "name", "args"), CASES, ids=[f"{c}-{n}-{i}" for i, (c, n, _) in enumerate(CASES)])
def test_view_parity(runtime: Runtime, providex: Runtime, company: str, name: str, args: dict[str, Any]) -> None:
    premium = shape(lambda: run_named(runtime, runtime.resolve_company(company), name, args))
    local = shape(lambda: run_named(providex, providex.resolve_company(company), name, args))
    assert local == premium
    if (company, name, args) not in EMPTY:
        assert premium.get("rows") or premium.get("error"), "the case should have data to compare"


@pytest.mark.parametrize(("name", "args"), COMPARED, ids=[n for n, _ in COMPARED])
def test_compare_companies_parity(runtime: Runtime, providex: Runtime, name: str, args: dict[str, Any]) -> None:
    premium = shape(lambda: compare_companies(runtime, name, args, ["EMP", "PCC"]))
    local = shape(lambda: compare_companies(providex, name, args, ["EMP", "PCC"]))
    assert local == premium
