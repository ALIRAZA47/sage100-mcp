"""M0 exit gate: launch a server command over stdio and check it answers sage_status.

    uv run python scripts/stdio_smoke.py uvx --from . sage100-mcp stdio

Runs once per protocol revision (2025-11-25 handshake and 2026-07-28) and exits non-zero on failure.
"""

from __future__ import annotations

import os
import sys

import anyio
from mcp import Client, StdioServerParameters

MODES = {"legacy": "2025-11-25", "auto": "2026-07-28"}


async def check(command: list[str], mode: str) -> None:
    # The SDK passes a child only a minimal environment; keep ours (SAGE100_MCP_*, proxy and TLS settings).
    params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
    async with Client(params, mode=mode, read_timeout_seconds=120) as client:
        if client.protocol_version != MODES[mode]:
            raise SystemExit(f"negotiated {client.protocol_version}, expected {MODES[mode]}")
        result = await client.call_tool("sage_status", {})
        if result.is_error or not result.structured_content:
            raise SystemExit(f"sage_status failed: {result.content}")
        server = result.structured_content["server"]
        print(
            f"[{client.protocol_version}] sage_status ok: {server['name']} {server['version']} ({server['milestone']})"
        )


def main() -> None:
    command = sys.argv[1:]
    if not command:
        raise SystemExit(__doc__)
    for mode in MODES:
        anyio.run(check, command, mode)


if __name__ == "__main__":
    main()
