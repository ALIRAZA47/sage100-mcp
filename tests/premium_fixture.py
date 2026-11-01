"""The pinned MAS_EMP / MAS_PCC schemas (structure only) as live layers for catalog tests."""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from sage100_mcp.catalog.builder import LiveLayer, RawColumn, RawTable
from sage100_mcp.catalog.model import Catalog, Column, Table
from tests.paths import REPO

FIXTURES = REPO / "tests" / "fixtures" / "premium"


@cache
def schema(database: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{database.lower()}.json").read_text(encoding="utf-8"))
    return data


def columns_of(entry: dict[str, Any]) -> tuple[RawColumn, ...]:
    """Real columns for core tables; generic stand-ins (Field1..n) where only the count was pinned."""
    if "columns" in entry:
        return tuple(RawColumn(name, sql_type, nullable) for name, sql_type, nullable in entry["columns"])
    return tuple(RawColumn(f"Field{i}", "varchar(10)", True) for i in range(1, entry["column_count"] + 1))


def live_layer(database: str, data_version: str = "7.3") -> LiveLayer:
    data = schema(database)
    tables = tuple(RawTable(t["name"], columns_of(t)) for t in data["tables"])
    return LiveLayer(data["company"], "test", {"AR": data_version}, "2026-10-29T00:00:00Z", tables)


def table(catalog: Catalog, name: str) -> Table:
    found = catalog.table(name)
    assert found is not None, f"no table {name}"
    return found


def column(catalog: Catalog, table_name: str, name: str) -> Column:
    found = table(catalog, table_name).column(name)
    assert found is not None, f"no column {table_name}.{name}"
    return found
