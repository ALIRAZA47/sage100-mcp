"""The Windows service end to end (SPEC.md §16.2): install, start, answer over HTTP, stop, remove.

    uv run python scripts/service_smoke.py      # on Windows, as Administrator (the CI runner is)

It installs the service as LocalSystem with a temporary home and a config that serves no company,
creates a token, waits for sage_status to answer over HTTP, and removes the service again whatever
happened. Exit status 0 means the service ran and answered.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

PORT = 18766


def cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    done = subprocess.run([sys.executable, "-m", "sage100_mcp", *args], capture_output=True, text=True, env=env)
    print(f"$ sage100-mcp {' '.join(args)} -> {done.returncode}\n{done.stdout}{done.stderr}".rstrip())
    return done


async def status(token: str) -> str:
    client = httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}, timeout=30)
    async with Client(streamable_http_client(f"http://127.0.0.1:{PORT}/mcp", http_client=client)) as session:
        result = await session.call_tool("sage_status", {})
    assert result.structured_content is not None, result
    return str(result.structured_content["server"]["version"])


def main() -> int:
    if sys.platform != "win32":
        print("service_smoke.py needs Windows", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="sage100-service-") as directory:
        home = Path(directory) / "home"
        config = Path(directory) / "config.yaml"
        config.write_text(f"http:\n  bind: 127.0.0.1:{PORT}\n", encoding="utf-8")
        env = os.environ | {"SAGE100_MCP_HOME": str(home)}
        created = cli("token", "create", "--principal", "service-smoke", env=env)
        token = re.search(r"s1mcp_[A-Za-z0-9_-]+", created.stdout)
        if created.returncode or not token:
            return 1
        try:
            if cli("service", "install", "--account", "LocalSystem", "--config", str(config), env=env).returncode:
                return 1
            if cli("service", "start", env=env).returncode:
                return 1
            deadline = time.monotonic() + 60
            while True:
                try:
                    version = anyio.run(status, token.group(0))
                    print(f"the service answered sage_status: version {version}")
                    return 0
                except Exception as exc:
                    if time.monotonic() > deadline:
                        print(f"no answer within 60 s: {type(exc).__name__}: {exc}")
                        return 1
                    time.sleep(2)
        finally:
            cli("service", "remove", env=env)
            log = home / "logs" / "sage100-mcp.log"
            if log.is_file():
                print(f"--- {log}\n{log.read_text(encoding='utf-8')[-4000:]}")


if __name__ == "__main__":
    raise SystemExit(main())
