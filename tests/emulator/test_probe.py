"""The capability probe (SPEC.md §5.3): probing a driver that behaves like profile X yields profile X."""

from __future__ import annotations

import pytest

from sage100_mcp.backends.providex_probe import CHECKED_FEATURES, probe_table_candidates
from sage100_mcp.capabilities import FEATURES, FUNCTIONS, KEYWORDS
from tests import emulator
from tests.emulator.conftest import Installer, Maker
from tests.providex_fixture import PROFILE_NAMES, custom, profile


def test_every_feature_has_a_check() -> None:
    assert set(CHECKED_FEATURES) == set(FEATURES)


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_probing_reproduces_each_profile(name: str, make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install(name))
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    found, expected = executor.capabilities, profile(name)
    assert found.source == "probe"
    assert found.table == "AP_Division"
    assert found.features == expected.features
    assert found.functions == expected.functions
    assert set(found.functions) == set(FUNCTIONS)
    assert found.keywords == expected.keywords
    assert set(found.keywords) == set(KEYWORDS)


def test_failures_keep_the_drivers_reason(make_install: Installer, make_executor: Maker) -> None:
    executor = make_executor(make_install("calvin_graham"))
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    notes = executor.capabilities.notes
    assert "Unexpected extra token: OR" in notes["or"]
    assert "Expected lexical element not found" in notes["fn:UCASE"]
    assert "[ProvideX]" not in notes["or"]  # vendor prefixes trimmed


@pytest.mark.parametrize(
    ("change", "feature"),
    [
        ({"compound_and": False}, "compound_and"),
        ({"order_by_desc": False}, "order_by_desc"),
        ({"group_by_expression": True}, "group_by_expression"),
        ({"fn_UCASE": False}, "fn:UCASE"),
    ],
)
def test_single_feature_changes_are_seen(
    change: dict[str, bool], feature: str, make_install: Installer, make_executor: Maker
) -> None:
    caps = custom("pxplus_full", **change)
    executor = make_executor(make_install(caps))
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    found = executor.capabilities
    if feature.startswith("fn:"):
        assert found.functions[feature[3:]] == caps.functions[feature[3:]]
    else:
        assert found.features[feature] == caps.features[feature]
    assert found.features == caps.features


def test_a_driver_that_ignores_limit_fails_the_limit_check(
    make_install: Installer, make_executor: Maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing means right answers, not just no error: LIMIT 1 must return one row."""
    original = emulator._Translator.translate

    def ignore_limit(self: emulator._Translator, sql: str) -> tuple[str, set[str]]:
        duck, tables = original(self, sql)
        return duck.replace(" LIMIT 1", ""), tables

    monkeypatch.setattr(emulator._Translator, "translate", ignore_limit)
    install = make_install("all_pass")
    install.companies["EMP"].rows["AP_Division"] = [
        {"APDivisionNo": f"0{i}", "APDivisionDesc": f"Division {i}"} for i in range(3)
    ]
    executor = make_executor(install)
    executor.query("EMP", "SELECT APDivisionNo FROM AP_Division", max_rows=1, timeout_s=10)
    found = executor.capabilities
    assert not found.supports("limit")
    assert not found.supports("top")
    assert found.supports("or")
    assert "returned 2 rows" in found.notes["limit"]


def test_the_probe_uses_the_first_table_with_rows(make_install: Installer, make_executor: Maker) -> None:
    """ABC on the test bed has no divisions, accounts or items; its customer table has a row."""
    executor = make_executor(make_install("pxplus_full"), companies={"default": "ABC"})
    executor.query("ABC", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert executor.capabilities.table == "AR_Customer"
    assert executor.capabilities.features == profile("pxplus_full").features


def test_without_any_rows_the_baseline_stays(make_install: Installer, make_executor: Maker) -> None:
    install = make_install("all_pass")
    install.companies["ABC"].rows = {}
    executor = make_executor(install, companies={"default": "ABC"})
    executor.query("ABC", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=10)
    assert executor.capabilities.source == "baseline"


def test_probe_tables_are_small_ones_first() -> None:
    assert [table for table, _ in probe_table_candidates()][:4] == [
        "AP_Division",
        "AR_Division",
        "GL_Account",
        "CI_Item",
    ]
