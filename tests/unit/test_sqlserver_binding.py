"""How the SQL Server backend binds parameters (docs/decisions.md D59): Sage keys are varchar, so strings
go as varchar wherever Sage's code page holds them; tests/premium checks the plan really seeks."""

from __future__ import annotations

from decimal import Decimal

from sage100_mcp.backends.sqlserver import VARCHAR_SIZE, input_sizes

SQL_VARCHAR = 12  # pyodbc.SQL_VARCHAR, without importing pyodbc here
VARCHAR = (SQL_VARCHAR, VARCHAR_SIZE, 0)


def test_strings_bind_as_varchar_and_others_as_before() -> None:
    assert input_sizes(["00", "C0001", Decimal("1.5"), 3, None], SQL_VARCHAR) == [VARCHAR, VARCHAR, None, None, None]


def test_text_outside_the_code_page_stays_nvarchar() -> None:
    assert input_sizes(["Müller & Søn", "Жук", "x" * (VARCHAR_SIZE + 1)], SQL_VARCHAR) == [VARCHAR, None, None]


def test_no_strings_means_no_override() -> None:
    assert input_sizes([], SQL_VARCHAR) is None
    assert input_sizes([1, Decimal("2")], SQL_VARCHAR) is None
