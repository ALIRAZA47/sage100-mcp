"""``sage100-mcp serve`` end to end over TLS: the M4 gate's transport, short of a Mac (SPEC.md §18).

The real commands run as a user would run them: ``token create`` issues a token, ``serve`` listens with
a self-signed certificate, and the SDK's client connects over HTTPS with both protocol revisions.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

pytestmark = [pytest.mark.contract, pytest.mark.anyio]

MODES = {"legacy": "2025-11-25", "auto": "2026-07-28"}
USAGE = x509.KeyUsage(
    digital_signature=True, key_encipherment=True, key_cert_sign=True, content_commitment=False,
    data_encipherment=False, key_agreement=False, crl_sign=False, encipher_only=False, decipher_only=False,
)  # fmt: skip


def self_signed(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    public = key.public_key()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=10))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(USAGE, critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert, private = directory / "server.crt", directory / "server.key"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert, private


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sage100_mcp", *args], capture_output=True, text=True, timeout=60, env=os.environ.copy()
    )


@dataclass
class Served:
    url: str
    cert: Path
    token: str
    home: Path
    log: Path  # the server's stdout and stderr


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    directory = tmp_path_factory.mktemp("tls")
    home = directory / "home"
    saved = os.environ.get("SAGE100_MCP_HOME")
    os.environ["SAGE100_MCP_HOME"] = str(home)  # the module fixture runs outside the per-test isolated home
    cert, key = self_signed(directory)
    port = free_port()
    config = directory / "config.yaml"
    config.write_text(
        f"http:\n  bind: 127.0.0.1:{port}\n  tls_cert: {json.dumps(str(cert))}\n  tls_key: {json.dumps(str(key))}\n",
        encoding="utf-8",
    )
    created = cli("token", "create", "--principal", "gate-check")
    assert created.returncode == 0, created.stderr
    token = re.search(r"s1mcp_[A-Za-z0-9_-]+", created.stdout)
    assert token, created.stdout
    log = (directory / "serve.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "sage100_mcp", "serve", "--config", str(config)],
        stdout=log, stderr=subprocess.STDOUT, env=os.environ | {"PYTHONUNBUFFERED": "1"},
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, (directory / "serve.log").read_text(encoding="utf-8")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "serve did not start listening within 30 s"
                time.sleep(0.2)
        yield Served(f"https://127.0.0.1:{port}/mcp", cert, token.group(0), home, directory / "serve.log")
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()
        if saved is None:
            os.environ.pop("SAGE100_MCP_HOME", None)
        else:
            os.environ["SAGE100_MCP_HOME"] = saved


def https(served: Served, token: str | None) -> httpx2.AsyncClient:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(verify=ssl.create_default_context(cafile=str(served.cert)), headers=headers)


@pytest.mark.parametrize("mode", MODES)
async def test_a_client_works_over_tls(served: Served, mode: str) -> None:
    async with Client(streamable_http_client(served.url, http_client=https(served, served.token)), mode=mode) as client:
        assert client.protocol_version == MODES[mode]
        assert len((await client.list_tools()).tools) == 22
        status = await client.call_tool("sage_status", {})
    assert status.is_error is False
    assert status.structured_content is not None
    assert status.structured_content["server"]["version"]


async def test_tls_refuses_what_it_should(served: Served) -> None:
    async with https(served, None) as anonymous:
        response = await anonymous.post(served.url, json={})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer ")
    async with httpx2.AsyncClient() as untrusting:  # the self-signed certificate is not trusted here
        with pytest.raises(httpx2.ConnectError):
            await untrusting.post(served.url, json={})
    async with httpx2.AsyncClient() as plain:
        with pytest.raises(httpx2.HTTPError):
            await plain.post(served.url.replace("https://", "http://"), json={})


async def test_calls_over_tls_are_audited(served: Served) -> None:
    async with Client(streamable_http_client(served.url, http_client=https(served, served.token))) as client:
        await client.call_tool("list_companies", {})
    files = list((served.home / "audit").glob("*.jsonl"))
    lines = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    seen = {(line["name"], line["transport"], line["tool"]) for line in lines}
    assert ("gate-check", "http", "list_companies") in seen
    assert all(served.token not in path.read_text(encoding="utf-8") for path in files)
    log = served.log.read_text(encoding="utf-8")
    assert '"POST /mcp HTTP/1.1" 200' in log, "the access log is written"
    assert served.token not in log
    assert "s1mcp_" not in log


async def test_remote_check_over_tls(served: Served) -> None:
    """The Mac pre-flight against the real server: the transport passes; this server sees no company."""
    import runpy

    from tests.paths import REPO

    script = runpy.run_path(str(REPO / "scripts" / "remote_check.py"), run_name="remote_check")
    factory = script["https_factory"](str(served.cert))
    checks = await script["run_checks"](served.url, served.token, factory=factory)
    assert [(c.name, c.passed) for c in checks] == [
        ("no token", True),
        ("foreign Origin", True),
        ("protocol 2025-11-25", True),
        ("protocol 2026-07-28", True),
        ("companies", False),
    ], [c.line() for c in checks]
    assert "none visible" in checks[-1].detail
