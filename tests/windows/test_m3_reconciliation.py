"""The M3 exit gate on a real Sage 100 install (SPEC.md §12, §18): ABC reconciles to Sage's reports.

Two ways, both to the cent:
- Without any exports, each view against the figures Sage keeps on ABC's own master records (customer
  aging and open order amounts, vendor balances, item totals) and the trial balance, through
  scripts/verify_views.py.
- Each view against an exported Sage report (a golden), run with the arguments the golden records
  (tests/reconciliation.py). The goldens are exported on the runner as docs/reconciliation.md describes,
  into tests/reconciliation/abc/ or the directory SAGE100_MCP_TEST_GOLDENS names. Without a manifest
  those tests are skipped, not passed.
"""

from __future__ import annotations

import os
import runpy
from pathlib import Path

import pytest

from sage100_mcp.runtime import Runtime
from sage100_mcp.views.api import run_named
from tests.paths import REPO
from tests.reconciliation import DEFAULT_GOLDENS, GOLDENS_ENV, VIEWS, Golden, differences, load_goldens
from tests.windows.conftest import SAGE_RUNNER, setting

pytestmark = SAGE_RUNNER


def test_views_agree_with_sages_own_figures(runtime: Runtime) -> None:
    verify = runpy.run_path(str(REPO / "scripts" / "verify_views.py"), run_name="verify_views")
    outcomes = verify["reconcile"](runtime, runtime.resolve_company(setting("COMPANY", "ABC")))
    lines = [outcome.line() for outcome in outcomes]
    print("\n".join(lines))
    assert len(outcomes) == len(verify["CHECKS"]), lines  # ABC has every module the checks need
    assert all(outcome.passed for outcome in outcomes), "\n".join(lines)


@pytest.fixture(scope="module")
def goldens() -> dict[str, Golden]:
    directory = Path(os.environ.get(GOLDENS_ENV) or DEFAULT_GOLDENS)
    if not (directory / "manifest.yaml").exists():
        pytest.skip(f"no goldens in {directory}: export them as docs/reconciliation.md describes")
    return load_goldens(directory)


@pytest.mark.parametrize("view", VIEWS)
def test_view_reconciles_to_sage(runtime: Runtime, goldens: dict[str, Golden], view: str) -> None:
    if view not in goldens:
        pytest.fail(f"The M3 gate needs a golden for {view}; see docs/reconciliation.md.")
    golden = goldens[view]
    limit = runtime.config.limits.max_rows_hard
    result = run_named(runtime, runtime.resolve_company(golden.company), view, golden.args, max_rows=limit)
    found = differences(result, golden)
    assert not found, f"{view} does not reconcile to {golden.report}:\n" + "\n".join(found[:50])
