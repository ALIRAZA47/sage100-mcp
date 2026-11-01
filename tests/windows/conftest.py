"""The real Sage 100 install the Windows tests run against (SPEC.md §15.3): see test_m2_gate.py's docstring
for the SAGE100_MCP_TEST_PROVIDEX_* settings."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sage100_mcp.config import Config, LoadedConfig
from sage100_mcp.runtime import Runtime

PREFIX = "SAGE100_MCP_TEST_PROVIDEX_"
SAGE_RUNNER = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="needs Windows"),
    pytest.mark.skipif(not os.environ.get(PREFIX + "DIRECTORY"), reason=f"set {PREFIX}DIRECTORY and the login"),
]


def helper_leg() -> bool:
    """The 32-bit leg: Sage's 32-bit driver through the 32-bit helper (docs/decisions.md D67)."""
    return bool(os.environ.get(PREFIX + "HELPER"))


def setting(name: str, default: str | None = None) -> str:
    value = os.environ.get(PREFIX + name, default)
    assert value, f"set {PREFIX}{name}"
    return value


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    company = setting("COMPANY", "ABC")
    backend: dict[str, Any] = {"type": "providex", "directory": setting("DIRECTORY"), "credential": "sage100-mcp/gate"}
    if os.environ.get(PREFIX + "CS_HOST"):
        backend |= {"transport": "cs", "cs": {"host": setting("CS_HOST"), "port": int(setting("CS_PORT", "20222"))}}
    if helper_leg():
        backend |= {"driver_bitness": "32", "helper": setting("HELPER")}
    data = {"backends": {"sage": backend}, "companies": {"default": company, "allow": [company]}}
    os.environ["SAGE100_MCP_CRED_GATE_USER"] = setting("USER")
    os.environ["SAGE100_MCP_CRED_GATE_PASSWORD"] = setting("PASSWORD")
    loaded = LoadedConfig(config=Config.model_validate(data), path=Path("gate.yaml"), found=True)
    started = Runtime(loaded, home=tmp_path_factory.mktemp("home"))
    started.start(background=False)
    assert not started.errors
    yield started
    started.close()
