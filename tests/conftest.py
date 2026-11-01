"""Every test runs with an isolated sage100-mcp home and without SAGE100_MCP__ overrides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sage100_mcp.config import CONFIG_ENV, ENV_PREFIX, HOME_ENV


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the developer's own config and environment out of every test."""
    for name in list(os.environ):
        if name.upper().startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv(HOME_ENV, str(home))
    # Never read or prompt for the developer's real keyring; tests that need one patch keyring itself.
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    for name in list(os.environ):
        if name.upper().startswith("SAGE100_MCP_CRED_"):
            monkeypatch.delenv(name)
    return home


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def write_config(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Write YAML text to a config file and return its path."""

    def write(text: str, name: str = "config.yaml") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return write
