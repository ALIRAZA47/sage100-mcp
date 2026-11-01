"""Load and soak a running sage100-mcp over Streamable HTTP (SPEC.md §13.5, §15.7), read-only.

    uv run python scripts/soak.py http://127.0.0.1:8765/mcp --tokens-file tokens.txt --minutes 120 \\
        --company ABC --report soak.json

Workers (20 by default) call tools in a loop, so that many calls are in flight at once. Each worker uses
the tokens in turn (one per line in --tokens-file, or SAGE100_MCP_TOKENS separated by commas). A server
with its default limit of 30 calls a minute per token needs one token per worker and calls slower than
2 s, so for a soak raise `limits.rate_per_min` on the server, or give more tokens. A RATE_LIMITED answer
is waited out as it says and counted, not failed.

The mix covers the canned views, run_query and the discovery tools. Every --sample-s seconds the soak
reads sage_status for the server's memory and its connection pools.

It passes when:
- no canned view failed with QUERY_TIMEOUT;
- no call failed with INTERNAL, or with a transport error;
- memory stayed flat: after the first fifth of the run, the last samples are within 10% of the first;
- no pool ever held more connections, in use and idle, than its cap.

The other §15.7 bar, a Sage user's screen timings within 10% of baseline, needs Sage itself: the Windows
runner measures it. The output holds tool names, counts, codes and timings only.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TOKENS_ENV = "SAGE100_MCP_TOKENS"
# Premium: an ODBC connection string for a login with VIEW SERVER STATE. The soak then counts the server's
# sessions as SQL Server sees them (APP=sage100-mcp), so a leaked connection can't hide behind the pools.
SESSIONS_ENV = "SAGE100_MCP_SOAK_SQLSERVER"
SESSIONS_SQL = "SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE program_name = 'sage100-mcp'"
VIEWS = {
    "ar_aging",
    "ap_aging",
    "open_sales_orders",
    "open_purchase_orders",
    "inventory_availability",
    "gl_trial_balance",
    "sales_history",
    "customer_lookup",
    "vendor_lookup",
    "item_lookup",
    "compare_companies",
}
FLAT = 0.10  # memory may move this much from the first samples to the last
WARM_UP = 0.2  # the share of the run before memory is compared

Factory = Callable[[dict[str, str]], httpx2.AsyncClient]


def mix(company: str, year: int) -> list[tuple[str, dict[str, Any]]]:
    """The calls each worker cycles through: mostly views, as people would ask, with queries and lookups."""
    with_company = {"company": company}
    return [
        ("ar_aging", with_company),
        ("open_sales_orders", {**with_company, "lines": False}),
        ("inventory_availability", {**with_company, "only_short": True}),
        ("ap_aging", with_company),
        ("customer_lookup", {**with_company, "query": "a"}),
        (
            "run_query",
            {**with_company, "sql": "SELECT ARDivisionNo, CustomerNo, CustomerName FROM AR_Customer", "max_rows": 50},
        ),
        ("gl_trial_balance", {**with_company, "fiscal_year": str(year), "period": "1"}),
        ("open_purchase_orders", {**with_company, "lines": False}),
        (
            "sales_history",
            {**with_company, "from_date": f"{year}-01-01", "to_date": f"{year}-12-31", "group_by": "month"},
        ),
        ("item_lookup", {**with_company, "query": "1"}),
        ("list_tables", {**with_company, "module": "AR", "limit": 20}),
        ("vendor_lookup", {**with_company, "query": "a"}),
        ("describe_table", {**with_company, "table": "AR_OpenInvoice"}),
    ]


@dataclass
class Sample:
    at_s: float
    memory_mb: float | None
    threads: int
    pools: list[dict[str, Any]]


@dataclass
class Totals:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    outcomes: Counter[tuple[str, str]] = field(default_factory=Counter)  # (tool, "ok" or a code) -> calls
    rate_limited: int = 0
    samples: list[Sample] = field(default_factory=list)
    sessions: list[int] = field(default_factory=list)  # live server sessions, when --sessions-sql is given


def http_factory(cafile: str | None) -> Factory:
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=cafile) if cafile else True

    def make(headers: dict[str, str]) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(verify=verify, headers=headers, timeout=httpx2.Timeout(300))

    return make


async def worker(
    url: str, token: str, factory: Factory, calls: list[tuple[str, dict[str, Any]]], offset: int, deadline: float,
    totals: Totals,
) -> None:  # fmt: skip
    headers = {"authorization": f"Bearer {token}"}
    step = offset
    async with Client(streamable_http_client(url, http_client=factory(headers))) as client:
        while time.monotonic() < deadline:
            tool, arguments = calls[step % len(calls)]
            step += 1
            started = time.perf_counter()
            try:
                result = await client.call_tool(tool, arguments)
            except Exception as exc:  # the transport failed: the finding is its type
                totals.outcomes[(tool, f"transport:{type(exc).__name__}")] += 1
                await anyio.sleep(1)
                continue
            elapsed = time.perf_counter() - started
            data: dict[str, Any] = result.structured_content or {}
            if result.is_error and data.get("code") == "RATE_LIMITED":
                totals.rate_limited += 1
                await anyio.sleep(min(float(data.get("retry_after_s") or 1), max(0.0, deadline - time.monotonic())))
                continue
            totals.latencies[tool].append(elapsed)
            totals.outcomes[(tool, str(data.get("code")) if result.is_error else "ok")] += 1


async def sampler(
    url: str, token: str, factory: Factory, every_s: float, deadline: float, totals: Totals,
    sessions: Callable[[], int] | None,
) -> None:  # fmt: skip
    started = time.monotonic()
    async with Client(streamable_http_client(url, http_client=factory({"authorization": f"Bearer {token}"}))) as client:
        while True:
            status = (await client.call_tool("sage_status", {})).structured_content or {}
            process = status.get("process") or {}
            pools = [pool for backend in status.get("backends", []) for pool in backend.get("pools", [])]
            totals.samples.append(
                Sample(round(time.monotonic() - started, 1), process.get("memory_mb"), process.get("threads", 0), pools)
            )
            if sessions is not None:
                totals.sessions.append(await anyio.to_thread.run_sync(sessions))
            left = deadline - time.monotonic()
            if left <= 0:
                return
            await anyio.sleep(min(every_s, left))


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))] if ordered else 0.0


def verdict(totals: Totals, caps: int | None = None) -> dict[str, Any]:
    """The report: per-tool timings and outcomes, memory and pools, and whether each bar was met."""
    tools = {}
    for tool in sorted({tool for tool, _ in totals.outcomes}):
        times = totals.latencies.get(tool, [])
        outcomes = {outcome: n for (name, outcome), n in sorted(totals.outcomes.items()) if name == tool}
        tools[tool] = {
            "calls": sum(outcomes.values()),
            "outcomes": outcomes,
            "p50_s": round(statistics.median(times), 3) if times else None,
            "p95_s": round(percentile(times, 0.95), 3) if times else None,
            "max_s": round(max(times), 3) if times else None,
        }
    failures = []
    timeouts = sum(n for (tool, code), n in totals.outcomes.items() if tool in VIEWS and code == "QUERY_TIMEOUT")
    if timeouts:
        failures.append(f"{timeouts} canned-view call(s) hit QUERY_TIMEOUT")
    broken = sum(n for (_, code), n in totals.outcomes.items() if code == "INTERNAL" or code.startswith("transport:"))
    if broken:
        failures.append(f"{broken} call(s) failed with INTERNAL or a transport error")
    memory = [s.memory_mb for s in totals.samples if s.memory_mb is not None]
    settled = memory[int(len(memory) * WARM_UP) :]
    drift = None
    if len(settled) >= 4:
        head = statistics.median(settled[: max(2, len(settled) // 5)])
        tail = statistics.median(settled[-max(2, len(settled) // 5) :])
        drift = round((tail - head) / head, 3) if head else None
        if drift is not None and abs(drift) > FLAT:
            failures.append(f"memory moved {drift:+.0%} after warm-up (allowed ±{FLAT:.0%})")
    over = [
        f"{pool['name']} at {s.at_s} s: {pool['in_use']} in use + {pool['idle']} idle > cap {pool['cap']}"
        for s in totals.samples
        for pool in s.pools
        if pool["in_use"] + pool["idle"] > pool["cap"]
    ]
    if over:
        failures.append("a pool passed its cap: " + over[0])
    if caps is not None and totals.sessions and max(totals.sessions) > caps:
        failures.append(
            f"the database saw {max(totals.sessions)} sessions from the server, more than its caps ({caps})"
        )
    calls = sum(totals.outcomes.values())
    return {
        "calls": calls,
        "rate_limited_waits": totals.rate_limited,
        "tools": tools,
        "memory_mb": {
            "first": memory[0] if memory else None,
            "last": memory[-1] if memory else None,
            "max": max(memory) if memory else None,
            "drift_after_warm_up": drift,
        },
        "pools_peak": max((p["in_use"] + p["idle"] for s in totals.samples for p in s.pools), default=0),
        "sessions_peak": max(totals.sessions) if totals.sessions else None,
        "samples": len(totals.samples),
        "passed": not failures,
        "failures": failures,
    }


async def run_soak(
    url: str,
    tokens: list[str],
    *,
    factory: Factory,
    seconds: float,
    workers: int = 20,
    company: str,
    year: int,
    sample_s: float = 30,
    sessions: Callable[[], int] | None = None,
    caps: int | None = None,
) -> dict[str, Any]:
    """Run the soak for ``seconds`` and return its report. ``sessions`` counts the server's database sessions."""
    totals = Totals()
    calls = mix(company, year)
    deadline = time.monotonic() + seconds
    async with anyio.create_task_group() as group:
        group.start_soon(sampler, url, tokens[0], factory, sample_s, deadline, totals, sessions)
        for n in range(workers):
            group.start_soon(worker, url, tokens[n % len(tokens)], factory, calls, n, deadline, totals)
    report = verdict(totals, caps)
    report |= {"workers": workers, "seconds": round(seconds), "company": company}
    return report


def text(report: dict[str, Any]) -> str:
    lines = [f"{report['calls']} calls by {report['workers']} workers in {report['seconds']} s "
             f"({report['rate_limited_waits']} rate-limit waits)"]  # fmt: skip
    for tool, figures in report["tools"].items():
        outcomes = ", ".join(f"{code} {n}" for code, n in figures["outcomes"].items())
        lines.append(
            f"  {tool:24} p50 {figures['p50_s']} s  p95 {figures['p95_s']} s  max {figures['max_s']} s  ({outcomes})"
        )
    memory = report["memory_mb"]
    lines.append(
        f"memory {memory['first']} -> {memory['last']} MB (max {memory['max']}, drift after warm-up "
        f"{memory['drift_after_warm_up']}); pools peak {report['pools_peak']}; "
        f"database sessions peak {report['sessions_peak']}"
    )
    lines += [f"FAIL: {failure}" for failure in report["failures"]] or ["every bar met"]
    return "\n".join(lines)


def session_counter(connection_string: str) -> Callable[[], int]:
    def count() -> int:
        import pyodbc

        connection = pyodbc.connect(connection_string, autocommit=True, timeout=15)
        try:
            found: int = connection.cursor().execute(SESSIONS_SQL).fetchone()[0]
        finally:
            connection.close()
        return found

    return count


def read_tokens(path: Path | None) -> list[str]:
    if path is not None:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [token.strip() for token in os.environ.get(TOKENS_ENV, "").split(",") if token.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("url", help="the MCP endpoint, such as https://sage-mcp.corp.example/mcp")
    parser.add_argument("--tokens-file", type=Path, help=f"one token per line (default: {TOKENS_ENV})")
    parser.add_argument("--minutes", type=float, default=120)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--company", required=True)
    parser.add_argument("--year", type=int, default=date.today().year, help="fiscal year for the GL and sales")
    parser.add_argument("--sample-s", type=float, default=30)
    parser.add_argument("--cafile")
    parser.add_argument("--session-cap", type=int, help=f"with {SESSIONS_ENV}: the most sessions allowed")
    parser.add_argument("--report", type=Path, help="write the full report as JSON")
    args = parser.parse_args(argv)
    tokens = read_tokens(args.tokens_file)
    if not tokens:
        print(f"give tokens with --tokens-file or {TOKENS_ENV}", file=sys.stderr)
        return 2
    sessions = session_counter(os.environ[SESSIONS_ENV]) if os.environ.get(SESSIONS_ENV) else None
    report = anyio.run(
        lambda: run_soak(
            args.url,
            tokens,
            factory=http_factory(args.cafile),
            seconds=args.minutes * 60,
            workers=args.workers,
            company=args.company.upper(),
            year=args.year,
            sample_s=args.sample_s,
            sessions=sessions,
            caps=args.session_cap,
        )
    )
    print(text(report))
    if args.report:
        args.report.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
