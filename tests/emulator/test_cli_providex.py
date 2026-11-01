"""sage100-mcp probe and doctor on an emulated ProvideX install (SPEC.md §16.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sage100_mcp.capabilities import Capabilities
from sage100_mcp.cli import EXIT_OK, _providex_checks, main
from sage100_mcp.config import LoadedConfig, ProvideXBackend, load_config
from tests import emulator
from tests.emulator.conftest import Installer


def write_config(
    tmp_path: Path, install: emulator.Install, monkeypatch: pytest.MonkeyPatch, *, extra: str = ""
) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "backends:\n"
        "  sage:\n"
        "    type: providex\n"
        f"    directory: '{install.directory}'\n"
        "    credential: sage100-mcp/sage\n"
        f"{extra}"
        "companies:\n"
        "  default: EMP\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAGE100_MCP_CRED_SAGE_USER", "mcp_reader")
    monkeypatch.setenv("SAGE100_MCP_CRED_SAGE_PASSWORD", "reader-pass")
    monkeypatch.setitem(sys.modules, "pyodbc", emulator)  # the executor imports pyodbc on first use
    return config


def checks(config: Path) -> list[tuple[str, str]]:
    loaded: LoadedConfig = load_config(config)
    backend = loaded.config.backends["sage"]
    assert isinstance(backend, ProvideXBackend)
    return _providex_checks("sage", backend, loaded, odbc=emulator)


def test_probe_prints_the_matrix(
    make_install: Installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, make_install("calvin_graham"), monkeypatch)
    assert main(["probe", "--config", str(config), "--company", "PCC"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "sage: Probed driver 4.40.0004 through company PCC" in out
    assert "  pass  limit" in out
    assert "  FAIL  or                     Unexpected extra token: OR" in out
    assert "  FAIL    fn:UCASE" in out
    assert "  pass  keyword EnforceNullDate" in out


def test_doctor_checks_share_login_and_reads(
    make_install: Installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path, make_install("pxplus_full"), monkeypatch)
    results = checks(config)
    levels = [level for level, _ in results]
    assert levels == ["OK"] * len(results), results
    messages = "\n".join(message for _, message in results)
    assert "reachable as" in messages
    assert "data folders for ABC, EMP, PCC" in messages
    assert "company EMP: Silent=1 connect in" in messages
    assert "capabilities probed" in messages


def test_doctor_reports_an_unreachable_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install = emulator.Install(str(tmp_path / "missing"), Capabilities(driver_version="x", source="baseline"),
                               {}, {})  # fmt: skip
    config = write_config(tmp_path, install, monkeypatch)
    results = checks(config)
    assert results[0][0] == "FAIL"
    assert "is not reachable as" in results[0][1]


def test_doctor_reports_a_closed_cs_port(
    make_install: Installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = "    transport: cs\n    cs: {host: 127.0.0.1, port: 9}\n"
    config = write_config(tmp_path, make_install(), monkeypatch, extra=extra)
    results = checks(config)
    assert results[-1][0] == "FAIL"
    assert "Client/Server ODBC port 127.0.0.1:9" in results[-1][1]
