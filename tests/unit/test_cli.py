from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from sage100_mcp import __version__
from sage100_mcp.backends.drivers import PROVIDEX_DRIVER, DriverRegistration
from sage100_mcp.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from tests.paths import EXAMPLE_CONFIG


@pytest.fixture(autouse=True)
def _in_a_scratch_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """doctor checks the audit directory by writing to it, and the example config's C:\\ProgramData path
    is a relative one off Windows: keep it out of the repository."""
    monkeypatch.chdir(tmp_path)


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"sage100-mcp {__version__}"


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_USAGE
    assert "stdio" in capsys.readouterr().err


def test_probe_needs_a_providex_backend(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["probe"]) == EXIT_FAILED
    assert "no ProvideX backend is configured" in capsys.readouterr().err


def test_company_arguments_are_validated(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["probe", "--company", "MAS_EMP"])
    assert caught.value.code == 2
    assert "use the code 'EMP'" in capsys.readouterr().err


def test_doctor_without_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[WARN] config:" in out
    assert "not found, using defaults" in out
    assert "[WARN] backends: none configured" in out


@pytest.mark.parametrize("before", [True, False])
def test_doctor_config_option_either_side(before: bool, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--config", str(EXAMPLE_CONFIG), "doctor"] if before else ["doctor", "--config", str(EXAMPLE_CONFIG)]
    main(argv)
    assert f"config: {EXAMPLE_CONFIG}" in capsys.readouterr().out


def test_doctor_rejected_config(write_config: Callable[..., Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--config", str(write_config("limits: {max_rows: 0}\n"))]) == EXIT_FAILED
    assert "[FAIL] config: invalid configuration" in capsys.readouterr().out


def test_doctor_missing_explicit_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--config", str(tmp_path / "missing.yaml")]) == EXIT_FAILED
    assert "config file not found" in capsys.readouterr().out


def test_doctor_providex_off_windows(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    assert main(["doctor", "--config", str(EXAMPLE_CONFIG)]) == EXIT_FAILED
    out = capsys.readouterr().out
    assert "[FAIL] backend sage_prod: the ProvideX driver is a Windows DLL" in out
    assert "[SKIP] backend sage_premium" in out


def test_doctor_on_windows_with_drivers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sage100_mcp.backends.drivers.process_bitness", lambda: 64)
    drivers = [
        DriverRegistration(PROVIDEX_DRIVER, 64, None, "03.00"),
        DriverRegistration("ODBC Driver 18 for SQL Server", 64, None, "03.80"),
    ]
    monkeypatch.setattr("sage100_mcp.cli.registered_drivers", lambda: drivers)
    monkeypatch.setattr("sage100_mcp.cli._sqlserver_checks", lambda *args: [("OK", "connected")])
    monkeypatch.setattr("sage100_mcp.cli._providex_checks", lambda *args: [("OK", "company ABC: Silent=1 connect")])
    assert main(["doctor", "--config", str(EXAMPLE_CONFIG)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[ OK ] backend sage_prod: MAS 90 4.0 ODBC Driver: registered for 64-bit" in out
    assert "[ OK ] backend sage_premium: connected" in out
    assert "[ OK ] backend sage_prod: company ABC: Silent=1 connect" in out


def test_doctor_reports_a_missing_login(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    assert main(["doctor", "--config", str(EXAMPLE_CONFIG)]) == EXIT_FAILED
    out = capsys.readouterr().out
    assert "[FAIL] backend sage_premium: AUTH_FAILED: No login is stored for sage100-mcp/sage_premium." in out
    assert 'keyring set "sage100-mcp/sage_premium"' in out


def test_catalog_build_without_backends(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("companies: {}\n", encoding="utf-8")
    assert main(["catalog", "build", "--config", str(config)]) == EXIT_FAILED
    assert "no companies found" in capsys.readouterr().err


def test_doctor_on_windows_wrong_bitness(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sage100_mcp.backends.drivers.process_bitness", lambda: 64)
    monkeypatch.setattr(
        "sage100_mcp.cli.registered_drivers", lambda: [DriverRegistration(PROVIDEX_DRIVER, 32, None, None)]
    )
    assert main(["doctor", "--config", str(EXAMPLE_CONFIG)]) == EXIT_FAILED
    assert "IM014" in capsys.readouterr().out


def test_doctor_on_windows_with_the_32_bit_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A 32-bit-only driver is fine once the 32-bit helper is there to load it (decisions D67)."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sage100_mcp.bridge.process_bitness", lambda: 64)
    monkeypatch.setattr(
        "sage100_mcp.cli.registered_drivers", lambda: [DriverRegistration(PROVIDEX_DRIVER, 32, None, None)]
    )
    config = tmp_path / "config.yaml"
    config.write_text(  # a directory that isn't there: doctor's later checks fail at once, not on a UNC lookup
        f"backends:\n  sage:\n    type: providex\n    directory: '{tmp_path / 'MAS90'}'\n"
        "    credential: sage100-mcp/sage\n    helper: C:\\Python312-32\\python.exe\n",
        encoding="utf-8",
    )
    main(["doctor", "--config", str(config)])
    out = capsys.readouterr().out
    assert (
        "[ OK ] backend sage: MAS 90 4.0 ODBC Driver: registered for 32-bit; "
        "this 64-bit server loads the 32-bit build through its helper"
    ) in out
    assert "[INFO] backend sage: 32-bit helper: C:\\Python312-32\\python.exe -E " in out


# --- M4: tokens, serve, and the doctor's HTTP checks ---------------------------------------------------------


def test_tokens_are_created_listed_and_revoked(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["token", "list"]) == EXIT_OK
    assert "No tokens" in capsys.readouterr().out
    assert main(["token", "create", "--principal", "ops", "--companies", "abc,EMP"]) == EXIT_OK
    created = capsys.readouterr().out
    secret = re.search(r"s1mcp_\S+", created)
    token_id = re.search(r"Token (\w+) for ops, limited to ABC, EMP", created)
    assert secret
    assert token_id
    stored = (isolated_home / "tokens.json").read_text(encoding="utf-8")
    assert secret.group(0) not in stored
    assert main(["token", "list"]) == EXIT_OK
    listed = capsys.readouterr().out
    assert f"{token_id.group(1)}  ops  ABC,EMP  created " in listed
    assert secret.group(0) not in listed
    assert main(["token", "revoke", token_id.group(1)]) == EXIT_OK
    assert main(["token", "revoke", token_id.group(1)]) == EXIT_FAILED
    assert "no token" in capsys.readouterr().err


@pytest.mark.parametrize("principal", ["", " ", "x" * 65, "tab\there"])
def test_a_token_needs_a_principal_name(principal: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["token", "create", "--principal", principal]) == EXIT_USAGE
    assert "--principal must be" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("http", "message"),
    [
        ("bind: 10.0.0.5:8765", "needs TLS"),
        ("bind: 0.0.0.0:8765\n  tls_cert: c.pem\n  tls_key: k.pem", "listens on every address"),
    ],
)
def test_serve_refuses_exposed_settings(
    http: str, message: str, write_config: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["serve", "--config", str(write_config(f"http:\n  {http}\n"))]) == EXIT_FAILED
    assert message in capsys.readouterr().err


def test_serve_refuses_a_rejected_config(write_config: Callable[..., Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", "--config", str(write_config("http: {bind: nowhere}\n"))]) == EXIT_FAILED
    assert "config: invalid configuration" in capsys.readouterr().err


def certificate(path: Path, days: int) -> Path:
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sage-mcp.example.com")])
    now = datetime.now(UTC)
    built = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now - timedelta(days=400)).not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    path.write_bytes(built.public_bytes(serialization.Encoding.PEM))
    path.with_suffix(".key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return path


@pytest.mark.parametrize(
    ("days", "expected"),
    [(200, "[ OK ] http: TLS certificate"), (10, "[WARN] http: TLS certificate"), (-3, "[FAIL] http: TLS certificate")],
)
def test_doctor_checks_the_tls_certificate(
    days: int, expected: str, tmp_path: Path, write_config: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    cert = certificate(tmp_path / "server.pem", days)
    config = write_config(f"http:\n  tls_cert: {cert}\n  tls_key: {cert.with_suffix('.key')}\n")
    assert main(["doctor", "--config", str(config)]) == (EXIT_FAILED if days < 0 else EXIT_OK)
    out = capsys.readouterr().out
    assert expected in out
    assert "[WARN] http: bearer auth, 0 token(s)" in out


def test_doctor_rejects_a_certificate_that_is_not_pem(
    tmp_path: Path, write_config: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "c.pem").write_text("not a certificate", encoding="utf-8")
    (tmp_path / "k.pem").write_text("not a key", encoding="utf-8")
    config = write_config(f"http:\n  tls_cert: {tmp_path / 'c.pem'}\n  tls_key: {tmp_path / 'k.pem'}\n")
    assert main(["doctor", "--config", str(config)]) == EXIT_FAILED
    assert "not a PEM certificate" in capsys.readouterr().out


@pytest.mark.parametrize("reachable", [True, False])
def test_doctor_reaches_the_oauth_issuer(
    reachable: bool, monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    def jwks_uri(self: object) -> str:
        if not reachable:
            raise OSError("connection refused")
        return "https://login.example.com/keys"

    monkeypatch.setattr("sage100_mcp.http.auth.JwtVerifier.jwks_uri", jwks_uri)
    monkeypatch.setattr("sage100_mcp.http.auth.clock_skew", lambda issuer: 2.0)
    config = write_config("http:\n  auth: oauth\n  oauth: {issuer: 'https://login.example.com', audience: x}\n")
    assert main(["doctor", "--config", str(config)]) == (EXIT_OK if reachable else EXIT_FAILED)
    out = capsys.readouterr().out
    if reachable:
        assert "[ OK ] http: OAuth issuer https://login.example.com publishes its keys at" in out
        assert "[ OK ] http: clock: the OAuth issuer is +2.0 s from this machine" in out
    else:
        assert "[FAIL] http: OAuth issuer https://login.example.com: connection refused" in out


@pytest.mark.parametrize(("skew", "level"), [(-12.0, "OK"), (45.0, "WARN"), (-400.0, "FAIL")])
def test_doctor_rates_the_issuers_clock(
    skew: float, level: str, monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setattr("sage100_mcp.http.auth.JwtVerifier.jwks_uri", lambda self: "https://login.example.com/keys")
    monkeypatch.setattr("sage100_mcp.http.auth.clock_skew", lambda issuer: skew)
    config = write_config("http:\n  auth: oauth\n  oauth: {issuer: 'https://login.example.com', audience: x}\n")
    main(["doctor", "--config", str(config)])
    assert f"[{level:^4}] http: clock: the OAuth issuer is {skew:+.1f} s from this machine" in capsys.readouterr().out


def test_doctor_checks_the_audit_directory(
    tmp_path: Path, write_config: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor"]) == EXIT_OK
    assert "[ OK ] audit: " in capsys.readouterr().out
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    config = write_config(f"server:\n  audit_dir: {blocker / 'audit'}\n")
    assert main(["doctor", "--config", str(config)]) == EXIT_FAILED
    assert "is not writable" in capsys.readouterr().out


def test_clock_skew_reads_the_issuers_date_header() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from sage100_mcp.http.auth import clock_skew

    class Missing(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_error(404)  # metadata is often absent at one of the two places; Date still comes

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Missing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        skew = clock_skew(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
    assert abs(skew) < 2
