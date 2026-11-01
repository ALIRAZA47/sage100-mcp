"""Streamable HTTP (SPEC.md §10.2, §10.3): the SDK's app behind Origin and Host checks, with authentication.

``sage100-mcp serve`` runs this on ``http.bind`` with uvicorn. Every request passes the Origin and Host
checks first (http/origin.py), then needs a bearer token: a static one from ``sage100-mcp token
create``, or, with ``http.auth: oauth``, an access token from ``http.oauth.issuer``. A missing or bad
token gets 401 with ``WWW-Authenticate``; a token without ``sage:read`` gets 403. With OAuth the server
also publishes its Protected Resource Metadata (RFC 9728), which tells clients where to get a token.

The endpoint's path is the path of ``http.public_url`` (``/mcp`` without one), so a proxy in front
forwards paths unchanged.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from sage100_mcp.config import HttpSection, LoadedConfig
from sage100_mcp.http.auth import READ_SCOPE, JwtVerifier, StaticTokenVerifier, TokenStore
from sage100_mcp.http.origin import LOOPBACK, OriginGuard, allowed_hosts
from sage100_mcp.runtime import Runtime
from sage100_mcp.server import build_server

TOKENS_FILE = "tokens.json"


def token_store(home: Path) -> TokenStore:
    return TokenStore(home / TOKENS_FILE)


def bind_host(http: HttpSection) -> str:
    """The bind address as a Host header names it: brackets kept around IPv6."""
    return http.bind.rpartition(":")[0]


def path_of(http: HttpSection) -> str:
    return urlsplit(http.endpoint).path or "/"


def serving_problems(http: HttpSection) -> list[str]:
    """Why ``serve`` should not start with these settings (§10.2: TLS whenever other machines connect)."""
    problems = []
    host = bind_host(http).strip("[]")
    if host not in LOOPBACK and not http.tls_cert and not (http.public_url or "").startswith("https://"):
        problems.append(
            f"http.bind {http.bind} takes connections from other machines, so it needs TLS: set http.tls_cert "
            "and http.tls_key, or bind 127.0.0.1:8765 behind a TLS proxy and set http.public_url to its https URL."
        )
    if host in ("0.0.0.0", "::") and not http.public_url:
        problems.append(
            f"http.bind {http.bind} listens on every address; set http.public_url to the URL clients use, so "
            "requests naming this server can be told from DNS-rebinding ones."
        )
    for name, path in (("tls_cert", http.tls_cert), ("tls_key", http.tls_key)):
        if path is not None and not path.is_file():
            problems.append(f"http.{name} {path} is not a readable file.")
    return problems


def verifier_for(http: HttpSection, home: Path) -> TokenVerifier:
    if http.auth == "oauth":
        assert http.oauth is not None  # the config requires the section with auth: oauth
        return JwtVerifier(http.oauth.issuer, http.oauth.audience)
    return StaticTokenVerifier(token_store(home))


def auth_settings(http: HttpSection) -> AuthSettings:
    if http.auth == "oauth":
        assert http.oauth is not None
        # The verifier checks the audience itself (http.oauth.audience), so the SDK's RFC 8707 resource
        # check stays off; resource_server_url is what the metadata document and WWW-Authenticate name.
        return AuthSettings(
            issuer_url=http.oauth.issuer,
            resource_server_url=http.endpoint,
            required_scopes=[READ_SCOPE],
            validate_token_resource=False,
        )
    # Static tokens have no authorization server and no metadata document; the SDK requires an
    # issuer_url all the same, and uses it for nothing without one.
    return AuthSettings(
        issuer_url=http.endpoint,
        resource_server_url=None,
        required_scopes=[READ_SCOPE],
    )


def build_http_app(loaded: LoadedConfig, runtime: Runtime, *, verifier: TokenVerifier | None = None) -> ASGIApp:
    """The ASGI app ``serve`` runs: Origin and Host checks, then authentication, then MCP."""
    http = loaded.config.http
    server = build_server(
        loaded, runtime, token_verifier=verifier or verifier_for(http, runtime.home), auth=auth_settings(http)
    )
    app = server.streamable_http_app(
        streamable_http_path=path_of(http),
        # OriginGuard does these checks, before authentication; the SDK's own would come after it.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=http.host,
    )
    hosts = allowed_hosts(bind_host(http), http.port, http.public_url)
    return OriginGuard(app, origins=http.allowed_origins, hosts=hosts)
