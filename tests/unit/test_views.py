"""The canned-view machinery without a backend: definitions, parameters, SQL templates, cells,
aging buckets and the available formula (SPEC.md §12)."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.views.api import describe_views, view_names, view_text
from sage100_mcp.views.engine import ViewResult, _cell, load_views, substitute
from sage100_mcp.views.lookups import LOOKUPS, Lookup, _score, fold
from sage100_mcp.views.model import OutputColumn, ViewDef, bind_params, named, positional, render, split_account
from sage100_mcp.views.rules import _GROUPS, _SUPPLY, PROJECTED, Buckets, _like_escaped, _starts, available_sql

TODAY = date(2024, 12, 31)
VIEWS = load_views()
# What views/rules.py fills in for each view, so every template can be rendered here.
RULE_PARAMS: dict[str, dict[str, Any]] = {
    "ar_aging": {"basis": "due", **Buckets((1, 16, 31, 61)).params()},
    "ap_aging": {"basis": "due", **Buckets((30, 60, 90, 120)).params()},
    "inventory_availability": {"item_pattern": None, "item_escaped": None},
}
FRAGMENTS: dict[str, dict[str, str]] = {
    "ar_aging": {"local:unit": "'day'", "tsql:unit": "day"},
    "ap_aging": {"local:unit": "'day'", "tsql:unit": "day"},
    "inventory_availability": {"available": available_sql("onhand - so"), "projected": available_sql(PROJECTED)},
    "sales_history": {"local:key": _GROUPS["item"][0], "tsql:key": _GROUPS["item"][1], "name": _GROUPS["item"][2]},
}
REQUIRED = {
    "gl_trial_balance": {"fiscal_year": "2024", "period": "12"},
    "sales_history": {"from_date": "2024-01-01", "to_date": "2024-12-31"},
}


def column(type_: str, semantic: str = "money") -> OutputColumn:
    return OutputColumn(name="x", type=type_, semantic=semantic)


# --- definitions -------------------------------------------------------------------------------


def test_seven_views_and_three_lookups() -> None:
    """With compare_companies, the eleven view tools of §11.1."""
    assert sorted(VIEWS) == [
        "ap_aging",
        "ar_aging",
        "gl_trial_balance",
        "inventory_availability",
        "open_purchase_orders",
        "open_sales_orders",
        "sales_history",
    ]
    assert sorted(LOOKUPS) == ["customer_lookup", "item_lookup", "vendor_lookup"]
    assert view_names() == sorted([*VIEWS, *LOOKUPS])


@pytest.mark.parametrize("name", sorted(VIEWS))
def test_every_variant_renders_on_both_plans(name: str) -> None:
    """Each plan's every :param has a value and every {{fragment}} is filled, with options on and off."""
    view = VIEWS[name]
    for flags in (False, True):
        args: dict[str, Any] = dict(REQUIRED.get(name, {}))
        for param_name, spec in view.params.items():
            if flags and spec.type == "bool":
                args[param_name] = spec.default is not True
            if flags and spec.type in ("code", "customer", "vendor") and param_name != "division":
                args[param_name] = "01-ABC" if spec.type in ("customer", "vendor") else "ABC"
            if flags and spec.type == "date" and not spec.required:
                args[param_name] = "2024-06-30"
        params = bind_params(view, args, today=TODAY) | RULE_PARAMS.get(name, {})
        if name == "inventory_availability" and flags:
            params |= {"item_pattern": "ABC%", "item_escaped": "ABC%"}
        for key, variant in view.variants.items():
            for plan, sql in (("local", variant.local), ("tsql", variant.tsql)):
                text = render(substitute(sql, FRAGMENTS.get(name, {}), plan), params)
                assert "{{" not in text, (name, key, plan)
                assert "-- if" not in text, (name, key, plan)
                positional(text, params)  # raises INTERNAL on a parameter without a value
                named(text, params)
        for pull in view.pulls:
            positional(render(pull.sql(params), params), params)


@pytest.mark.parametrize("name", sorted(VIEWS))
def test_both_plans_name_every_output_column_once(name: str) -> None:
    for variant in VIEWS[name].variants.values():
        names = [c.name for c in variant.columns]
        assert len(set(names)) == len(names)
        assert set(variant.totals) <= set(names)
        for column_ in variant.columns:
            assert re.fullmatch(r"varchar\(\d+\)|decimal\(\d+,\d+\)|date|int|bool", column_.type), column_


def test_views_never_reference_site_specific_columns() -> None:
    """CLAUDE.md: UDF_* and D###_/AVA234_ columns are site-specific and never appear in a view."""
    for view in VIEWS.values():
        texts = [p.model_dump_json() for p in view.pulls] + [v.model_dump_json() for v in view.variants.values()]
        assert not re.search(r"\bUDF_|\bD\d{3}[A-Z]*_|\bAVA234_", " ".join(texts)), view.name


def test_open_order_views_leave_out_quotes_and_completed_pos() -> None:
    assert VIEWS["open_sales_orders"].params["order_types"].default == ["S", "B"]
    po = VIEWS["open_purchase_orders"]
    for variant in po.variants.values():
        assert "OrderStatus IN ('N', 'O', 'B', 'C')" in variant.tsql
    assert "OrderStatus IN ('N', 'O', 'B', 'C')" in po.pulls[0].where


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"variants": {"wrong": None}}, "variants"),
        ({"pulls": [{"name": "a", "table": "T", "columns": ["X"]}] * 2}, "pull names repeat"),
        (
            {"pulls": [{"name": "b", "table": "T", "columns": ["X"], "keys_from": {"pull": "a", "column": "X",
                                                                                      "filter": "X"}}]},
            "does not come before",
        ),
        ({"total": "nope"}, "is not an output column"),
    ],
)  # fmt: skip
def test_bad_definitions_are_refused(change: dict[str, Any], message: str) -> None:
    data = VIEWS["open_purchase_orders"].model_dump(by_alias=True)
    if "variants" in change:
        data["variants"] = {"wrong": data["variants"]["true"]}
    if "pulls" in change:
        data["pulls"] = change["pulls"]
    if "total" in change:
        data["variants"]["true"]["totals"] = ["nope"]
    with pytest.raises(ValueError, match=message):
        ViewDef.model_validate(data)


# --- parameters ----------------------------------------------------------------------------------


def test_defaults_and_conversions() -> None:
    params = bind_params(VIEWS["ar_aging"], {"customer": "01-abf", "min_balance": "5"}, today=TODAY)
    assert params["as_of"] == TODAY
    assert params["detail"] == "summary"
    assert params["age_by"] is None
    assert params["min_balance"] == Decimal(5)
    assert (params["customer_division"], params["customer_no"]) == ("01", "ABF")
    orders = bind_params(VIEWS["open_sales_orders"], {"order_types": "s, q,S"}, today=TODAY)
    assert orders["order_types"] == ["S", "Q"]
    assert bind_params(VIEWS["open_sales_orders"], {"order_types": ("B",)}, today=TODAY)["order_types"] == ["B"]
    assert orders["lines"] is True
    assert orders["include_misc"] is False
    gl = bind_params(VIEWS["gl_trial_balance"], {"fiscal_year": 2024, "period": 3}, today=TODAY)
    assert (gl["fiscal_year"], gl["period"]) == ("2024", "03")
    history = bind_params(
        VIEWS["sales_history"], {"from_date": "2024-01-01", "to_date": date(2024, 2, 1), "group_by": "MONTH"},
        today=TODAY,
    )  # fmt: skip
    assert (history["from_date"], history["to_date"], history["group_by"]) == (
        date(2024, 1, 1),
        date(2024, 2, 1),
        "month",
    )
    assert bind_params(VIEWS["ap_aging"], {"include_on_hold": "FALSE"}, today=TODAY)["include_on_hold"] is False


@pytest.mark.parametrize(
    ("view", "args", "fragment"),
    [
        ("ar_aging", {"as_of": "12/31/2024"}, "is not a date"),
        ("ar_aging", {"min_balance": "lots"}, "is not a number"),
        ("ar_aging", {"min_balance": "NaN"}, "finite"),
        ("ar_aging", {"detail": "everything"}, "is not one of summary, invoice"),
        ("ar_aging", {"division": "TOOLONG"}, "is not a valid code"),
        ("ar_aging", {"customer": "a;b"}, "is not a customer or vendor number"),
        ("ar_aging", {"bucket": "1"}, "unknown argument(s) bucket"),
        ("ap_aging", {"include_on_hold": "maybe"}, "is not true or false"),
        ("open_sales_orders", {"order_types": ["S", "X"]}, "X is not among S, B, Q, M, R"),
        ("open_sales_orders", {"order_types": []}, "nothing is not among"),
        ("gl_trial_balance", {"fiscal_year": "24", "period": "1"}, "four-digit fiscal year"),
        ("gl_trial_balance", {"fiscal_year": "2024", "period": "14"}, "fiscal period from 1 to 13"),
        ("gl_trial_balance", {"fiscal_year": "2024"}, "period is required"),
        ("sales_history", {"from_date": "2024-01-01"}, "to_date is required"),
        ("open_purchase_orders", {"item": "x" * 40}, "is not a valid code"),
    ],
)
def test_bad_arguments(view: str, args: dict[str, Any], fragment: str) -> None:
    with pytest.raises(SageError) as raised:
        bind_params(VIEWS[view], args, today=TODAY)
    assert raised.value.code == ErrorCode.PARSE_ERROR
    assert fragment in raised.value.message


@pytest.mark.parametrize(
    ("text", "expected"),
    [("ABF", (None, "ABF")), ("01-abf", ("01", "ABF")), (" 02-AIRWAY ", ("02", "AIRWAY")), ("A-B-C", (None, "A-B-C"))],
)
def test_split_account(text: str, expected: tuple[str | None, str]) -> None:
    assert split_account(text, "customer") == expected


# --- SQL templates -------------------------------------------------------------------------------


def test_render_keeps_or_drops_conditional_lines() -> None:
    sql = "SELECT 1\nWHERE a = :a  -- if a\n  AND b <> 'Y'  -- if not b\nORDER BY 1"
    assert render(sql, {"a": "x", "b": None}) == "SELECT 1\nWHERE a = :a\n  AND b <> 'Y'\nORDER BY 1"
    empties: list[Any] = [None, "", [], False]
    for empty in empties:
        assert render(sql, {"a": empty, "b": True}) == "SELECT 1\nORDER BY 1"


def test_positional_and_named_parameters() -> None:
    sql = "SELECT ':skip' FROM t WHERE x IN (:codes) AND d <= :when AND y = :when"
    params = {"codes": ["S", "B"], "when": date(2024, 1, 1), "skip": "never"}
    text, values = positional(sql, params)
    assert text == "SELECT ':skip' FROM t WHERE x IN (?, ?) AND d <= ? AND y = ?"
    assert values == ["S", "B", date(2024, 1, 1), date(2024, 1, 1)]
    text, used = named(sql, params)
    assert text == "SELECT ':skip' FROM t WHERE x IN ($codes_0, $codes_1) AND d <= $when AND y = $when"
    assert used == {"codes_0": "S", "codes_1": "B", "when": date(2024, 1, 1)}
    assert positional("x IN (:none)", {"none": []}) == ("x IN (NULL)", [])
    with pytest.raises(SageError) as raised:
        positional("WHERE a = :missing", {})
    assert raised.value.code == ErrorCode.INTERNAL


def test_fragments_by_plan() -> None:
    fragments = {"local:unit": "'month'", "tsql:unit": "month", "name": "x"}
    assert substitute("{{unit}} {{name}}", fragments, "local") == "'month' x"
    assert substitute("{{unit}} {{name}}", fragments, "tsql") == "month x"
    with pytest.raises(SageError) as raised:
        substitute("{{other}}", fragments, "tsql")
    assert raised.value.code == ErrorCode.INTERNAL


# --- cells ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "type_", "semantic", "expected"),
    [
        (Decimal("2.345"), "decimal(17,2)", "money", "2.35"),  # half up, never banker's
        (Decimal("-2.345"), "decimal(17,2)", "money", "-2.35"),
        (Decimal("-0.001"), "decimal(17,2)", "money", "0.00"),  # never -0.00
        (Decimal("12"), "decimal(17,2)", "money", "12.00"),
        (Decimal("5.000000"), "decimal(15,6)", "quantity", "5"),
        (Decimal("2.500000"), "decimal(15,6)", "quantity", "2.5"),
        (Decimal("100"), "decimal(15,6)", "quantity", "100"),
        (Decimal("0E-6"), "decimal(15,6)", "quantity", "0"),
        (Decimal("12.500000"), "decimal(15,6)", "money", "12.50"),  # a unit price: cents at least
        (Decimal("12.345600"), "decimal(15,6)", "money", "12.3456"),
        (7.5, "decimal(17,2)", "money", "7.50"),
        (datetime(2024, 3, 1, 0, 0), "date", "date", "2024-03-01"),
        (date(1753, 1, 1), "date", "date", None),
        (None, "date", "date", None),
        (Decimal(3), "int", "number", 3),
        (1, "bool", "flag", True),
        ("ABC   ", "varchar(20)", "code", "ABC"),
    ],
)
def test_cells(value: Any, type_: str, semantic: str, expected: Any) -> None:
    assert _cell(value, column(type_, semantic)) == expected


# --- aging ---------------------------------------------------------------------------------------


def test_bucket_labels_follow_the_options() -> None:
    assert Buckets((1, 16, 31, 61)).labels() == ["current", "days_1_15", "days_16_30", "days_31_60", "days_61_plus"]
    assert Buckets((30, 60, 90, 120)).labels() == [
        "current",
        "days_30_59",
        "days_60_89",
        "days_90_119",
        "days_120_plus",
    ]
    assert Buckets((1, 2, 3, 4), "months").labels() == ["current", "months_1", "months_2", "months_3", "months_4_plus"]
    params = Buckets((1, 16, 31, 61)).params()
    assert (params["c1"], params["c4"], params["label_0"], params["label_4"]) == (1, 61, "current", "days_61_plus")


def _span(label: str, starts: list[int]) -> tuple[float, float]:
    """The ages a label claims: current, days_1_15, days_16, days_61_plus."""
    if label == "current":
        return float("-inf"), starts[0] - 1
    numbers = [int(n) for n in label.split("_")[1:] if n.isdigit()]
    if label.endswith("_plus"):
        return numbers[0], float("inf")
    return numbers[0], numbers[-1]


@given(st.lists(st.integers(min_value=1, max_value=999), min_size=4, max_size=4, unique=True).map(sorted))
def test_every_age_lands_where_its_label_says(starts: list[int]) -> None:
    """The SQL puts an age in the first bucket whose next start it is below; the label must name that span."""
    labels = Buckets((starts[0], starts[1], starts[2], starts[3])).labels()
    for age in {-1, 0, *starts, *(s - 1 for s in starts), starts[3] + 500}:
        index = next((i for i, start in enumerate(starts) if age < start), 4)  # CASE WHEN age < c1 ... ELSE
        low, high = _span(labels[index], starts)
        assert low <= age <= high, (starts, age, labels[index])


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"AgingCategory1": Decimal(1), "AgingCategory2": Decimal(16), "AgingCategory3": Decimal(31),
          "AgingCategory4": Decimal(61)}, (1, 16, 31, 61)),
        ({"AgingCategory1": Decimal(30), "AgingCategory2": Decimal(60), "AgingCategory3": Decimal(90)}, None),
        ({"AgingCategory1": 30, "AgingCategory2": 30, "AgingCategory3": 90, "AgingCategory4": 120}, None),
        ({"AgingCategory1": 0, "AgingCategory2": 30, "AgingCategory3": 90, "AgingCategory4": 120}, None),
        ({"AgingCategory1": 60, "AgingCategory2": 30, "AgingCategory3": 90, "AgingCategory4": 120}, None),
        (None, None),
    ],
)  # fmt: skip
def test_aging_categories_must_be_usable(row: dict[str, Any] | None, expected: tuple[int, ...] | None) -> None:
    assert _starts(row) == expected


# --- inventory -----------------------------------------------------------------------------------


def test_available_formula_sql() -> None:
    assert available_sql("onhand + po - so") == (
        "COALESCE(s.QuantityOnHand, 0) + COALESCE(s.QuantityOnPurchaseOrder, 0) - COALESCE(s.QuantityOnSalesOrder, 0)"
    )
    assert _SUPPLY == {"P": " + po", "W": " + wo", "B": " + po + wo", "N": ""}
    assert _like_escaped("A_B%C!D[") == "A!_B!%C!!D!["


# --- lookups -------------------------------------------------------------------------------------


def test_fold_ignores_case_and_accents() -> None:
    assert fold("Café Ünicode") == fold("CAFE unicode") == "cafe unicode"
    assert fold(None) == ""


def test_lookup_scores() -> None:
    lookup: Lookup = LOOKUPS["customer_lookup"]
    row = {"ARDivisionNo": "00", "CustomerNo": "C0013", "CustomerName": "Café Ünicode Gmbh",
           "EmailAddress": "ap@cafe.example", "TelephoneNo": "(555) 010-2030", "City": "", "State": ""}  # fmt: skip
    assert _score(row, ["c0013"], "c0013", "0013", lookup) == 0
    assert _score(row, ["c001"], "c001", "001", lookup) == 1
    assert _score(row, ["cafe"], "cafe", "", lookup) == 2
    assert _score(row, ["unicode", "gmbh"], "unicode gmbh", "", lookup) == 3
    assert _score(row, ["5550102030"], "5550102030", "5550102030", lookup) == 3  # by phone digits
    assert _score(row, ["cafe", "nowhere"], "cafe nowhere", "", lookup) is None


# --- descriptions --------------------------------------------------------------------------------


def test_views_resource_shows_both_plans() -> None:
    described = describe_views()
    names = [v["name"] for v in described["views"]]
    assert names == sorted(VIEWS)
    aging = next(v for v in described["views"] if v["name"] == "ar_aging")
    assert aging["parameters"]["detail"] == {
        "type": "choice",
        "description": "One row per customer or per invoice",
        "default": "summary",
        "required": False,
        "choices": ["summary", "invoice"],
    }
    assert aging["providex_pulls"][0]["table"] == "AR_OpenInvoice"
    assert "DATEDIFF({{unit}}" in aging["variants"]["summary"]["premium_tsql"]
    assert [lookup["name"] for lookup in described["lookups"]] == list(LOOKUPS)


def test_view_text() -> None:
    result = ViewResult(
        company="EMP", backend="providex", view="ar_aging", version=1, parameters={},
        columns=[], rows=[], row_count=0, truncated=False, totals={"total": "10.00"}, as_of="2024-12-31T00:00:00+00:00",
        elapsed_ms=5, null_dates_coerced=False, sent_sql=[], warnings=["DIVISION_DISABLED: off"],
    )  # fmt: skip
    text = view_text(result)
    assert text.startswith("EMP (providex) ar_aging: 0 rows in 5 ms")
    assert "Totals: total 10.00" in text
    assert "- DIVISION_DISABLED: off" in text
