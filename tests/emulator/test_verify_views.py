"""scripts/verify_views.py on emulated ProvideX installs: the views agree with the figures Sage keeps on
its master records (the test bed stores them as Sage would), and a single cent of disagreement fails."""

from __future__ import annotations

import runpy
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest

from sage100_mcp.runtime import Runtime
from sage100_mcp.views.engine import ViewResult
from tests import emulator
from tests.emulator.conftest import install_at, providex_runtime
from tests.paths import REPO

VERIFY = runpy.run_path(str(REPO / "scripts" / "verify_views.py"), run_name="verify_views")
reconcile: Callable[..., list[Any]] = VERIFY["reconcile"]


@pytest.fixture(scope="module", params=["pxplus_full", "calvin_graham"])
def runtime(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    tmp = tmp_path_factory.mktemp(request.param)
    install = install_at(tmp, request.param)
    rt = providex_runtime(tmp / "home", install)
    rt.install = install  # type: ignore[attr-defined]
    yield rt
    rt.close()


@pytest.fixture(autouse=True)
def _registered(runtime: Runtime) -> None:
    install = runtime.install  # type: ignore[attr-defined]
    emulator.INSTALLS[install.directory.lower()] = install


def test_every_view_agrees_with_sages_own_figures(runtime: Runtime) -> None:
    outcomes = {o.check: o for o in reconcile(runtime, runtime.resolve_company("EMP"))}
    assert all(o.passed for o in outcomes.values()), [o.line() for o in outcomes.values()]
    aging = outcomes["ar_aging vs AR_Customer's stored aging"]
    assert aging.compared == aging.agreed > 25
    assert aging.notes == [
        "1 with invoices changed on or after their aging date",  # C0007, changed on the aging day
        "1 whose stored aging no longer adds up to their open invoices",  # C0011
    ]
    assert outcomes["open_sales_orders vs AR_Customer.OpenOrderAmt"].agreed > 15
    assert outcomes["ap_aging vs AP_Vendor.BalanceDue"].agreed > 10
    assert outcomes["inventory_availability vs CI_Item.TotalQuantityOnHand"].agreed > 20
    assert outcomes["gl_trial_balance in balance, this fiscal year"].agreed == 12


def test_a_cent_of_disagreement_fails(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """A view that put one cent into the wrong bucket for C0004 is named, and nothing else is."""
    real = VERIFY["reconcile"].__globals__["run_named"]

    def shifted(*args: Any, **kwargs: Any) -> ViewResult:
        result: ViewResult = real(*args, **kwargs)
        if result.view == "ar_aging":
            for row in result.rows:
                if row[1] == "C0004":
                    row[4] = format(Decimal(str(row[4])) - Decimal("0.01"), "f")
                    row[5] = format(Decimal(str(row[5])) + Decimal("0.01"), "f")
        return result

    monkeypatch.setitem(VERIFY["reconcile"].__globals__, "run_named", shifted)
    outcomes = reconcile(runtime, runtime.resolve_company("EMP"))
    failed = [o for o in outcomes if not o.passed]
    assert [(o.check, o.failed) for o in failed] == [("ar_aging vs AR_Customer's stored aging", ["00-C0004"])]
    assert failed[0].line().startswith("[FAIL] ar_aging vs AR_Customer's stored aging: ")


def test_a_company_without_the_modules_runs_what_it_can(runtime: Runtime) -> None:
    """PCC has AP, CI and GL only (and ages AP by month): the vendor and trial balance checks run."""
    outcomes = reconcile(runtime, runtime.resolve_company("PCC"))
    assert [o.check for o in outcomes] == [
        "ap_aging vs AP_Vendor.BalanceDue",
        "gl_trial_balance in balance, this fiscal year",
    ]
    assert all(o.passed for o in outcomes), [o.line() for o in outcomes]
    assert outcomes[0].agreed == 5
