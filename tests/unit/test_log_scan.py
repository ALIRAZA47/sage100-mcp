"""scripts/log_scan.py (SPEC.md §15.8): it finds every kind of leak it promises to, and nothing else."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.paths import REPO

SCAN = runpy.run_path(str(REPO / "scripts" / "log_scan.py"), run_name="log_scan")
main: Callable[[list[str]], int] = SCAN["main"]
secrets_of_the_testbed: Callable[[], tuple[set[str], set[str]]] = SCAN["testbed_values"]


def tree(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".log-scan-fixture").write_text("planted leaks for tests/unit/test_log_scan.py\n", encoding="utf-8")
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return tmp_path


CLEAN: dict[str, str | bytes] = {
    "pytest.log": "INFO sage100_mcp: connected with DRIVER={MAS 90 4.0 ODBC Driver};UID=mcp_reader;PWD=***\n"
    "DEBUG sage100_mcp.server: run_query failed for token:ab12: {'code': 'PARSE_ERROR', 'detail': \"near '…'\"}\n"
    "set SAGE100_MCP_CRED_SAGE_PASSWORD and the user to log in\n",
    "home/audit/2026-10-31.jsonl": '{"principal": "token:ab12", "sql": ["SELECT ? FROM AR_Vendor"]}\n',
    "home/tmp/spill.bin": b"\x00\xff\x00PWD=hunter2\xff",  # binary files are not text logs
    "home/tokens.json": '{"tokens": [{"id": "ab12", "sha256": "9f86d081884c7d659a2feaa0c55ad015"}]}\n',
}


def test_a_clean_tree_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tree(tmp_path, CLEAN)), "--testbed"]) == 0
    assert "0 finding(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("connect: DRIVER={SQL Server};UID=sa;PWD=hunter2;Database=MAS_EMP", "a connection-string password"),
        ("password = Sage100-mcp-Test-Reader-1", "a known secret"),
        ("Authorization: Bearer s1mcp_" + "A" * 43, "a bearer token"),
        ("detail: Conversion failed for '900000007'", "a value from a masked column"),
        ("rows: [['V0001', \"900000012\"]]", "a value from a masked column"),
        ("Login failed with reader-pass", "a known secret"),
    ],
)
def test_each_kind_of_leak_is_found(line: str, kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tree(tmp_path, {**CLEAN, "server/serve.log": f"ok\n{line}\n"})
    assert main([str(root), "--testbed"]) == 1
    out = capsys.readouterr().out
    assert f"serve.log:2: {kind}" in out
    assert "hunter2" not in out, "the report never repeats a secret"


def test_masked_values_match_whole_values_only(tmp_path: Path) -> None:
    root = tree(tmp_path, {"a.log": "order 19000000071 and invoice 9000000070 and port 900000\n"})
    assert main([str(root), "--testbed"]) == 0


def test_secrets_can_be_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tree(tmp_path, {"a.log": "the site's own password is correct-horse\n"})
    assert main([str(root)]) == 0
    assert main([str(root), "--secret", "correct-horse"]) == 1
    assert "a.log:1: a known secret" in capsys.readouterr().out


def test_the_testbed_supplies_passwords_and_masked_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAGE100_MCP_TEST_SA_PASSWORD", "sa-secret-1")
    passwords, masked = secrets_of_the_testbed()
    assert {"Sage100-mcp-Test-Reader-1", "reader-pass", "sa-secret-1"} <= passwords
    assert "900000001" in masked
    assert all(len(value) >= 6 for value in masked)


def test_a_missing_path_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "nowhere")]) == 2
    assert "not found" in capsys.readouterr().err


def test_the_scan_reads_nested_directories(tmp_path: Path) -> None:
    files: dict[str, Any] = {f"t{n}/home/audit/2026-10-31.jsonl": "{}\n" for n in range(3)}
    files["t2/home/audit/2026-10-31.jsonl"] = '{"detail": "PASSWORD=Wr0ng;pass"}\n'
    assert main([str(tree(tmp_path, files))]) == 1


def test_planted_fixtures_are_skipped_from_above(tmp_path: Path) -> None:
    tree(tmp_path / "test_x0", {"a.log": "PWD=hunter2\n"})
    (tmp_path / "real.log").write_text("all quiet\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 0, "a planted tree below the scanned directory is a test of the scan"
    assert main([str(tmp_path / "test_x0")]) == 1, "scanned directly, it is not skipped"


def test_gzipped_audit_days_are_read(tmp_path: Path) -> None:
    import gzip

    root = tree(tmp_path, {"home/audit/2026-10-30.jsonl.gz": gzip.compress(b'{"detail": "PWD=hunter2"}\n')})
    assert main([str(root)]) == 1
