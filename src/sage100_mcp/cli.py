"""Command line: ``sage100-mcp stdio | serve | init | doctor | probe | catalog | service | token`` (SPEC.md §16.2)."""

from __future__ import annotations

import argparse
import logging
import platform
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sage100_mcp import __version__, logs
from sage100_mcp.backends.drivers import check_driver, process_bitness, registered_drivers
from sage100_mcp.bridge import providex_check
from sage100_mcp.config import (
    ConfigError,
    LoadedConfig,
    ProvideXBackend,
    SqlServerBackend,
    load_config,
    load_config_or_defaults,
    parse_company_code,
)
from sage100_mcp.errors import SageError
from sage100_mcp.status import MILESTONE, PROVIDEX_REMEDY, SQLSERVER_REMEDY

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

logger = logging.getLogger("sage100_mcp")

TLS_WARN_DAYS = 30
CLOCK_WARN_S, CLOCK_FAIL_S = 30, 300  # JWT leeway; Kerberos refuses past five minutes


def _skew(seconds: float, against: str) -> tuple[str, str]:
    level = "OK" if abs(seconds) <= CLOCK_WARN_S else "WARN" if abs(seconds) <= CLOCK_FAIL_S else "FAIL"
    return level, f"clock: {against} is {seconds:+.1f} s from this machine"


def _configure_logging(level: str) -> None:
    # stdout carries the MCP protocol over stdio, so logs always go to stderr.
    logging.basicConfig(
        level=level.upper(), stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logs.guard()  # no password or token in any line (§9.4)


def _company(value: str) -> str:
    try:
        return parse_company_code(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _cmd_stdio(args: argparse.Namespace) -> int:
    from sage100_mcp.runtime import Runtime
    from sage100_mcp.server import build_server

    loaded = load_config_or_defaults(args.config, must_exist=args.config is not None)
    _configure_logging(loaded.config.server.log_level)
    if loaded.error:
        logger.error("Starting with no backends because the configuration was rejected:\n%s", loaded.error)
    runtime = Runtime(loaded)
    for error in runtime.errors[1 if loaded.error else 0 :]:
        logger.error("Serving nothing: %s", error)
    runtime.start()  # discovery and catalogs build in the background; the client is answered at once
    try:
        build_server(loaded, runtime).run("stdio")
    except KeyboardInterrupt:
        return 130
    finally:
        runtime.close()
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    """Streamable HTTP on http.bind (§10.2, §10.3, §16.2)."""
    from sage100_mcp.http.app import serving_problems
    from sage100_mcp.http.host import Host

    try:
        loaded = load_config(args.config, must_exist=args.config is not None)
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_FAILED
    problems = serving_problems(loaded.config.http)
    if problems:
        print("\n".join(f"serve: {problem}" for problem in problems), file=sys.stderr)
        return EXIT_FAILED
    _configure_logging(loaded.config.server.log_level)
    try:
        started = Host(loaded).run()
    except KeyboardInterrupt:
        return 130
    return EXIT_OK if started else EXIT_FAILED


def _cmd_init(args: argparse.Namespace) -> int:
    """Interactive setup (§16.2): the SOTAMAS90 template or a SQL Server, a tested login, config.yaml."""
    import os

    from sage100_mcp.config import default_config_path, default_home
    from sage100_mcp.setup import Answers, TerminalPrompter, Unattended, run_init

    password = None
    if args.password_env:
        password = os.environ.get(args.password_env)
        if not password:
            print(f"init: {args.password_env} is not set", file=sys.stderr)
            return EXIT_USAGE
    answers = Answers(
        kind=args.type,
        directory=args.directory,
        server=args.server,
        trust_server_certificate=args.trust_server_certificate,
        user=args.user,
        password=password,
        company=args.company,
    )
    prompter = Unattended() if args.yes else TerminalPrompter()
    path = args.config or default_config_path()
    return run_init(path, answers, prompter, home=default_home(), force=args.force)


def _cmd_token_create(args: argparse.Namespace) -> int:
    from sage100_mcp.config import default_home
    from sage100_mcp.http.app import token_store

    principal = args.principal.strip()
    if not principal or len(principal) > 64 or not principal.isprintable():
        print("token create: --principal must be a printable name of 1 to 64 characters", file=sys.stderr)
        return EXIT_USAGE
    store = token_store(default_home())
    record, secret = store.create(principal, args.companies)
    scope = f"limited to {', '.join(record.companies)}" if record.companies else "for every company"
    print(f"Token {record.id} for {principal}, {scope}:\n\n    {secret}\n")
    print(f"It is shown once: only its hash is kept, in {store.path}.")
    print("Clients send it as the header  Authorization: Bearer <token>")
    print(f"Revoke it with: sage100-mcp token revoke {record.id}")
    return EXIT_OK


def _cmd_token_list(args: argparse.Namespace) -> int:
    from sage100_mcp.config import default_home
    from sage100_mcp.http.app import token_store

    records = token_store(default_home()).records()
    if not records:
        print("No tokens. Create one with: sage100-mcp token create --principal NAME")
    for record in records:
        companies = ",".join(record.companies) if record.companies else "all companies"
        print(f"{record.id}  {record.principal}  {companies}  created {record.created}")
    return EXIT_OK


def _cmd_token_revoke(args: argparse.Namespace) -> int:
    from sage100_mcp.config import default_home
    from sage100_mcp.http.app import token_store

    if not token_store(default_home()).revoke(args.id):
        print(f"token revoke: no token {args.id}; sage100-mcp token list shows them", file=sys.stderr)
        return EXIT_FAILED
    print(f"Token {args.id} revoked; requests with it get 401 from now on.")
    return EXIT_OK


def _http_checks(loaded: LoadedConfig) -> list[tuple[str, str]]:
    """The HTTP settings `serve` would use: refusals, the TLS certificate's expiry, tokens or the issuer."""
    from datetime import UTC, datetime

    from cryptography import x509

    from sage100_mcp.config import default_home
    from sage100_mcp.http.app import serving_problems, token_store
    from sage100_mcp.http.auth import JwtVerifier, clock_skew

    http = loaded.config.http
    results = [("FAIL", problem) for problem in serving_problems(http)]
    if http.tls_cert is not None and http.tls_cert.is_file():
        try:
            certificate = x509.load_pem_x509_certificate(http.tls_cert.read_bytes())
        except ValueError as exc:
            results.append(("FAIL", f"TLS certificate {http.tls_cert}: not a PEM certificate ({exc})"))
        else:
            ends = certificate.not_valid_after_utc
            days = (ends - datetime.now(UTC)).days
            if days < 0:
                results.append(("FAIL", f"TLS certificate {http.tls_cert} expired on {ends:%Y-%m-%d}"))
            else:
                level = "WARN" if days < TLS_WARN_DAYS else "OK"
                results.append((level, f"TLS certificate {http.tls_cert} valid until {ends:%Y-%m-%d} ({days} days)"))
    if http.auth == "bearer":
        count = len(token_store(default_home()).records())
        results.append(("OK" if count else "WARN", f"bearer auth, {count} token(s); token create adds one"))
    elif http.oauth is not None:
        try:
            uri = JwtVerifier(http.oauth.issuer, http.oauth.audience, timeout=5).jwks_uri()
            results.append(("OK", f"OAuth issuer {http.oauth.issuer} publishes its keys at {uri}"))
            results.append(_skew(clock_skew(http.oauth.issuer), "the OAuth issuer"))
        except Exception as exc:  # any failure to reach or read the issuer is the finding
            results.append(("FAIL", f"OAuth issuer {http.oauth.issuer}: {exc}"))
    return results


def _audit_check(loaded: LoadedConfig) -> tuple[str, str]:
    """The audit directory takes a line (§9.4); a write that fails there is only logged by the server."""
    from sage100_mcp.config import default_home

    directory = loaded.config.server.audit_dir or default_home() / "audit"
    probe = directory / ".doctor-write-check"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return "FAIL", f"{directory} is not writable: {exc}"
    return "OK", f"{directory} is writable"


def _cmd_catalog_build(args: argparse.Namespace) -> int:
    """Rebuild catalogs from the database, ignoring the cache (§7.1, §16.2)."""
    from sage100_mcp.runtime import Runtime

    try:
        loaded = load_config(args.config, must_exist=args.config is not None)
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_FAILED
    _configure_logging("error")
    runtime = Runtime(loaded)
    if not runtime.serving:
        print("\n".join(runtime.errors), file=sys.stderr)
        return EXIT_FAILED
    runtime.discover()
    failures = 0
    for name, backend in runtime.backends.items():
        if backend.error is not None:
            failures += 1
            print(f"backend {name}: {backend.error.code} {backend.error.message}", file=sys.stderr)
    codes = [args.company] if args.company else list(runtime.companies)
    if args.company and args.company not in runtime.companies:
        available = ", ".join(runtime.companies) or "none"
        print(f"company {args.company}: not found; available: {available}", file=sys.stderr)
        return EXIT_FAILED
    if not codes:
        print("no companies found", file=sys.stderr)
        return EXIT_FAILED
    for code in codes:
        started = time.perf_counter()
        state = runtime.build(code, force=True)
        elapsed = time.perf_counter() - started
        if state.error is not None or state.catalog is None:
            failures += 1
            error = state.error
            print(f"{code}: failed: {error.code} {error.message}" if error else f"{code}: failed", file=sys.stderr)
            continue
        print(f"{code}: {len(state.catalog.tables)} tables in {elapsed:.1f} s, saved to {runtime.store.path(code)}")
        if state.catalog.drift:
            print(f"{code}: overlay entries the database contradicts: {', '.join(state.catalog.drift)}")
    runtime.close()
    return EXIT_FAILED if failures else EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    """The checks of §16.2 built so far: config, bitness, drivers, and SQL Server login and access."""
    failures = 0

    def report(level: str, check: str, message: str) -> None:
        nonlocal failures
        failures += level == "FAIL"
        print(f"[{level:^4}] {check}: {message}")

    try:
        loaded = load_config(args.config, must_exist=args.config is not None)
    except ConfigError as exc:
        report("FAIL", "config", str(exc))
        return EXIT_FAILED
    origin = "" if loaded.found else " (not found, using defaults)"
    report("OK" if loaded.found else "WARN", "config", f"{loaded.path}{origin}")
    if loaded.env_overrides:
        report("INFO", "config", f"environment overrides: {', '.join(loaded.env_overrides)}")
    python = f"Python {platform.python_version()} {process_bitness()}-bit on {sys.platform}"
    report("INFO", "process", f"sage100-mcp {__version__} ({MILESTONE}), {python}")

    backends = loaded.config.backends
    if not backends:
        report("WARN", "backends", "none configured, so no company data is reachable")
    drivers = registered_drivers()
    for name, backend in backends.items():
        if isinstance(backend, ProvideXBackend):
            if sys.platform != "win32":
                report("FAIL", f"backend {name}", "the ProvideX driver is a Windows DLL; run this backend on Windows")
                continue
            check = providex_check(backend, drivers, remedy=PROVIDEX_REMEDY)
        else:
            check = check_driver(backend.driver, drivers, remedy=SQLSERVER_REMEDY)
        level = {True: "OK", False: "FAIL", None: "SKIP"}[check.loadable]
        report(level, f"backend {name}", f"{check.name}: {check.message}")
        if check.helper:
            report("INFO", f"backend {name}", f"32-bit helper: {check.helper}")
        if isinstance(backend, SqlServerBackend) and check.loadable is not False:
            for level, message in _sqlserver_checks(name, backend, loaded):
                report(level, f"backend {name}", message)
        elif isinstance(backend, ProvideXBackend) and check.loadable is not False:
            for level, message in _providex_checks(name, backend, loaded):
                report(level, f"backend {name}", message)
    for level, message in _http_checks(loaded):
        report(level, "http", message)
    level, message = _audit_check(loaded)
    report(level, "audit", message)
    return EXIT_FAILED if failures else EXIT_OK


def _providex_checks(
    name: str, backend: ProvideXBackend, loaded: LoadedConfig, odbc: Any = None
) -> list[tuple[str, str]]:
    """Share, C/S port, login, a Silent=1 connect and a one-row read per company (§16.2)."""
    import getpass
    import socket

    from sage100_mcp.backends.providex import ProvideXExecutor
    from sage100_mcp.config import default_home
    from sage100_mcp.credentials import resolve_credential

    results: list[tuple[str, str]] = []
    account = getpass.getuser()
    folder = Path(backend.directory)
    if not folder.is_dir():
        return [("FAIL", f"{backend.directory} is not reachable as {account}; use a UNC path the account can read")]
    companies = sorted(entry.name[4:] for entry in folder.iterdir() if entry.is_dir() and len(entry.name) == 7
                       and entry.name.upper().startswith("MAS_"))  # fmt: skip
    results.append(
        ("OK", f"{backend.directory} reachable as {account}; data folders for {', '.join(companies) or 'no company'}")
    )
    if backend.transport == "cs" and backend.cs is not None:
        try:
            with socket.create_connection((backend.cs.host, backend.cs.port), timeout=5):
                results.append(("OK", f"Client/Server ODBC port {backend.cs.host}:{backend.cs.port} open"))
        except OSError as exc:
            return [*results, ("FAIL", f"Client/Server ODBC port {backend.cs.host}:{backend.cs.port}: {exc}")]
    executor: ProvideXExecutor | None = None
    try:
        credential = resolve_credential(backend.credential)
        results.append(("OK", f"Sage user {credential.user} found for {backend.credential}"))
        executor = ProvideXExecutor(
            name, backend, credential, loaded.config.limits, home=default_home(), companies=loaded.config.companies,
            odbc=odbc,
        )  # fmt: skip
        timeout = loaded.config.limits.query_timeout_s.providex
        for company in executor.discover():
            connect_ms, query_ms = executor.ping(company.database, executor.health_query(()), timeout)
            results.append(
                ("OK", f"company {company.code}: Silent=1 connect in {connect_ms} ms, one row in {query_ms} ms")
            )
        host = executor.driver_host()
        if host:
            results.append(("OK", f"32-bit helper running: {host}"))
        caps = executor.capabilities
        results.append(("OK" if caps.source == "probe" else "WARN",
                        f"driver {executor.driver_version}: capabilities {executor.capabilities_origin} "
                        f"({len(caps.summary()['passed'])} checks passed)"))  # fmt: skip
    except SageError as error:
        hint = f" Hint: {error.hint}" if error.hint else ""
        results.append(("FAIL", f"{error.code}: {error.message}{hint}"))
    finally:
        if executor is not None:
            executor.close()  # its pools, and the 32-bit helper where it has one
    return results


def _cmd_probe(args: argparse.Namespace) -> int:
    """Run the capability probe for each ProvideX backend and print the matrix (§5.3, §16.2)."""
    from sage100_mcp.capabilities import FEATURES, FUNCTIONS, KEYWORDS
    from sage100_mcp.capability_tools import refresh_capabilities
    from sage100_mcp.runtime import Runtime

    try:
        loaded = load_config(args.config, must_exist=args.config is not None)
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_FAILED
    _configure_logging("error")
    runtime = Runtime(loaded)
    if not runtime.serving:
        print("\n".join(runtime.errors), file=sys.stderr)
        return EXIT_FAILED
    runtime.discover()
    providex = [name for name, state in runtime.backends.items() if state.kind == "providex"]
    if not providex:
        print("no ProvideX backend is configured; Premium (SQL Server) needs no probe", file=sys.stderr)
        return EXIT_FAILED
    try:
        if args.company:
            targets = [runtime.resolve_company(args.company)]
        else:
            targets = [next(c for c in runtime.companies.values() if c.backend == name) for name in providex
                       if any(c.backend == name for c in runtime.companies.values())]  # fmt: skip
        failures = 0
        for name in providex:
            state = runtime.backends[name]
            if state.error is not None:
                failures += 1
                print(f"backend {name}: {state.error.code} {state.error.message}", file=sys.stderr)
        for company in targets:
            result = refresh_capabilities(runtime, company)
            print(f"{company.backend}: {result.message}")
            executor = runtime.executor(company)
            caps = executor.capabilities
            if caps is None:
                continue
            for feature in FEATURES:
                note = caps.notes.get(feature, "")
                print(f"  {'pass' if caps.supports(feature) else 'FAIL':4}  {feature:22} {note}")
            for function in FUNCTIONS:
                form = caps.function_form(function) or "FAIL"
                print(f"  {form:7} fn:{function:16} {caps.notes.get('fn:' + function, '')}")
            for keyword in KEYWORDS:
                print(f"  {'pass' if caps.accepts(keyword) else 'FAIL':4}  keyword {keyword}")
    except SageError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return EXIT_FAILED
    finally:
        runtime.close()
    return EXIT_FAILED if failures else EXIT_OK


def _sqlserver_checks(name: str, backend: SqlServerBackend, loaded: LoadedConfig) -> list[tuple[str, str]]:
    """Login, connection, clock, company databases and a read in each (§16.2), stopping at the first failure."""
    from datetime import UTC, datetime

    from sage100_mcp.backends.sqlserver import SqlServerExecutor
    from sage100_mcp.credentials import resolve_credential

    results: list[tuple[str, str]] = []
    try:
        credential = resolve_credential(backend.credential)
        results.append(("OK", f"login {credential.user} found for {backend.credential}"))
        executor = SqlServerExecutor(name, backend, credential, loaded.config.limits)
        timeout = loaded.config.limits.query_timeout_s.sqlserver
        connect_ms, _ = executor.ping(backend.system_database, "SELECT TOP 1 CompanyCode FROM dbo.SY_Company", timeout)
        versions = f"SQL Server {executor.server_version}, driver {executor.driver_version}"
        results.append(("OK", f"connected to {backend.server} in {connect_ms} ms ({versions})"))
        before = datetime.now(UTC)
        server_now = executor.scalar_rows(backend.system_database, "SELECT SYSUTCDATETIME()")[0][0]
        after = datetime.now(UTC)
        skew = (server_now.replace(tzinfo=UTC) - (before + (after - before) / 2)).total_seconds()
        results.append(_skew(skew, "SQL Server"))
        companies = executor.discover()
        if not companies:
            results.append(("WARN", f"the login can open no database matching {backend.database_pattern}"))
        for company in companies:
            probe = "SELECT TOP 1 TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME LIKE '%[_]Options'"
            executor.ping(company.database, probe, timeout)
            results.append(("OK", f"company {company.code} ({company.database}) readable"))
        executor.close()
    except SageError as error:
        hint = f" Hint: {error.hint}" if error.hint else ""
        results.append(("FAIL", f"{error.code}: {error.message}{hint}"))
    return results


def _cmd_service(args: argparse.Namespace) -> int:
    """The Windows service (§16.2, §16.5): install, remove, start, stop, status; run is for the service manager."""
    import os

    from sage100_mcp import service

    if args.action == "run":
        return service.run_service(args.config)
    if sys.platform != "win32":
        print("sage100-mcp service manages a Windows service; elsewhere run sage100-mcp serve under systemd or launchd",
              file=sys.stderr)  # fmt: skip
        return EXIT_USAGE
    manager = service.Win32Manager()
    try:
        if args.action == "install":
            password = os.environ.get(args.password_env) if args.password_env else None
            if args.password_env and not password:
                print(f"service install: {args.password_env} is not set", file=sys.stderr)
                return EXIT_USAGE
            return service.install(manager, args.account, password=password, config=args.config)
        if args.action == "remove":
            if manager.state() != "stopped":
                manager.stop()
            manager.delete()
        elif args.action == "start":
            manager.start()
        elif args.action == "stop":
            manager.stop()
        print(f"{service.NAME}: {manager.state()}")
    except Exception as exc:  # the service manager's own message says what went wrong
        if args.action == "remove" and "does not exist" in str(exc):
            print(f"{service.NAME}: not installed")
            return EXIT_OK
        print(f"service {args.action}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    config_help = "config file (default: %%ProgramData%%\\sage100-mcp\\config.yaml)"
    # --config is accepted before or after the command; SUPPRESS keeps an earlier value when it is absent.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=config_help)

    parser = argparse.ArgumentParser(prog="sage100-mcp", description="Read-only MCP server for US Sage 100.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help=config_help)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(name: str, summary: str, handler: Callable[[argparse.Namespace], int]) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=summary, parents=[common])
        sub.set_defaults(handler=handler)
        return sub

    command("stdio", "run as a stdio MCP server", _cmd_stdio)
    command("serve", "run Streamable HTTP on http.bind", _cmd_serve)
    init = command("init", "interactive setup from the SOTAMAS90 DSN or a SQL Server", _cmd_init)
    init.add_argument("--type", choices=["providex", "sqlserver"], help="providex (Standard, Advanced) or sqlserver")
    init.add_argument("--directory", help="ProvideX: the MAS90 folder (default: SOTAMAS90's)")
    init.add_argument("--server", help="Premium: the SQL Server instance")
    init.add_argument("--trust-server-certificate", action="store_true", help="Premium: a self-signed server")
    init.add_argument("--user", help="the Sage user, or the SQL Server login")
    init.add_argument("--password-env", help="read the password from this environment variable")
    init.add_argument("--company", help="the default company")
    init.add_argument("--yes", action="store_true", help="ask nothing: take defaults, or stop for a missing flag")
    init.add_argument("--force", action="store_true", help="replace an existing config file")
    command("doctor", "check config, bitness, drivers and access", _cmd_doctor)
    command("probe", "run the ProvideX capability probe", _cmd_probe).add_argument("--company", type=_company)
    services = commands.add_parser("service", help="manage the Windows service").add_subparsers(
        dest="action", required=True
    )
    install = services.add_parser("install", help="register the service under a named account", parents=[common])
    install.add_argument("--account", required=True, help="such as CORP\\svc-sage100mcp, or LocalSystem")
    install.add_argument("--password-env", help="read the account's password from this environment variable")
    for action, summary in (("remove", "stop and unregister the service"), ("start", "start the service"),
                            ("stop", "stop the service"), ("status", "show whether the service runs"),
                            ("run", "for the service manager only: run as the service")):  # fmt: skip
        services.add_parser(action, help=summary, parents=[common])
    for parser_ in services.choices.values():
        parser_.set_defaults(handler=_cmd_service)

    catalog = commands.add_parser("catalog", help="catalog maintenance").add_subparsers(dest="action", required=True)
    build = catalog.add_parser("build", help="build the table catalog in the foreground", parents=[common])
    build.add_argument("--company", type=_company)
    build.set_defaults(handler=_cmd_catalog_build)

    token = commands.add_parser("token", help="static bearer tokens").add_subparsers(dest="action", required=True)
    create = token.add_parser("create", help="issue a token (only its hash is stored)", parents=[common])
    create.add_argument("--principal", required=True, help="who the token is for, as audit lines name them")
    create.add_argument(
        "--companies",
        type=lambda v: [_company(c) for c in v.split(",") if c.strip()],
        help="limit the token to these companies, such as ABC,EMP",
    )
    create.set_defaults(handler=_cmd_token_create)
    token.add_parser("list", help="list tokens (never the secrets)", parents=[common]).set_defaults(
        handler=_cmd_token_list
    )
    revoke = token.add_parser("revoke", help="revoke a token; takes effect at once", parents=[common])
    revoke.add_argument("id", help="the token id that token create and token list show")
    revoke.set_defaults(handler=_cmd_token_revoke)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)
