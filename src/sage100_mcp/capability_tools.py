"""refresh_capabilities, sage://capabilities and sage://dialect-guide (SPEC.md §5.3, §11.1, §11.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from sage100_mcp.capabilities import Capabilities, CapabilitySummary, load, summarize
from sage100_mcp.companies import Company
from sage100_mcp.runtime import Runtime


class BackendCapabilities(BaseModel):
    backend: str
    companies: list[str]
    capabilities: CapabilitySummary | None = Field(description="None until the backend has connected")


class CapabilitiesReport(BaseModel):
    backends: list[BackendCapabilities]
    cached_profiles: list[CapabilitySummary] = Field(description="Every driver version probed on this host")


class CapabilityRefresh(BaseModel):
    company: str
    backend: str
    status: Literal["probed", "not_applicable"]
    message: str
    capabilities: CapabilitySummary | None
    changed: list[str] = Field(description="Checks whose result differs from the previous profile")


def _cached(home: Path) -> list[CapabilitySummary]:
    found = []
    for path in sorted((home / "capabilities").glob("*.json")):
        caps = load(path)
        if caps is not None:
            found.append(summarize(caps))
    return found


def capability_report(runtime: Runtime) -> CapabilitiesReport:
    backends = []
    for name, state in runtime.backends.items():
        if state.kind != "providex":
            continue
        companies = [code for code, company in runtime.visible_companies().items() if company.backend == name]
        caps = state.executor.capabilities if state.executor is not None else None
        backends.append(
            BackendCapabilities(backend=name, companies=companies, capabilities=summarize(caps) if caps else None)
        )
    return CapabilitiesReport(backends=backends, cached_profiles=_cached(runtime.home))


def _changes(before: Capabilities, after: Capabilities) -> list[str]:
    old, new = before.summary(), after.summary()
    return sorted((set(old["passed"]) ^ set(new["passed"])) | (set(old["failed"]) ^ set(new["failed"])))


def refresh_capabilities(runtime: Runtime, company: Company) -> CapabilityRefresh:
    from sage100_mcp.backends.providex import ProvideXExecutor

    executor = runtime.executor(company)
    if not isinstance(executor, ProvideXExecutor):
        return CapabilityRefresh(
            company=company.code,
            backend=company.backend,
            status="not_applicable",
            message="This company is on SQL Server (Premium), which runs T-SQL; there is no driver profile to probe.",
            capabilities=None,
            changed=[],
        )
    before = executor.capabilities
    after = executor.refresh_capabilities(company.code)
    changed = _changes(before, after)
    message = f"Probed driver {after.driver_version} through company {company.code}: " + (
        f"{len(changed)} check(s) changed." if changed else "no changes."
    )
    return CapabilityRefresh(
        company=company.code,
        backend=company.backend,
        status="probed",
        message=message,
        capabilities=summarize(after),
        changed=changed,
    )


GUIDE = """# ProvideX SQL on this server

Sage 100 Standard and Advanced keep their data in ProvideX files, read through Sage's ODBC driver,
which accepts only part of SQL. Write ordinary ANSI SQL or T-SQL: run_query rewrites it for the
driver, and runs what the driver can't in a local engine (mode "hybrid"). explain_query shows the
plan, the SQL sent and why, without running anything.

## Rewritten for you
- Joins become {IJ ...} or {oj ...}; a bare JOIN is never sent.
- An ISO date compared with a date column becomes {d 'YYYY-MM-DD'}.
- UPPER, LEN, LEFT, SUBSTRING, CHARINDEX, ISNULL, ROUND, YEAR, DATEADD, DATEDIFF, GETDATE and friends
  become ODBC {fn ...} calls where this driver supports them.
- TOP n becomes LIMIT n, or the fetch stops after n rows. Semicolons and [brackets] go.

## Run locally (hybrid)
- Anything the driver failed in its probe (listed below per backend).
- Always: WITH, window functions, RIGHT or FULL joins, inner and left joins mixed in one query, a WHERE
  on a LEFT-joined table, ORDER BY together with TOP or LIMIT.
- Each table is read once, with the filters the driver can apply, up to max_pushdown_rows rows; past
  that the query fails with PUSHDOWN_LIMIT and names the filter to add.

## Fast queries
- Filter on key columns (ARDivisionNo + CustomerNo, VendorNo, ItemCode, SalesOrderNo): ProvideX reads
  by key; any other filter scans the whole file across the network.
- History tables (AR_InvoiceHistory*, SO_SalesOrderHistory*, AP_InvoiceHistory*, GL_DetailPosting,
  IM_ItemTransactionHistory and others) need a date range or a key filter (FILTER_REQUIRED).
- Name the columns you need: wide tables cost more to read.

## Sage data
- Text is never NULL: a blank is ''. A blank date reads as NULL.
- Money and quantities are exact decimals; results return them as strings.
- AR_OpenInvoice keeps paid invoices: add Balance <> 0 for open AR.

## Raw ProvideX
dialect='providex' sends your text exactly as written, after the same guard and policy checks. Use it
only when the rewrite gets something wrong: joins must be inside {oj} or {IJ}, functions inside {fn},
dates as {d '...'}, and nothing the driver profile below lists as failed.
"""


def dialect_guide(runtime: Runtime) -> str:
    lines = [GUIDE.rstrip(), "", "## This server's ProvideX drivers"]
    report = capability_report(runtime)
    if not report.backends:
        lines.append("No ProvideX backend is configured; every company here runs T-SQL on SQL Server.")
    for backend in report.backends:
        caps = backend.capabilities
        if caps is None:
            lines.append(f"- {backend.backend}: not connected yet.")
            continue
        state = f"probed {caps.probed_at}" if caps.source == "probe" else "not probed yet (baseline profile)"
        lines.append(f"- {backend.backend} (companies {', '.join(backend.companies) or 'none'}): driver "
                     f"{caps.driver_version}, {state}.")  # fmt: skip
        lines.append(f"  - Sent natively: {', '.join(caps.passed) or 'nothing beyond the baseline'}.")
        lines.append(f"  - Run locally: {', '.join(caps.failed) or 'nothing'}.")
    return "\n".join(lines) + "\n"
