"""The reconciliation harness itself (tests/reconciliation.py), on emulated results: the M3 gate's
comparison must catch a single cent before it is trusted against Sage's reports on the Windows runner."""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from sage100_mcp.runtime import Runtime
from sage100_mcp.views.api import run_named
from sage100_mcp.views.engine import ViewResult
from tests import emulator
from tests.emulator.conftest import install_at, providex_runtime
from tests.reconciliation import Golden, amount, differences, load_goldens

ARGS = {"as_of": "2024-12-31"}
KEY = ("ARDivisionNo", "CustomerNo")
COMPARE = ("current", "days_1_15", "days_16_30", "days_31_60", "days_61_plus", "total")


@pytest.fixture(scope="module")
def aging(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ViewResult]:
    tmp = tmp_path_factory.mktemp("px")
    install = install_at(tmp, "pxplus_full")
    runtime: Runtime = providex_runtime(tmp / "home", install)
    emulator.INSTALLS[install.directory.lower()] = install
    yield run_named(runtime, runtime.resolve_company("EMP"), "ar_aging", ARGS)
    runtime.close()


def sage_print(value: str) -> str:
    """How a Sage report prints an amount: thousands separators, negatives in parentheses."""
    number = Decimal(value)
    text = f"{abs(number):,.2f}"
    return f"({text})" if number < 0 else text


def write_golden(directory: Path, result: ViewResult, change: Callable[[list[dict[str, str]]], object]) -> None:
    names = [c.name for c in result.columns]
    rows = [{k: str(row[names.index(k)]) for k in KEY} | {c: sage_print(str(row[names.index(c)])) for c in COMPARE}
            for row in result.rows]  # fmt: skip
    change(rows)
    with (directory / "ar_aging.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*KEY, *COMPARE])
        writer.writeheader()
        writer.writerows(rows)
    manifest: dict[str, Any] = {
        "company": "EMP",
        "views": {"ar_aging": {"report": "A/R Aged Invoice Report (summary)", "file": "ar_aging.csv", "args": ARGS,
                               "key": list(KEY), "compare": list(COMPARE)}},
    }  # fmt: skip
    (directory / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


def golden_with(tmp_path: Path, result: ViewResult, change: Callable[[list[dict[str, str]]], object]) -> Golden:
    write_golden(tmp_path, result, change)
    return load_goldens(tmp_path)["ar_aging"]


def test_a_matching_report_reconciles(aging: ViewResult, tmp_path: Path) -> None:
    golden = golden_with(tmp_path, aging, lambda rows: None)
    assert (golden.company, golden.args, golden.key) == ("EMP", ARGS, KEY)
    assert differences(aging, golden) == []


def test_one_cent_is_caught(aging: ViewResult, tmp_path: Path) -> None:
    def nudge(rows: list[dict[str, str]]) -> None:
        rows[0]["total"] = sage_print(str(amount(rows[0]["total"]) + Decimal("0.01")))

    found = differences(aging, golden_with(tmp_path, aging, nudge))
    assert len(found) == 2
    assert found[0].startswith(f"00/{aging.rows[0][1]} total: view ")
    assert found[1].endswith("off by -0.01")


def test_a_missing_row_is_caught(aging: ViewResult, tmp_path: Path) -> None:
    found = differences(aging, golden_with(tmp_path, aging, lambda rows: rows.pop()))
    assert any(line.endswith("only in the view") for line in found)
    assert any(line.startswith("total total: ") for line in found)


def test_a_missing_column_is_named(aging: ViewResult, tmp_path: Path) -> None:
    golden = golden_with(tmp_path, aging, lambda rows: None)
    renamed = Golden(**{**golden.__dict__, "compare": ("days_1_30",)})
    assert differences(aging, renamed)[0].startswith("ar_aging has no column days_1_30")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1,234.56", "1234.56"), ("$1,234.56", "1234.56"), ("(1,234.56)", "-1234.56"), ("1,234.56-", "-1234.56"),
     ("-5", "-5"), ("", "0"), ("  ", "0"), (".50", "0.50")],
)  # fmt: skip
def test_amounts_as_sage_prints_them(text: str, expected: str) -> None:
    assert amount(text) == Decimal(expected)


def test_a_bad_amount_is_refused() -> None:
    with pytest.raises(ValueError, match="is not an amount"):
        amount("n/a")
