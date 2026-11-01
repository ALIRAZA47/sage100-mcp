"""Check a remote sage100-mcp from a client machine, such as a Mac, before running the eval set (SPEC.md §18 M4).

    export SAGE100_MCP_TOKEN=s1mcp_...     # from `sage100-mcp token create --principal NAME` on the server
    uv run python scripts/remote_check.py https://sage-mcp.corp.example:8765/mcp [--cafile ca.pem] [--company ABC]

It connects as Claude Code does, over HTTPS with the bearer token, and checks that:

- without a token the server answers 401 with WWW-Authenticate, and a foreign Origin gets 403;
- both protocol revisions (2025-11-25 and 2026-07-28) list the 22 tools and answer sage_status;
- a company set with set_company is used by later calls that name none;
- run_query pages: a truncated result's next_cursor brings the next rows;
- a canned view runs (ar_aging, else gl_trial_balance).

The token is read from SAGE100_MCP_TOKEN so that it stays out of shell history. --cafile names the
certificate (or CA) to trust when the server's is self-signed or from a private CA. The output holds
counts and codes only. Exit status 0 means every check passed.
"""

from __future__ import annotations

import argparse
import os
import ssl
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

TOKEN_ENV = "SAGE100_MCP_TOKEN"
MODES = {"legacy": "2025-11-25", "auto": "2026-07-28"}
TOOLS = 22
ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "remote_check", "version": "1"},
    },
}
# A table to page through, by the first module the company has: (module, table, key columns).
PAGEABLE = [
    ("AR", "AR_Customer", "ARDivisionNo, CustomerNo"),
    ("AP", "AP_Vendor", "APDivisionNo, VendorNo"),
    ("GL", "GL_Account", "AccountKey"),
]

Factory = Callable[[dict[str, str]], httpx2.AsyncClient]  # headers -> a client for the server


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def line(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


def https_factory(cafile: str | None) -> Factory:
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=cafile) if cafile else True

    def make(headers: dict[str, str]) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(verify=verify, headers=headers, timeout=httpx2.Timeout(120))

    return make


def data(outcome: CallToolResult) -> dict[str, Any]:
    content: dict[str, Any] = outcome.structured_content or {}
    if outcome.is_error:
        raise RuntimeError(f"{content.get('code')}: {content.get('message')}")
    return content


async def transport_checks(url: str, token: str, factory: Factory) -> list[Check]:
    checks = []
    async with factory({}) as anonymous:
        response = await anonymous.post(url, json=INITIALIZE, headers=ACCEPT)
    challenged = response.status_code == 401 and response.headers.get("www-authenticate", "").startswith("Bearer")
    checks.append(Check("no token", challenged, f"HTTP {response.status_code}"))
    async with factory({"authorization": f"Bearer {token}", "origin": "https://evil.example"}) as foreign:
        response = await foreign.post(url, json=INITIALIZE, headers=ACCEPT)
    checks.append(Check("foreign Origin", response.status_code == 403, f"HTTP {response.status_code}"))
    for mode, version in MODES.items():
        try:
            async with Client(streamable_http_client(url, http_client=factory(bearer(token))), mode=mode) as client:
                tools = len((await client.list_tools()).tools)
                server = data(await client.call_tool("sage_status", {}))["server"]
                passed = client.protocol_version == version and tools == TOOLS
                detail = f"{tools} tools, server {server['version']} ({server['milestone']})"
                checks.append(Check(f"protocol {version}", passed, detail))
        except Exception as exc:  # what failed is the finding: TLS, auth, protocol
            checks.append(Check(f"protocol {version}", False, f"{type(exc).__name__}: {exc}"))
    return checks


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def company_checks(url: str, token: str, factory: Factory, company: str | None) -> list[Check]:
    checks = []
    async with Client(streamable_http_client(url, http_client=factory(bearer(token)))) as client:
        companies = data(await client.call_tool("list_companies", {}))["companies"]
        if not companies:
            return [Check("companies", False, "none visible to this token; see sage_status on the server")]
        chosen = next((c for c in companies if c["code"] == (company or "").upper()), None) if company else companies[0]
        if chosen is None:
            return [Check("companies", False, f"{company} is not among {', '.join(c['code'] for c in companies)}")]
        code = chosen["code"]
        checks.append(Check("companies", True, f"{len(companies)} visible; checking {code}"))
        data(await client.call_tool("set_company", {"company": code}))
        used = data(await client.call_tool("list_tables", {"limit": 1}))["company"]
        checks.append(Check("active company", used == code, f"set_company {code}, then list_tables used {used}"))
        modules = set(chosen.get("modules") or [])
        table = next(((t, key) for module, t, key in PAGEABLE if module in modules), None)
        if table is None:
            checks.append(Check("paging", False, "the company has none of AR, AP and GL"))
        else:
            arguments = {"sql": f"SELECT {table[1]} FROM {table[0]} ORDER BY {table[1]}", "max_rows": 2}
            first = data(await client.call_tool("run_query", arguments))
            if not first.get("next_cursor"):
                checks.append(Check("paging", True, f"only {first['row_count']} row(s) in {table[0]}; nothing to page"))
            else:
                second = data(await client.call_tool("run_query", {**arguments, "cursor": first["next_cursor"]}))
                moved = second["row_count"] > 0 and second["rows"][0] != first["rows"][0]
                checks.append(Check("paging", moved, f"{table[0]}: page 2 holds {second['row_count']} row(s)"))
        view, arguments = "ar_aging", {}
        if "AR" not in modules:
            year, _, period = chosen["current_periods"].get("GL", f"{date.today().year}-01").partition("-")
            view, arguments = "gl_trial_balance", {"fiscal_year": year, "period": int(period)}
        try:
            rows = data(await client.call_tool(view, arguments))["row_count"]
            checks.append(Check("canned view", True, f"{view}: {rows} row(s)"))
        except RuntimeError as exc:
            checks.append(Check("canned view", False, f"{view}: {exc}"))
    return checks


async def run_checks(url: str, token: str, *, factory: Factory, company: str | None = None) -> list[Check]:
    checks = await transport_checks(url, token, factory)
    if all(check.passed for check in checks):
        try:
            checks += await company_checks(url, token, factory, company)
        except Exception as exc:
            checks.append(Check("company checks", False, f"{type(exc).__name__}: {exc}"))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("url", help="the MCP endpoint, such as https://sage-mcp.corp.example:8765/mcp")
    parser.add_argument("--cafile", help="certificate or CA bundle to trust for the server's TLS certificate")
    parser.add_argument("--company", help="company code to check (default: the first one the token sees)")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"set {TOKEN_ENV} to a token from `sage100-mcp token create`", file=sys.stderr)
        return 2
    checks = anyio.run(lambda: run_checks(args.url, token, factory=https_factory(args.cafile), company=args.company))
    for check in checks:
        print(check.line())
    failures = sum(not check.passed for check in checks)
    print("every check passed" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
