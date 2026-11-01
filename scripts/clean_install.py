"""The M6 exit gate's measurable part: a clean install to the first answer, timed (SPEC.md §18).

    SAGE100_MCP_TEST_SQLSERVER=localhost SAGE100_MCP_TEST_SA_PASSWORD=... uv run python scripts/clean_install.py

The gate is a clean-VM install that reaches a first answer within 15 minutes, following docs/setup.md. No
Windows VM with Sage is available here, so this script times the same steps on Linux against the Premium
test bed (tests/premium_testbed.py). It works in a fresh environment that sees nothing of this checkout:
no package cache, no keyring, and an empty home.

1. Build the wheel. This stands in for downloading the MSI or the PyPI package.
2. Install the wheel and its dependencies into a new virtual environment, without the uv cache.
3. Run ``sage100-mcp init --yes`` for Premium, taking the password from an environment variable.
4. Run ``sage100-mcp doctor``.
5. Get a first answer: an MCP client starts ``sage100-mcp stdio``, then calls list_companies and ar_aging.

It prints each step's time and the total. It fails when a step fails or the total passes 15 minutes. The
time a person spends reading the guide isn't measured; docs/setup.md keeps a sheet for timing the Windows run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import premium_testbed as testbed

REPO = Path(__file__).resolve().parents[1]
LIMIT_S = 15 * 60
PASSWORD_ENV = "CLEAN_INSTALL_PASSWORD"


def step(name: str, timings: list[tuple[str, float]], command: list[str], env: dict[str, str]) -> str:
    started = time.perf_counter()
    done = subprocess.run(command, capture_output=True, text=True, env=env, cwd=env["HOME"])
    timings.append((name, time.perf_counter() - started))
    output = (done.stdout + done.stderr).strip()
    if done.returncode:
        raise SystemExit(f"{name} failed ({done.returncode}):\n{output}")
    return output


async def first_answer(program: Path, config: Path, env: dict[str, str]) -> tuple[list[str], int]:
    params = StdioServerParameters(command=str(program), args=["stdio", "--config", str(config)], env=env)
    async with Client(params, read_timeout_seconds=120) as client:
        companies = await client.call_tool("list_companies", {})
        aging = await client.call_tool("ar_aging", {"company": "EMP", "as_of": testbed.AS_OF.isoformat()})
    for result in (companies, aging):
        if result.is_error or not result.structured_content:
            raise SystemExit(f"no answer: {result.content}")
    found: Any = companies.structured_content
    codes = [company["code"] for company in found["companies"]]
    rows: Any = aging.structured_content
    return codes, len(rows["rows"])


def main() -> int:
    server, sa_password = os.environ.get(testbed.SERVER_ENV), os.environ.get(testbed.SA_PASSWORD_ENV)
    if not server or not sa_password:
        print(f"set {testbed.SERVER_ENV} and {testbed.SA_PASSWORD_ENV}", file=sys.stderr)
        return 2
    testbed.build(server, sa_password)  # the site's databases exist before anyone installs anything
    uv = shutil.which("uv")
    if uv is None:
        print("needs uv on PATH", file=sys.stderr)
        return 2
    timings: list[tuple[str, float]] = []
    root = Path(tempfile.mkdtemp(prefix="sage100-clean-"))
    try:
        (root / "user").mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith(("SAGE100_MCP_", "PYTHON"))}
        env |= {"HOME": str(root / "user"), "SAGE100_MCP_HOME": str(root / "home"), "UV_NO_CACHE": "1",
                PASSWORD_ENV: testbed.READER_PASSWORD}  # fmt: skip
        env.pop("VIRTUAL_ENV", None)
        step("build the wheel", timings, [uv, "build", "--wheel", "--out-dir", str(root / "dist"), str(REPO)], env)
        wheel = next((root / "dist").glob("sage100_mcp-*.whl"))
        venv = root / "venv"
        step("install it and its dependencies", timings, [uv, "venv", "--python", "3.12", str(venv)], env)
        step("install it and its dependencies", timings, [uv, "pip", "install", "--python", str(venv), str(wheel)], env)
        program = venv / "bin" / "sage100-mcp"
        config = root / "home" / "config.yaml"
        said = step("sage100-mcp init", timings, [
            str(program), "init", "--config", str(config), "--yes", "--type", "sqlserver", "--server", server,
            "--trust-server-certificate", "--user", testbed.READER, "--password-env", PASSWORD_ENV, "--company", "EMP",
        ], env)  # fmt: skip
        print(said)
        # docs/setup.md: a host with no keyring takes the login from the server's environment.
        env |= {"SAGE100_MCP_CRED_PREMIUM_USER": testbed.READER,
                "SAGE100_MCP_CRED_PREMIUM_PASSWORD": testbed.READER_PASSWORD}  # fmt: skip
        print(step("sage100-mcp doctor", timings, [str(program), "doctor", "--config", str(config)], env))
        started = time.perf_counter()
        codes, rows = anyio.run(first_answer, program, config, env)
        timings.append(("first answer (stdio)", time.perf_counter() - started))
        print(f"companies {', '.join(codes)}; ar_aging for EMP: {rows} rows")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    merged: dict[str, float] = {}
    for name, seconds in timings:
        merged[name] = merged.get(name, 0.0) + seconds
    total = sum(merged.values())
    print()
    for name, seconds in merged.items():
        print(f"  {name:34} {seconds:7.1f} s")
    print(f"  {'total':34} {total:7.1f} s  (gate: under {LIMIT_S // 60} minutes)")
    return 0 if total < LIMIT_S else 1


if __name__ == "__main__":
    raise SystemExit(main())
