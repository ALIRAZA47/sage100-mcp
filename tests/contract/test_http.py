"""The MCP contract over Streamable HTTP (SPEC.md §10.3, §15.5): authentication, Origin and Host checks,
per-principal state, cursors, the rate limit and the audit log, for both protocol revisions.

The app runs in process: the SDK's client talks to it through httpx's ASGI transport, with the app's
lifespan entered by hand (ASGITransport sends no lifespan events). test_tls.py runs the real
``serve`` command over TLS.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.types import ASGIApp

from sage100_mcp.errors import ErrorCode
from sage100_mcp.http.app import auth_settings, build_http_app, serving_problems, token_store
from sage100_mcp.http.auth import JwtVerifier, StaticTokenVerifier, metadata_urls
from sage100_mcp.http.origin import OriginGuard
from sage100_mcp.runtime import Runtime
from sage100_mcp.server import build_server
from tests.contract.test_mcp_contract import SNAPSHOT, tools_json
from tests.fake_backend import FakeExecutor, fake_config, fake_runtime, result

pytestmark = [pytest.mark.contract, pytest.mark.anyio]

MODES = {"legacy": "2025-11-25", "auto": "2026-07-28"}
LOCAL = "http://127.0.0.1:8765"
ISSUER = "https://login.example.com/tenant"
PUBLIC = "https://sage-mcp.example.com/mcp"
QUERY = "SELECT CustomerNo, CreditLimit FROM AR_Customer WHERE CreditLimit > ?"
ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "raw", "version": "1"}},
}


@dataclass
class Site:
    app: ASGIApp
    runtime: Runtime
    fake: FakeExecutor
    tokens: dict[str, str]  # name -> secret
    base: str = LOCAL


@asynccontextmanager
async def running(app: ASGIApp) -> AsyncIterator[None]:
    inner = cast(Starlette, cast(OriginGuard, app).app)
    async with inner.router.lifespan_context(inner):
        yield


def http(site: Site, token: str | None = None, **headers: str) -> httpx2.AsyncClient:
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=site.app), base_url=site.base, headers=headers)


@asynccontextmanager
async def connect(site: Site, who: str, mode: str = "auto", **headers: str) -> AsyncIterator[Client]:
    transport = streamable_http_client(f"{site.base}/mcp", http_client=http(site, site.tokens.get(who, who), **headers))
    async with Client(transport, mode=mode) as client:
        yield client


def customers(count: int) -> Any:
    rows = [(f"C{n:04}", Decimal("100.00")) for n in range(count)]
    return result([("CustomerNo", "varchar(20)"), ("CreditLimit", "decimal(13,2)")], rows)


def structured(outcome: CallToolResult) -> dict[str, Any]:
    content: dict[str, Any] | None = outcome.structured_content
    assert content is not None, outcome
    return content


def ok(outcome: CallToolResult) -> dict[str, Any]:
    content = outcome.content[0]
    assert isinstance(content, TextContent)
    assert outcome.is_error is False, content.text
    return structured(outcome)


def failed(outcome: CallToolResult, code: ErrorCode) -> dict[str, Any]:
    assert outcome.is_error is True
    error = structured(outcome)
    assert error["code"] == code, error
    return error


def make_site(tmp_path: Path, *, limits: dict[str, Any] | None = None, **http_settings: Any) -> Site:
    settings = {"bind": "127.0.0.1:8765", "allowed_origins": ["https://claude.ai"], **http_settings}
    runtime, fake = fake_runtime(tmp_path / "home", limits=limits, http=settings)
    store = token_store(runtime.home)
    tokens = {
        "alice": store.create("alice")[1],
        "bob": store.create("bob")[1],
        "emp-only": store.create("emp-only", ["EMP"])[1],
    }
    return Site(build_http_app(runtime.loaded, runtime), runtime, fake, tokens)


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    made = make_site(tmp_path)
    yield made
    made.runtime.close()


# --- the protocol over HTTP ------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_the_tools_are_the_same_over_http(site: Site, mode: str) -> None:
    async with running(site.app), connect(site, "alice", mode) as client:
        assert client.protocol_version == MODES[mode]
        assert tools_json(await client.list_tools()) == json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        status = ok(await client.call_tool("sage_status", {}))
        companies = ok(await client.call_tool("list_companies", {}))
    assert status["server"]["version"]
    assert [c["code"] for c in companies["companies"]] == ["EMP", "PCC"]


# --- authentication (§10.3, §15.5) --------------------------------------------------------------------------


async def test_a_missing_or_unknown_token_gets_401_with_www_authenticate(site: Site) -> None:
    async with running(site.app):
        for token in (None, "s1mcp_not-a-token", site.tokens["alice"] + "x"):
            async with http(site, token, **ACCEPT) as raw:
                response = await raw.post("/mcp", json=INITIALIZE)
            assert response.status_code == 401, token
            assert response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')
        async with http(site, site.tokens["alice"], **ACCEPT) as raw:
            assert (await raw.post("/mcp", json=INITIALIZE)).status_code == 200


async def test_a_revoked_token_is_refused_at_once(site: Site) -> None:
    store = token_store(site.runtime.home)
    record = next(r for r in store.records() if r.principal == "bob")
    async with running(site.app):
        async with connect(site, "bob") as client:
            ok(await client.call_tool("list_companies", {}))
        assert store.revoke(record.id)
        async with http(site, site.tokens["bob"], **ACCEPT) as raw:
            assert (await raw.post("/mcp", json=INITIALIZE)).status_code == 401


async def test_the_token_file_holds_hashes_only(site: Site) -> None:
    text = (site.runtime.home / "tokens.json").read_text(encoding="utf-8")
    assert all(secret not in text for secret in site.tokens.values())
    assert text.count('"sha256"') == 3


# --- Origin and Host (§10.3) ---------------------------------------------------------------------------------


async def test_a_disallowed_origin_gets_403_even_with_a_good_token(site: Site) -> None:
    async with running(site.app):
        for origin, status in (("https://evil.example", 403), ("null", 403), ("https://claude.ai", 200)):
            async with http(site, site.tokens["alice"], origin=origin, **ACCEPT) as raw:
                response = await raw.post("/mcp", json=INITIALIZE)
            assert response.status_code == status, origin
        async with http(site, None, origin="https://evil.example", **ACCEPT) as raw:
            assert (await raw.post("/mcp", json=INITIALIZE)).status_code == 403, "checked before the token"


async def test_an_unknown_host_gets_421(site: Site) -> None:
    async with running(site.app):
        for host, status in (("evil.example", 421), ("127.0.0.1:9999", 421), ("localhost:8765", 200)):
            async with http(site, site.tokens["alice"], host=host, **ACCEPT) as raw:
                response = await raw.post("/mcp", json=INITIALIZE)
            assert response.status_code == status, host


def test_serve_refuses_settings_that_would_expose_it(tmp_path: Path) -> None:
    def problems(**settings: Any) -> list[str]:
        return serving_problems(fake_config(http=settings).config.http)

    assert problems() == []
    assert "needs TLS" in problems(bind="10.0.0.5:8765")[0]
    assert problems(bind="10.0.0.5:8765", public_url="https://sage.example.com/mcp") == []
    assert len(problems(bind="0.0.0.0:8765")) == 2
    missing = problems(bind="10.0.0.5:8765", tls_cert=str(tmp_path / "c.pem"), tls_key=str(tmp_path / "k.pem"))
    assert [p.split()[0] for p in missing] == ["http.tls_cert", "http.tls_key"]


# --- per-principal state (§10.3) ------------------------------------------------------------------------------


async def test_a_company_scoped_token_sees_only_its_companies(site: Site) -> None:
    async with running(site.app), connect(site, "emp-only") as client:
        companies = ok(await client.call_tool("list_companies", {}))
        refused = failed(await client.call_tool("list_tables", {"company": "PCC"}), ErrorCode.COMPANY_NOT_FOUND)
        tables = ok(await client.call_tool("list_tables", {"module": "AR"}))
    assert [c["code"] for c in companies["companies"]] == ["EMP"]
    assert refused["choices"] == ["EMP"]
    assert tables["company"] == "EMP", "the one company in scope needs no naming"


async def test_each_principal_has_its_own_active_company(site: Site) -> None:
    async with running(site.app):
        async with connect(site, "alice") as alice:
            ok(await alice.call_tool("set_company", {"company": "PCC"}))
        async with connect(site, "bob", mode="legacy") as bob:
            failed(await bob.call_tool("list_tables", {}), ErrorCode.COMPANY_REQUIRED)
            ok(await bob.call_tool("set_company", {"company": "EMP"}))
            assert ok(await bob.call_tool("list_tables", {}))["company"] == "EMP"
        async with connect(site, "alice") as alice:  # a new connection: the state is the principal's
            assert ok(await alice.call_tool("list_tables", {}))["company"] == "PCC"


async def test_a_cursor_from_another_principal_is_rejected(site: Site) -> None:
    site.fake.results += [customers(30), customers(30)]
    arguments = {"company": "EMP", "sql": QUERY, "params": [0], "max_rows": 10}
    async with running(site.app):
        async with connect(site, "alice") as alice:
            first = ok(await alice.call_tool("run_query", arguments))
        assert first["next_cursor"]
        sent = len(site.fake.sent)
        async with connect(site, "bob") as bob:
            outcome = await bob.call_tool("run_query", {**arguments, "cursor": first["next_cursor"]})
            refused = failed(outcome, ErrorCode.CURSOR_INVALID)
        assert "another caller" in refused["message"]
        assert len(site.fake.sent) == sent
        async with connect(site, "alice", mode="legacy") as alice:
            second = ok(await alice.call_tool("run_query", {**arguments, "cursor": first["next_cursor"]}))
    assert second["rows"][0][0] == "C0010"


async def test_the_rate_limit_is_per_principal(tmp_path: Path) -> None:
    site = make_site(tmp_path, limits={"rate_per_min": 3})
    async with running(site.app), connect(site, "alice") as alice, connect(site, "bob") as bob:
        for _ in range(3):
            ok(await alice.call_tool("list_companies", {}))
        limited = failed(await alice.call_tool("list_companies", {}), ErrorCode.RATE_LIMITED)
        ok(await bob.call_tool("list_companies", {}))
    site.runtime.close()
    assert limited["retryable"] is True
    assert 1 <= limited["retry_after_s"] <= 60
    assert f"Wait {limited['retry_after_s']} s" in limited["hint"]


# --- the audit log (§9.4) ------------------------------------------------------------------------------------


async def test_every_call_is_audited_without_secrets_or_values(site: Site) -> None:
    site.fake.results += [customers(3)]
    async with running(site.app), connect(site, "alice", **{"user-agent": "audit-test"}) as client:
        ok(await client.call_tool("run_query", {"company": "EMP", "sql": QUERY + " AND CustomerNo <> 'O''Brien'",
                                                "params": [1234.5]}))  # fmt: skip
        failed(await client.call_tool("run_query", {"company": "EMP", "sql": "DELETE FROM AR_Customer"}),
               ErrorCode.WRITE_BLOCKED)  # fmt: skip
        failed(await client.call_tool("list_tables", {"company": "XYZ"}), ErrorCode.COMPANY_NOT_FOUND)
    files = list((site.runtime.home / "audit").glob("*.jsonl"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines()]
    record = next(r for r in store_records(site) if r.principal == "alice")
    assert [line["tool"] for line in lines] == ["run_query", "run_query", "list_tables"]
    first = lines[0]
    assert (first["principal"], first["name"]) == (f"token:{record.id}", "alice")
    assert (first["transport"], first["company"], first["mode"], first["rows"], first["outcome"]) == (
        "http", "EMP", "native", 3, "ok"
    )  # fmt: skip
    assert first["client"].startswith("mcp")
    assert first["args"] == ["company", "params", "sql"]
    assert "'…'" in first["sql"][0]
    assert "1234.5" not in text
    assert "Brien" not in text
    assert (lines[1]["outcome"], lines[1]["code"]) == ("error", "WRITE_BLOCKED")
    assert (lines[2]["code"], lines[2].get("company")) == ("COMPANY_NOT_FOUND", None)
    assert all(secret not in text for secret in site.tokens.values())


def store_records(site: Site) -> list[Any]:
    return token_store(site.runtime.home).records()


# --- OAuth 2.1 resource server (§10.3) -----------------------------------------------------------------------


@dataclass
class Issuer:
    key: rsa.RSAPrivateKey
    jwks: dict[str, Any]

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": "sage100-mcp", "sub": "user-1", "exp": now + 300, "iat": now,
                   "scope": "sage:read"} | claims  # fmt: skip
        payload = {name: value for name, value in payload.items() if value is not None}
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "k1"})


def new_issuer() -> Issuer:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return Issuer(key, {"keys": [public | {"kid": "k1", "use": "sig", "alg": "RS256"}]})


@pytest.fixture(scope="module")
def issuer() -> Issuer:
    return new_issuer()


@pytest.fixture
def oauth(tmp_path: Path, issuer: Issuer) -> Iterator[Site]:
    settings = {"auth": "oauth", "oauth": {"issuer": ISSUER, "audience": "sage100-mcp"}, "public_url": PUBLIC}
    runtime, fake = fake_runtime(tmp_path / "home", http={"bind": "127.0.0.1:8765", **settings})
    verifier = JwtVerifier(ISSUER, "sage100-mcp", keys=issuer.jwks)
    yield Site(build_http_app(runtime.loaded, runtime, verifier=verifier), runtime, fake, {}, base=PUBLIC[:-4])
    runtime.close()


async def test_oauth_publishes_protected_resource_metadata(oauth: Site) -> None:
    async with running(oauth.app), http(oauth, None, **ACCEPT) as raw:
        unauthenticated = await raw.post("/mcp", json=INITIALIZE)
        metadata = await raw.get("/.well-known/oauth-protected-resource/mcp")
    assert unauthenticated.status_code == 401
    expected = 'resource_metadata="https://sage-mcp.example.com/.well-known/oauth-protected-resource/mcp"'
    assert expected in unauthenticated.headers["www-authenticate"]
    document = metadata.json()
    assert document["resource"] == PUBLIC
    assert document["authorization_servers"] == [ISSUER]
    assert document["scopes_supported"] == ["sage:read"]


async def test_oauth_access_tokens(oauth: Site, issuer: Issuer) -> None:
    good = issuer.token(sub="ann@example.com", scope="sage:read sage:company:EMP")
    async with running(oauth.app):
        async with connect(oauth, good) as client:
            companies = ok(await client.call_tool("list_companies", {}))
        assert [c["code"] for c in companies["companies"]] == ["EMP"]
        refused = {
            "wrong audience": issuer.token(aud="another-api"),
            "wrong issuer": issuer.token(iss="https://login.example.com/other"),
            "expired": issuer.token(exp=int(time.time()) - 120),
            "no expiry": issuer.token(exp=None),
            "no subject": issuer.token(sub=None),
            "another key": new_issuer().token(),
            "not a JWT": "s1mcp_static-tokens-are-not-accepted-here",
        }
        for why, token in refused.items():
            async with http(oauth, token, **ACCEPT) as raw:
                response = await raw.post("/mcp", json=INITIALIZE)
            assert response.status_code == 401, why
        async with http(oauth, issuer.token(scope="openid profile"), **ACCEPT) as raw:
            response = await raw.post("/mcp", json=INITIALIZE)
        assert response.status_code == 403
        assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


async def test_oauth_principals_are_issuer_and_subject(oauth: Site, issuer: Issuer) -> None:
    ann, bea = issuer.token(sub="ann", scp=["sage:read"], scope=None), issuer.token(sub="bea")
    async with running(oauth.app):
        async with connect(oauth, ann) as client:
            ok(await client.call_tool("set_company", {"company": "PCC"}))
        async with connect(oauth, issuer.token(sub="ann")) as client:  # a fresh token for the same person
            assert ok(await client.call_tool("list_tables", {}))["company"] == "PCC"
        async with connect(oauth, bea) as client:
            failed(await client.call_tool("list_tables", {}), ErrorCode.COMPANY_REQUIRED)
    line = json.loads(next((oauth.runtime.home / "audit").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0])
    assert (line["principal"], line["name"]) == (f"oauth:{ISSUER}#ann", "ann")


def test_issuer_metadata_locations() -> None:
    assert metadata_urls("https://login.example.com/tenant/") == [
        "https://login.example.com/.well-known/oauth-authorization-server/tenant",
        "https://login.example.com/tenant/.well-known/openid-configuration",
    ]
    assert (
        metadata_urls("https://login.example.com")[0]
        == "https://login.example.com/.well-known/oauth-authorization-server"
    )


async def test_a_request_without_http_context_sees_nothing_on_an_http_server(site: Site) -> None:
    """Fail closed: a server built for HTTP never treats a caller as the local process."""
    http_settings = site.runtime.loaded.config.http
    server = build_server(
        site.runtime.loaded,
        site.runtime,
        token_verifier=StaticTokenVerifier(token_store(site.runtime.home)),
        auth=auth_settings(http_settings),
    )
    async with Client(server) as client:  # in process: no HTTP request behind the call
        companies = ok(await client.call_tool("list_companies", {}))
    assert companies["companies"] == []
