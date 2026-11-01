"""sage100-mcp init on Premium (SPEC.md §16.2): unattended, against the SQL Server test bed, then doctor."""

from __future__ import annotations

from pathlib import Path

import pytest

from sage100_mcp.cli import EXIT_OK, main
from sage100_mcp.config import SqlServerBackend, load_config
from tests import premium_testbed as testbed

pytestmark = pytest.mark.premium


def test_init_then_doctor(server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    stored: list[tuple[str, str, str]] = []
    monkeypatch.setattr("sage100_mcp.setup.store_in_keyring", lambda *entry: stored.append(entry))
    monkeypatch.setenv("INIT_PASSWORD", testbed.READER_PASSWORD)
    config = tmp_path / "config.yaml"
    argv = ["init", "--config", str(config), "--yes", "--type", "sqlserver", "--server", server,
            "--trust-server-certificate", "--user", testbed.READER, "--password-env", "INIT_PASSWORD",
            "--company", "EMP"]  # fmt: skip
    assert main(argv) == EXIT_OK, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "Companies: " in out
    assert "EMP (Example Emporium Inc.)" in out
    assert stored == [("sage100-mcp/premium", testbed.READER, testbed.READER_PASSWORD)]
    written = load_config(config, env={}, must_exist=True).config
    backend = written.backends["premium"]
    assert isinstance(backend, SqlServerBackend)
    assert (backend.server, backend.trust_server_certificate, written.companies.default) == (server, True, "EMP")
    assert testbed.READER_PASSWORD not in config.read_text(encoding="utf-8")
    # The keyring is stubbed out here, so doctor finds the login in the environment instead.
    monkeypatch.setenv("SAGE100_MCP_CRED_PREMIUM_USER", testbed.READER)
    monkeypatch.setenv("SAGE100_MCP_CRED_PREMIUM_PASSWORD", testbed.READER_PASSWORD)
    assert main(["doctor", "--config", str(config)]) == EXIT_OK, capsys.readouterr().out
