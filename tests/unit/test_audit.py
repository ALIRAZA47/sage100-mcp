"""The audit log (SPEC.md §9.4): what a line holds, what it never holds, and how long files are kept."""

from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import Client

from sage100_mcp.audit import AuditLog, call_entry, redact_sql
from sage100_mcp.server import build_server
from tests.fake_backend import fake_runtime

pytestmark = [pytest.mark.anyio]

if sys.platform == "win32":
    LIMITED_USER = False  # chmod cannot take write access away on Windows
else:
    LIMITED_USER = os.geteuid() != 0  # root writes anywhere


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT CustomerNo FROM AR_Customer WHERE CustomerName = 'O''Brien'",
         "SELECT CustomerNo FROM AR_Customer WHERE CustomerName = '…'"),
        ("SELECT TOP 1001 Balance FROM AR_OpenInvoice WHERE Balance > 1250.50 AND DaysDelinquent >= -3",
         "SELECT TOP ? Balance FROM AR_OpenInvoice WHERE Balance > ? AND DaysDelinquent >= ?"),
        ("SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= {d '2024-12-01'}",
         "SELECT InvoiceNo FROM AR_OpenInvoice WHERE InvoiceDate >= {d '…'}"),
        ("SELECT AgingCategory1, [UDF_X2] FROM AR_Options WHERE Col3 = ?",
         "SELECT AgingCategory1, [UDF_X2] FROM AR_Options WHERE Col3 = ?"),
    ],
)  # fmt: skip
def test_literals_are_redacted_but_names_are_not(sql: str, expected: str) -> None:
    assert redact_sql(sql) == expected


def test_a_call_entry_keeps_names_not_values() -> None:
    entry = call_entry(
        principal="token:ab12",
        name="alice",
        transport="http",
        client="claude-code 2.1",
        tool="run_query",
        arguments={"sql": "SELECT 1 FROM AR_Customer", "params": ["secret value"], "company": "EMP"},
        structured={"company": "EMP", "sent_sql": ["SELECT 1 FROM AR_Customer"], "mode": "native", "row_count": 1},
        is_error=False,
        elapsed_ms=12,
    )
    assert entry == {
        "principal": "token:ab12",
        "name": "alice",
        "transport": "http",
        "client": "claude-code 2.1",
        "tool": "run_query",
        "args": ["company", "params", "sql"],
        "company": "EMP",
        "sql": ["SELECT 1 FROM AR_Customer"],
        "mode": "native",
        "rows": 1,
        "elapsed_ms": 12,
        "outcome": "ok",
    }


def test_an_error_entry_carries_its_code_and_correlation_id() -> None:
    internal = {"code": "INTERNAL", "message": "x", "hint": "Report correlation id 0123456789ab; the server ..."}
    entry = call_entry(principal="stdio", name="local", transport="stdio", client=None, tool="ar_aging", arguments={},
                       structured=internal, is_error=True, elapsed_ms=3)  # fmt: skip
    assert (entry["outcome"], entry["code"], entry["correlation_id"]) == ("error", "INTERNAL", "0123456789ab")
    assert "rows" not in entry
    assert "client" not in entry
    refused = call_entry(principal="stdio", name="local", transport="stdio", client=None, tool="run_query",
                         arguments={"bogus": 1}, structured=None, is_error=True, elapsed_ms=1)  # fmt: skip
    assert refused["code"] == "INVALID_ARGUMENTS"


def test_lines_go_to_a_file_per_utc_day_and_old_files_are_pruned(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit", keep_days=90)
    today = datetime.now(UTC).date()
    (tmp_path / "audit").mkdir()
    old, kept = log.path_for(today - timedelta(days=91)), log.path_for(today - timedelta(days=90))
    for path in (old, kept, tmp_path / "audit" / "notes.jsonl"):
        path.write_text("{}\n", encoding="utf-8")
    log.record({"tool": "list_companies", "sql": ["SELECT 1 FROM AR_Customer WHERE CustomerNo = 'C1'"]})
    log.record({"tool": "list_tables"})
    lines = [json.loads(line) for line in log.path_for(today).read_text(encoding="utf-8").splitlines()]
    assert [line["tool"] for line in lines] == ["list_companies", "list_tables"]
    assert lines[0]["ts"].endswith("Z")
    assert lines[0]["sql"] == ["SELECT ? FROM AR_Customer WHERE CustomerNo = '…'"]
    assert not old.exists()
    assert not kept.exists(), "an earlier day's file is gzipped"
    assert gzip.decompress(kept.with_name(kept.name + ".gz").read_bytes()) == b"{}\n"
    assert (tmp_path / "audit" / "notes.jsonl").exists()


def test_literals_stay_when_redaction_is_off(tmp_path: Path) -> None:
    log = AuditLog(tmp_path, redact_literals=False)
    log.record({"sql": ["SELECT 1 FROM AR_Customer WHERE CustomerNo = 'C1'"]})
    assert "'C1'" in log.path_for(datetime.now(UTC).date()).read_text(encoding="utf-8")


@pytest.mark.skipif(not LIMITED_USER, reason="needs a directory this user cannot write")
def test_an_unwritable_directory_never_fails_a_call(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    tmp_path.chmod(0o500)
    try:
        AuditLog(tmp_path / "audit").record({"tool": "x"})
    finally:
        tmp_path.chmod(0o700)
    assert "Could not write the audit log" in caplog.text


async def test_stdio_calls_are_audited_as_the_local_principal(tmp_path: Path) -> None:
    runtime, _ = fake_runtime(tmp_path)
    async with Client(build_server(runtime.loaded, runtime)) as client:
        await client.call_tool("list_companies", {})
        await client.call_tool("set_company", {"company": "NOPE"})
    text = AuditLog(tmp_path / "audit").path_for(datetime.now(UTC).date()).read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines()]
    seen = [(line["principal"], line["name"], line["transport"], line["tool"], line["outcome"]) for line in lines]
    assert seen == [
        ("stdio", "local", "stdio", "list_companies", "ok"),
        ("stdio", "local", "stdio", "set_company", "error"),
    ]
    assert lines[1]["code"] == "COMPANY_NOT_FOUND"


def test_gzipped_days_are_pruned_too(tmp_path: Path) -> None:
    log = AuditLog(tmp_path, keep_days=90)
    today = datetime.now(UTC).date()
    packed = tmp_path / f"{today - timedelta(days=91):%Y-%m-%d}.jsonl.gz"
    packed.write_bytes(gzip.compress(b"{}\n"))
    log.record({"tool": "x"})
    assert not packed.exists()
