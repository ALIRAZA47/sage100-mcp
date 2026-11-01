from __future__ import annotations

from sage100_mcp.formatting import text_fields, text_table


def test_table_shape() -> None:
    assert text_table(["CustomerNo", "Balance"], [["ABF", "1250.00"], ["XYZ", None]]) == (
        "| CustomerNo | Balance |\n|---|---|\n| ABF | 1250.00 |\n| XYZ |  |"
    )


def test_cells_cannot_break_the_table() -> None:
    """Sage text is written by people: a pipe or newline in a comment must not add columns or rows."""
    table = text_table(["Comment"], [["line one\nline two | not a column"]])
    assert table.splitlines()[2] == "| line one line two \\| not a column |"


def test_long_cells_are_shortened() -> None:
    cell = text_table(["Text"], [["x" * 500]]).splitlines()[2]
    assert len(cell) < 70
    assert cell.endswith("… |")


def test_row_limit_footer() -> None:
    table = text_table(["n"], [[i] for i in range(60)], limit=50)
    lines = table.splitlines()
    assert len(lines) == 2 + 50 + 1
    assert lines[-1] == "Showing 50 of 60 rows; all rows are in structuredContent."


def test_fields_skip_empty_values_and_indent_continuations() -> None:
    text = text_fields([("a", "1"), ("b", None), ("c", ""), ("d", []), ("e", "x\ny")])
    assert text == "a: 1\ne: x\n  y"
