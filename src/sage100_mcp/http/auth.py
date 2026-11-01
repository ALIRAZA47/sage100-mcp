"""Who may call over HTTP (SPEC.md §10.3, §16.2): static bearer tokens, or OAuth 2.1 access tokens.

Static tokens suit single-tenant installs. ``sage100-mcp token create`` prints a token once and keeps
only its SHA-256 hash, with the principal's name and any company limit, in ``<home>/tokens.json``.
``token revoke`` takes effect on the next request; nothing needs restarting.

OAuth access tokens are JWTs from ``http.oauth.issuer``: the signature is checked against the issuer's
published keys (found through its RFC 8414 or OpenID metadata), and the issuer, audience
(``http.oauth.audience``) and expiry must match. Either kind of token must carry the ``sage:read``
scope; ``sage:company:ABC`` scopes, when present, limit the companies its principal can see.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import BaseModel

from sage100_mcp.principals import Principal

READ_SCOPE = "sage:read"
COMPANY_SCOPE = "sage:company:"
TOKEN_PREFIX = "s1mcp_"
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def principal_of(token: AccessToken) -> Principal:
    """The principal a verified token stands for; its company scopes, if any, limit what it sees."""
    claims = token.claims or {}
    companies = {s.removeprefix(COMPANY_SCOPE).upper() for s in token.scopes if s.startswith(COMPANY_SCOPE)}
    return Principal(
        id=str(claims.get("principal") or f"client:{token.client_id}"),
        label=str(claims.get("label") or token.subject or token.client_id),
        companies=frozenset(companies) if companies else None,
        transport="http",
    )


# --- static bearer tokens ---------------------------------------------------------------------------


class TokenRecord(BaseModel):
    id: str  # short and public: names the token in lists, the audit log and revocation
    principal: str
    companies: list[str] | None = None
    created: str
    sha256: str


class TokenStore:
    """``<home>/tokens.json``: token hashes only. Reloaded when the file changes, so revocation is live."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._records: list[TokenRecord] = []

    def records(self) -> list[TokenRecord]:
        with self._lock:
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                self._stamp, self._records = None, []
                return []
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)  # each write replaces the file: a new inode
            if stamp != self._stamp:
                data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                self._records = [TokenRecord.model_validate(item) for item in data.get("tokens", [])]
                self._stamp = stamp
            return list(self._records)

    def _write(self, records: list[TokenRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = {"tokens": [record.model_dump() for record in records]}
        temporary.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)

    def create(self, principal: str, companies: list[str] | None = None) -> tuple[TokenRecord, str]:
        """A new token for ``principal``: the record kept, and the secret, which is shown once."""
        secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
        existing = self.records()
        token_id = secrets.token_hex(4)
        while any(r.id == token_id for r in existing):
            token_id = secrets.token_hex(4)
        record = TokenRecord(
            id=token_id,
            principal=principal,
            companies=sorted({c.upper() for c in companies}) if companies else None,
            created=datetime.now(UTC).isoformat(timespec="seconds"),
            sha256=_digest(secret),
        )
        self._write([*existing, record])
        return record, secret

    def revoke(self, token_id: str) -> bool:
        records = self.records()
        kept = [r for r in records if r.id != token_id]
        if len(kept) == len(records):
            return False
        self._write(kept)
        return True

    def find(self, secret: str) -> TokenRecord | None:
        digest = _digest(secret)
        found = None
        for record in self.records():
            if hmac.compare_digest(record.sha256, digest):
                found = record
        return found


class StaticTokenVerifier(TokenVerifier):
    def __init__(self, store: TokenStore) -> None:
        self.store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        record = await anyio.to_thread.run_sync(self.store.find, token)
        if record is None:
            return None
        scopes = [READ_SCOPE, *(COMPANY_SCOPE + c for c in record.companies or [])]
        return AccessToken(
            token=token,
            client_id=record.principal,
            scopes=scopes,
            subject=record.principal,
            claims={"principal": f"token:{record.id}", "label": record.principal},
        )


# --- OAuth 2.1 access tokens --------------------------------------------------------------------------


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data: dict[str, Any] = json.load(response)
    return data


def clock_skew(issuer: str, timeout: float = 5) -> float:
    """Seconds the issuer's clock (its HTTP Date header) is ahead of this machine's."""
    url = metadata_urls(issuer)[0]
    before = datetime.now(UTC)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            stamp = response.headers.get("Date")
    except urllib.error.HTTPError as exc:  # a 404 for one metadata form still carries the server's Date
        stamp = exc.headers.get("Date")
    after = datetime.now(UTC)
    if not stamp:
        raise OSError(f"{url} sent no Date header")
    return (parsedate_to_datetime(stamp) - (before + (after - before) / 2)).total_seconds()


def metadata_urls(issuer: str) -> list[str]:
    """Where an issuer publishes its metadata: RFC 8414 puts the issuer's path after the well-known
    segment, OpenID Connect Discovery appends the well-known segment to the issuer."""
    parts = urlsplit(issuer)
    path = parts.path.rstrip("/")
    return [
        f"{parts.scheme}://{parts.netloc}/.well-known/oauth-authorization-server{path}",
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
    ]


class JwtVerifier(TokenVerifier):
    """Validates JWT access tokens from one issuer; ``keys`` replaces discovery (tests, pinned keys)."""

    def __init__(self, issuer: str, audience: str, *, keys: dict[str, Any] | None = None, timeout: float = 10) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.timeout = timeout
        self._keys = jwt.PyJWKSet.from_dict(keys) if keys is not None else None
        self._client: jwt.PyJWKClient | None = None
        self._lock = threading.Lock()

    def jwks_uri(self) -> str:
        """The issuer's key set URL, from its published metadata."""
        for url in metadata_urls(self.issuer):
            try:
                uri = _get_json(url, self.timeout).get("jwks_uri")
            except (OSError, ValueError):
                continue
            if uri:
                return str(uri)
        raise jwt.PyJWKClientError(f"{self.issuer} publishes no jwks_uri in its metadata")

    def _jwks_client(self) -> jwt.PyJWKClient:
        with self._lock:
            if self._client is None:
                self._client = jwt.PyJWKClient(self.jwks_uri(), cache_keys=True, timeout=self.timeout)
            return self._client

    def _key(self, token: str) -> Any:
        if self._keys is not None:
            kid = jwt.get_unverified_header(token).get("kid")
            for key in self._keys.keys:
                if kid is None or key.key_id == kid:
                    return key
            raise jwt.PyJWKError(f"no key {kid!r}")
        return self._jwks_client().get_signing_key_from_jwt(token)

    def decode(self, token: str) -> dict[str, Any]:
        claims: dict[str, Any] = jwt.decode(
            token,
            self._key(token),
            algorithms=ALGORITHMS,
            audience=self.audience,
            issuer=[self.issuer, self.issuer + "/"],
            options={"require": ["exp", "iss", "aud", "sub"]},
            leeway=30,
        )
        return claims

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(self.decode, token)
        except (jwt.PyJWTError, OSError):
            return None
        scope = claims.get("scope") or claims.get("scp") or []
        scopes = scope.split() if isinstance(scope, str) else [str(s) for s in scope]
        subject = str(claims["sub"])
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or subject),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            subject=subject,
            claims={"iss": claims["iss"], "principal": f"oauth:{claims['iss']}#{subject}", "label": subject},
        )
