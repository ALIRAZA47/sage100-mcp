"""Origin and Host checks ahead of everything else on the HTTP endpoint (SPEC.md §10.3).

A request whose ``Origin`` is not in ``http.allowed_origins`` gets 403, as the MCP spec requires since
2025-11-25; non-browser clients such as Claude Code send no Origin and pass. A request whose ``Host`` is
not the server's own name (``http.public_url``, the bind address, or loopback names when bound to
loopback) gets 421, which stops DNS-rebinding pages from reaching a local server. Both checks run
before authentication, so a bad Origin is refused even with a good token.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def allowed_hosts(bind_host: str, bind_port: int, public_url: str | None) -> set[str]:
    """Host header values that name this server: host:port, plus the bare host on a default port."""
    hosts = {f"{bind_host}:{bind_port}".lower()}
    if bind_host in LOOPBACK:
        hosts |= {f"{name}:{bind_port}" for name in ("127.0.0.1", "localhost", "[::1]")}
    if public_url:
        parts = urlsplit(public_url)
        name = (parts.hostname or "").lower()
        if ":" in name:
            name = f"[{name}]"
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 443)
        hosts.add(f"{name}:{port}")
        if port == _DEFAULT_PORTS.get(parts.scheme):
            hosts.add(name)
    return hosts


class OriginGuard:
    """Pure ASGI middleware; lifespan and other scopes pass straight through."""

    def __init__(self, app: ASGIApp, *, origins: list[str], hosts: set[str]) -> None:
        self.app = app
        self.origins = {origin.rstrip("/").lower() for origin in origins}
        self.hosts = {host.lower() for host in hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            if origin is not None and origin.rstrip("/").lower() not in self.origins:
                response = PlainTextResponse("Origin not allowed.", status_code=403)
                await response(scope, receive, send)
                return
            if (headers.get("host") or "").lower() not in self.hosts:
                response = PlainTextResponse("Unknown Host.", status_code=421)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
