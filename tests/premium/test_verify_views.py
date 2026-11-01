"""scripts/verify_views.py against the SQL Server test bed: the views' Premium plans agree with the
figures the test bed keeps on its master records the way Sage does (see test_verify_views in the
emulator tests for the ProvideX plans)."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from tests import premium_testbed as testbed
from tests.paths import REPO

pytestmark = pytest.mark.premium


def test_views_agree_with_sages_own_figures(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "backends:\n  premium:\n    type: sqlserver\n"
        f"    server: '{server}'\n    trust_server_certificate: true\n    credential: sage100-mcp/test_premium\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAGE100_MCP_CRED_TEST_PREMIUM_USER", testbed.READER)
    monkeypatch.setenv("SAGE100_MCP_CRED_TEST_PREMIUM_PASSWORD", testbed.READER_PASSWORD)
    verify = runpy.run_path(str(REPO / "scripts" / "verify_views.py"), run_name="verify_views")
    assert verify["main"](["--config", str(config), "--company", "EMP"]) == 0
    out = capsys.readouterr().out
    assert out.count("[PASS]") == len(verify["CHECKS"])
    assert "[FAIL]" not in out
    assert "(not compared: 1 with invoices changed on or after their aging date; 1 whose stored aging" in out
    assert out.rstrip().endswith("every check agreed")
    for customer in testbed.emp_rows()["AR_Customer"]:  # codes only: no names or amounts in the output
        assert not customer.get("CustomerName") or customer["CustomerName"].strip() not in out
