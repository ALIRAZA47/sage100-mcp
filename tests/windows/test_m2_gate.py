"""The M2 exit gate on a real Sage 100 install (SPEC.md §15.3, §18): the self-hosted Windows runner.

Run against Sage 100 2026's 64-bit driver, and against 2024's 32-bit driver through the 32-bit helper
(docs/decisions.md D67), each with the ABC sample company:

    SAGE100_MCP_TEST_PROVIDEX_DIRECTORY=\\\\SAGESRV\\Sage\\Sage 100\\MAS90
    SAGE100_MCP_TEST_PROVIDEX_USER=mcp_reader  SAGE100_MCP_TEST_PROVIDEX_PASSWORD=...
    SAGE100_MCP_TEST_PROVIDEX_COMPANY=ABC      (optional; ABC by default)
    SAGE100_MCP_TEST_PROVIDEX_HELPER=C:\\Python312-32\\python.exe   (the 32-bit leg: a 32-bit Python with pyodbc)
    uv run pytest -m windows

The first run on a new driver build writes tests/capabilities/<driver version>-<bitness>.json and
fails, so the probe's results are reviewed and committed; after that a change in them fails the
build until someone looks (§15.3).
"""

from __future__ import annotations

import json

import pytest

from sage100_mcp.backends.drivers import process_bitness
from sage100_mcp.backends.providex import ProvideXExecutor
from sage100_mcp.capabilities import Capabilities, load, save
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.runtime import Runtime
from tests.paths import REPO
from tests.sample_queries import run_samples
from tests.windows.conftest import SAGE_RUNNER, helper_leg, setting

COMMITTED = REPO / "tests" / "capabilities"

pytestmark = SAGE_RUNNER


def executor_of(runtime: Runtime) -> ProvideXExecutor:
    executor = runtime.backends["sage"].executor
    assert isinstance(executor, ProvideXExecutor), runtime.backends["sage"].error
    return executor


def test_probe_results_are_committed(runtime: Runtime) -> None:
    executor = executor_of(runtime)
    found = executor.refresh_capabilities(setting("COMPANY", "ABC"))
    bits = 32 if helper_leg() else process_bitness()
    path = COMMITTED / f"{found.driver_version}-{'x64' if bits == 64 else 'x86'}.json"
    committed = load(path)
    if committed is None:
        save(path, found)
        pytest.fail(f"New driver build: review {path.relative_to(REPO)} and commit it.")
    assert isinstance(committed, Capabilities)
    differences = {
        part: sorted(k for k in set(getattr(found, part)) | set(getattr(committed, part))
                     if getattr(found, part).get(k) != getattr(committed, part).get(k))
        for part in ("features", "functions", "keywords")
    }  # fmt: skip
    assert not any(differences.values()), f"Probe results changed from {path.name}: {json.dumps(differences)}"


def test_fifty_samples_against_the_sample_company(runtime: Runtime) -> None:
    outcomes = run_samples(runtime, runtime.resolve_company(setting("COMPANY", "ABC")))
    failures = [f"{o.sample.name} ({o.mode}): {o.detail}" for o in outcomes if not o.passed]
    assert not failures, "\n".join(failures)
    hybrid = [o.sample.name for o in outcomes if o.mode == "hybrid"]
    assert len(hybrid) >= 15, hybrid


def test_driver_messages_match_the_error_rules(runtime: Runtime) -> None:
    """The texts errors.yaml keys on, as this driver build words them (§14.3)."""
    executor = executor_of(runtime)
    company = setting("COMPANY", "ABC")
    for sql in (
        "SELECT c.CustomerNo FROM AR_Customer c INNER JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo",
        "SELECT CustomerNo FROM AR_Customer WHERE CustomerNo IN (SELECT CustomerNo FROM AR_OpenInvoice)",
    ):
        with pytest.raises(SageError) as caught:
            executor.query(company, sql, max_rows=1, timeout_s=30)
        assert caught.value.code == ErrorCode.DIALECT_UNSUPPORTED, caught.value.detail
    with pytest.raises(SageError) as caught:
        executor.query("ZZZ", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=30)
    assert caught.value.code == ErrorCode.COMPANY_NOT_FOUND, caught.value.detail


def test_company_discovery_path(runtime: Runtime) -> None:
    """§19.1: is SY_Company readable through a company connection, and which column holds the name?"""
    executor = executor_of(runtime)
    companies = executor.discover()
    assert any(c.code == setting("COMPANY", "ABC") for c in companies)
    names = [c.name for c in companies]
    print(f"SY_Company reachable: {any(names)}; companies: {[(c.code, c.name) for c in companies]}")
