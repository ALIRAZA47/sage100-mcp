"""sage100-mcp: a read-only, Sage-aware MCP server for US Sage 100."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sage100-mcp")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"
