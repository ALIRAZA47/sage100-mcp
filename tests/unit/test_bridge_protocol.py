"""The 32-bit driver helper's wire protocol (docs/decisions.md D67): values, messages and the helper's reach."""

from __future__ import annotations

import ast
import io
import sys
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import IO, Any, cast

import pytest

from sage100_mcp.backends.base import description_type
from sage100_mcp.bridge import SCRIPT
from sage100_mcp.bridge.client import Row, row_type
from sage100_mcp.bridge.helper import MAX_MESSAGE, ProtocolError, encode, read_message

VALUES: list[Any] = [
    None, True, False, 0, -1, 2**70, 1.5, float("inf"), "", "Grüße, 東京", "tab\tquote\"backslash\\", "\ud800",
    Decimal("1234567890123.45"), Decimal("-0.00"), Decimal("1E+3"), Decimal("NaN"),
    date(2026, 10, 31), datetime(2026, 10, 31, 23, 59, 59, 999999), datetime(1753, 1, 1), time(12, 30, 5, 250),
    b"", b"\x00\xff", uuid.UUID("12345678-1234-5678-1234-567812345678"),
]  # fmt: skip


def round_trip(message: dict[str, Any]) -> dict[str, Any]:
    found = read_message(io.BytesIO(encode(message)))
    assert found is not None
    return found


@pytest.mark.parametrize("value", VALUES, ids=[f"{type(v).__name__}-{i}" for i, v in enumerate(VALUES)])
def test_values_cross_with_their_type(value: Any) -> None:
    back = round_trip({"id": 1, "rows": [[value]]})["rows"][0][0]
    assert type(back) is type(value)
    if isinstance(value, Decimal):
        assert str(back) == str(value)  # the exponent and the sign of zero too
    else:
        assert back == value


def test_bytearray_and_memoryview_cross_as_bytes() -> None:
    assert round_trip({"v": [bytearray(b"ab"), memoryview(b"cd")]})["v"] == [b"ab", b"cd"]


def test_descriptions_keep_their_type_codes() -> None:
    """description_type reads the Python type pyodbc puts in a description, so it must survive the trip."""
    description = [
        ("Balance", Decimal, None, 13, 13, 2, True),
        ("CustomerNo", str, None, 20, 20, 0, True),
        ("InvoiceDate", date, None, 10, 10, 0, True),
        ("Stamp", datetime, None, 23, 23, 3, True),
        ("Count", int, None, 10, 10, 0, False),
        ("Flag", bool, None, 1, 1, 0, True),
        ("Blob", bytearray, None, 16, 16, 0, True),
        ("Id", uuid.UUID, None, 36, 36, 0, True),
    ]
    back = [tuple(column) for column in round_trip({"description": description})["description"]]
    assert back == description
    assert [description_type(column) for column in back] == [
        "decimal(13,2)", "varchar(20)", "date", "datetime", "int", "bit", "varbinary", "uuid",
    ]  # fmt: skip


def test_a_type_this_side_lacks_keeps_its_name() -> None:
    sql_variant = type("SQLVariant", (), {})
    (column,) = round_trip({"description": [("v", sql_variant, None, 0, 0, 0, True)]})["description"]
    assert column[1].__name__ == "SQLVariant"
    assert description_type(column) == "sqlvariant"


def test_objects_that_are_not_tags_pass_through() -> None:
    message = {"id": 7, "result": {"cursor": 3, "nested": {"$d": "1.5", "other": 1}}}
    assert round_trip(message) == message


def test_a_value_without_a_wire_form_is_refused() -> None:
    with pytest.raises(TypeError, match="no wire form"):
        encode({"params": [object()]})


def test_messages_are_ascii_json_behind_a_length() -> None:
    data = encode({"id": 1, "op": "execute", "sql": "SELECT 'é'"})
    assert int.from_bytes(data[:4], "big") == len(data) - 4
    assert data[4:].isascii()


def test_reading_stops_cleanly_between_messages() -> None:
    stream = io.BytesIO(encode({"id": 1}) + encode({"id": 2}))
    assert [read_message(stream), read_message(stream), read_message(stream)] == [{"id": 1}, {"id": 2}, None]


class Trickle(io.RawIOBase):
    """A pipe that hands over one byte per read."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if not self.data:
            return 0
        buffer[0], self.data = self.data[0], self.data[1:]
        return 1


def test_short_reads_are_joined() -> None:
    stream = cast(IO[bytes], Trickle(encode({"id": 9, "rows": [["x" * 50]]})))
    assert read_message(stream) == {"id": 9, "rows": [["x" * 50]]}


@pytest.mark.parametrize(
    "data",
    [
        encode({"id": 1})[:-1],  # cut inside the body
        b"\x00\x00",  # cut inside the length
        b"\x00\x00\x00\x02[]",  # not an object
        b"\x00\x00\x00\x03{x}",  # not JSON
        b"\x00\x00\x00\x02\xff\xfe",  # not ASCII
        len(b'{"$d":"one"}').to_bytes(4, "big") + b'{"$d":"one"}',  # a tag whose value doesn't parse
        (MAX_MESSAGE + 1).to_bytes(4, "big"),  # too long to be ours
    ],
)
def test_broken_input_is_a_protocol_error(data: bytes) -> None:
    with pytest.raises(ProtocolError):
        read_message(io.BytesIO(data))


def test_rows_answer_by_name_like_pyodbc_rows() -> None:
    row = row_type(("table_name", "column_name", "table_name"))(("AR_Customer", "CustomerNo", "x"))
    assert isinstance(row, Row)
    assert isinstance(row, tuple)
    assert tuple(row) == ("AR_Customer", "CustomerNo", "x")
    assert (row.table_name, row.column_name, row[1]) == ("AR_Customer", "CustomerNo", "CustomerNo")
    with pytest.raises(AttributeError):
        _ = row.nope
    assert row_type(("a",)) is row_type(("a",))  # one class per description


# --- the helper runs on its own: the standard library and pyodbc, on a 32-bit CPython 3.9 or later ----------


def test_the_helper_imports_only_the_standard_library_and_pyodbc() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported - set(sys.stdlib_module_names) == {"pyodbc"}


def test_the_helper_parses_as_python_3_9() -> None:
    ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 9))


def test_the_helper_evaluates_no_new_syntax_at_run_time() -> None:
    """Annotations are lazy there; anything evaluated must not use X | Y, which 3.9 lacks."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    annotations = {
        id(node)
        for owner in ast.walk(tree)
        for node in ast.walk(getattr(owner, "annotation", None) or getattr(owner, "returns", None) or ast.Pass())
        if isinstance(owner, ast.arg | ast.AnnAssign | ast.FunctionDef)
    }
    unions = [node for node in ast.walk(tree) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)]
    assert [ast.unparse(node) for node in unions if id(node) not in annotations] == []
    assert "from __future__ import annotations" in SCRIPT.read_text(encoding="utf-8")
