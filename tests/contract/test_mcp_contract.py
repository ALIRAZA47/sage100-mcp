"""Tier 6 MCP contract tests (SPEC.md §15.5), against both protocol revisions.

Regenerate the tools/list snapshot after a deliberate change with:
    SAGE100_MCP_UPDATE_SNAPSHOTS=1 uv run pytest tests/contract
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ListToolsResult, TextContent

from sage100_mcp.config import load_config_or_defaults
from sage100_mcp.server import ROWS_ARE_DATA, build_server
from tests.paths import EXAMPLE_CONFIG

pytestmark = [pytest.mark.contract, pytest.mark.anyio]

SNAPSHOT = Path(__file__).parent / "snapshots" / "tools_list.json"
# Client mode -> the revision it must negotiate: "legacy" forces the 2025-11-25 initialize handshake;
# "auto" runs server/discover, as a 2026-07-28 client does, and must land on 2026-07-28.
MODES = {"legacy": "2025-11-25", "auto": "2026-07-28"}
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def tools_json(result: ListToolsResult) -> list[dict[str, Any]]:
    tools = sorted(result.tools, key=lambda tool: tool.name)
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


def text_of(result: CallToolResult) -> str:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


@pytest.fixture
def server() -> MCPServer:
    return build_server(load_config_or_defaults())


@pytest.mark.parametrize("mode", MODES)
async def test_tools_list_snapshot(server: MCPServer, mode: str) -> None:
    async with Client(server, mode=mode) as client:
        assert client.protocol_version == MODES[mode]
        tools = tools_json(await client.list_tools())
    if os.environ.get("SAGE100_MCP_UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(tools, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert tools == json.loads(SNAPSHOT.read_text(encoding="utf-8")), "tools/list changed; see module docstring"


async def test_every_tool_follows_the_rules(server: MCPServer) -> None:
    """§11.1: read-only annotations, an outputSchema, short descriptions, and the data warning on row tools."""
    async with Client(server) as client:
        tools = tools_json(await client.list_tools())
    assert tools
    for tool in tools:
        assert tool["annotations"] == READ_ONLY, tool["name"]
        assert tool["outputSchema"]["type"] == "object", tool["name"]
        assert tool.get("title"), tool["name"]
        assert len(tool["description"].split()) < 60, tool["name"]
        if "rows" in tool["outputSchema"].get("properties", {}):
            assert tool["description"].endswith(ROWS_ARE_DATA), tool["name"]


@pytest.mark.parametrize("mode", MODES)
async def test_sage_status(server: MCPServer, mode: str) -> None:
    async with Client(server, mode=mode) as client:
        schema = next(t.output_schema for t in (await client.list_tools()).tools if t.name == "sage_status")
        result = await client.call_tool("sage_status", {})
    assert result.is_error is False
    assert result.structured_content is not None
    assert schema is not None
    jsonschema.validate(result.structured_content, schema)
    assert result.structured_content["server"]["name"] == "sage100-mcp"
    assert "server: sage100-mcp" in text_of(result)


async def test_unexpected_errors_become_internal_without_leaking(
    server: MCPServer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Driver={MAS 90 4.0 ODBC Driver};PWD=hunter2")

    monkeypatch.setattr("sage100_mcp.server.collect_status", explode)
    with caplog.at_level(logging.ERROR, logger="sage100_mcp.server"):
        async with Client(server) as client:
            result = await client.call_tool("sage_status", {})
    assert result.is_error is True
    envelope = result.structured_content
    assert envelope is not None
    assert (envelope["code"], envelope["retryable"]) == ("INTERNAL", False)
    correlation_id = envelope["hint"].split()[3].rstrip(";")
    assert correlation_id in caplog.text
    assert "hunter2" not in result.model_dump_json()
    assert "hunter2" not in caplog.text, "the traceback is logged with the password redacted"
    assert "PWD=***" in caplog.text
    assert text_of(result).startswith("INTERNAL: Unexpected server error.")


@pytest.mark.parametrize("mode", MODES)
async def test_stdio_subprocess(mode: str, isolated_home: Path) -> None:
    """The real entry point over stdio. Debug logging is on: any log line on stdout would break the protocol."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sage100_mcp", "stdio", "--config", str(EXAMPLE_CONFIG)],
        env={
            "SAGE100_MCP_HOME": str(isolated_home),
            "SAGE100_MCP__SERVER__LOG_LEVEL": "debug",
            # The example's C:\ProgramData path would be a relative directory off Windows.
            "SAGE100_MCP__SERVER__AUDIT_DIR": str(isolated_home / "audit"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        },
    )
    async with Client(params, mode=mode, read_timeout_seconds=60) as client:
        assert client.protocol_version == MODES[mode]
        assert client.server_info is not None
        assert client.server_info.name == "sage100-mcp"
        names = sorted(tool.name for tool in (await client.list_tools()).tools)
        assert names == [tool["name"] for tool in json.loads(SNAPSHOT.read_text(encoding="utf-8"))]
        result = await client.call_tool("sage_status", {})
    assert result.structured_content is not None
    config = result.structured_content["config"]
    assert (config["path"], config["found"], config["valid"]) == (str(EXAMPLE_CONFIG), True, True)
    assert sorted(config["env_overrides"]) == ["SAGE100_MCP__SERVER__AUDIT_DIR", "SAGE100_MCP__SERVER__LOG_LEVEL"]
    assert (isolated_home / "audit").is_dir(), "the sage_status call was audited in the isolated home"
    assert [backend["name"] for backend in result.structured_content["backends"]] == ["sage_prod", "sage_premium"]
