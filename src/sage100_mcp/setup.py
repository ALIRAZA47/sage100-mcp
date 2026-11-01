"""``sage100-mcp init`` (SPEC.md §16.2): from Sage's own SOTAMAS90 DSN, or a SQL Server, to a working config.

It reads SOTAMAS90's Directory, Prefix and ViewDLL from HKCU\\Software\\ODBC\\ODBC.INI\\SOTAMAS90 as a
template (§4.2), asks for the Sage login (or, on Premium, the SQL Server and its login), connects with it
to list the companies, stores the login in the keyring under the backend's credential name (§9.4), and
writes config.yaml. The login is stored only once it has connected. Every question has a flag, so a
script can run init without prompts; a password comes from a prompt or an environment variable, never
from the command line.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol

from sage100_mcp.backends.base import DiscoveredCompany, Executor
from sage100_mcp.config import (
    Config,
    ConfigError,
    ProvideXBackend,
    SqlServerBackend,
    load_config,
    parse_company_code,
)
from sage100_mcp.credentials import Credential, env_names
from sage100_mcp.errors import SageError

SOTAMAS90_KEY = r"Software\ODBC\ODBC.INI\SOTAMAS90"
NAMES = {"providex": "sage", "sqlserver": "premium"}  # the backend's name in config.yaml
_MAPPED_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

Kind = Literal["providex", "sqlserver"]
Connect = Callable[[str, ProvideXBackend | SqlServerBackend, Credential, Path], Executor]


class Prompter(Protocol):
    def ask(self, question: str, default: str | None = None) -> str: ...

    def secret(self, question: str) -> str: ...


@dataclass
class Answers:
    """What init needs. Flags fill in what they can; the prompter asks for the rest."""

    kind: Kind | None = None
    directory: str | None = None
    server: str | None = None
    trust_server_certificate: bool = False
    user: str | None = None
    password: str | None = None
    company: str | None = None


def sotamas90(read: Callable[[str], str | None] | None = None) -> dict[str, str]:
    """Directory, Prefix and ViewDLL from the current user's SOTAMAS90 DSN, as far as it has them."""
    if read is None:
        read = _registry_reader()
    found = {}
    for name in ("Directory", "Prefix", "ViewDLL"):
        value = read(name)
        if value:
            found[name] = value.strip()
    return found


def _registry_reader() -> Callable[[str], str | None]:
    if sys.platform != "win32":
        return lambda name: None
    import winreg

    def read(name: str) -> str | None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SOTAMAS90_KEY) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return None

    return read


def default_connect(
    name: str, settings: ProvideXBackend | SqlServerBackend, credential: Credential, home: Path
) -> Executor:
    limits = Config().limits
    if isinstance(settings, ProvideXBackend):
        from sage100_mcp.backends.providex import ProvideXExecutor

        return ProvideXExecutor(name, settings, credential, limits, home=home, companies=Config().companies)
    from sage100_mcp.backends.sqlserver import SqlServerExecutor

    return SqlServerExecutor(name, settings, credential, limits)


def store_in_keyring(reference: str, user: str, password: str) -> None:
    import keyring

    keyring.set_password(reference, user, password)


def backend_settings(kind: Kind, answers: Answers, prompter: Prompter, template: dict[str, str],
                     say: Callable[[str], None]) -> dict[str, Any]:  # fmt: skip
    """The backends entry, from the flags, the template and the answers to the prompts."""
    credential = f"sage100-mcp/{NAMES[kind]}"
    if kind == "sqlserver":
        question = "SQL Server instance that holds the MAS_ databases, such as SQLSRV\\SAGE"
        server = answers.server or prompter.ask(question)
        entry: dict[str, Any] = {"type": "sqlserver", "server": server, "credential": credential}
        if answers.trust_server_certificate:
            entry["trust_server_certificate"] = True
        return entry
    if template:
        say(f"Found Sage's SOTAMAS90 DSN: Directory {template.get('Directory', '(none)')}.")
    directory = answers.directory or prompter.ask(
        "The MAS90 folder (the one holding PROVIDEX.DDF); a UNC path if the server runs as a service",
        template.get("Directory"),
    )
    if _MAPPED_DRIVE.match(directory):
        say(f"Note: {directory} is a drive letter. A Windows service has no mapped drives, so use the UNC path "
            "(\\\\server\\share\\...\\MAS90) if sage100-mcp will run as a service.")  # fmt: skip
    entry = {"type": "providex", "directory": directory, "credential": credential}
    if template.get("Directory", "").rstrip("\\/").lower() == directory.rstrip("\\/").lower():
        # The DSN's own search prefix and ViewDLL folder fit this directory: keep them (§16.1).
        copied = {"prefix": "Prefix", "viewdll": "ViewDLL"}
        entry |= {key: template[name] for key, name in copied.items() if name in template}
    return entry


def config_text(kind: Kind, backend: dict[str, Any], default: str | None) -> str:
    lines = [
        f"# Written by sage100-mcp init on {date.today():%Y-%m-%d}. docs/config.example.yaml shows every setting.",
        "backends:",
        f"  {NAMES[kind]}:",
    ]
    for key, value in backend.items():
        lines.append(f"    {key}: {_yaml_scalar(value)}")
    if default:
        lines += ["companies:", f"  default: {default}"]
    return "\n".join(lines) + "\n"


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def run_init(
    path: Path,
    answers: Answers,
    prompter: Prompter,
    *,
    home: Path,
    force: bool = False,
    connect: Connect | None = None,
    store: Callable[[str, str, str], None] | None = None,
    registry: Callable[[str], str | None] | None = None,
    say: Callable[[str], None] = print,
) -> int:
    """0 when the config was written; 1 when a check failed; 2 when the config already exists."""
    if path.exists() and not force:
        say(f"{path} already exists; edit it, or run init with --force to replace it.")
        return 2
    try:
        return _run(path, answers, prompter, home=home, connect=connect or default_connect,
                    store=store or store_in_keyring, registry=registry, say=say)  # fmt: skip
    except ValueError as exc:  # Unattended: a value init could not get
        say(str(exc))
        return 1


def _run(
    path: Path,
    answers: Answers,
    prompter: Prompter,
    *,
    home: Path,
    connect: Connect,
    store: Callable[[str, str, str], None],
    registry: Callable[[str], str | None] | None,
    say: Callable[[str], None],
) -> int:
    template = sotamas90(registry)
    kind: str = answers.kind or prompter.ask(
        "Sage 100 edition: providex (Standard or Advanced) or sqlserver (Premium)",
        "providex" if template or sys.platform == "win32" else "sqlserver",
    )
    if kind not in NAMES:
        say(f"Unknown edition {kind!r}: answer providex or sqlserver.")
        return 1
    backend = backend_settings(kind, answers, prompter, template, say)  # type: ignore[arg-type]
    user = answers.user or prompter.ask("Sage user" if kind == "providex" else "SQL Server login")
    password = answers.password or prompter.secret(f"Password for {user}")
    settings = Config.model_validate({"backends": {NAMES[kind]: backend}}).backends[NAMES[kind]]
    executor = None
    try:
        executor = connect(NAMES[kind], settings, Credential(user, password), home)
        companies = executor.discover()
    except SageError as error:
        say(f"Could not connect: {error.code} {error.message}" + (f" {error.hint}" if error.hint else ""))
        return 1
    finally:
        if executor is not None:
            executor.close()
    if not companies:
        say("Connected, but the login can open no company. Check its company access in Sage, then run init again.")
        return 1
    say("Companies: " + ", ".join(_company_label(company) for company in companies))
    try:
        default = _default_company(answers, prompter, companies)
    except ValueError as exc:
        say(f"Default company: {exc}")
        return 1
    try:
        store(str(backend["credential"]), user, password)
        say(f"Stored the login for {user} in the keyring as {backend['credential']}.")
    except Exception as exc:  # no keyring on this host: a container, or Linux without a desktop session
        names = " and ".join(env_names(str(backend["credential"])))
        say(f"This host has no keyring to store the login in ({type(exc).__name__}). "
            f"Set {names} in the server's environment instead (decisions D69).")  # fmt: skip
    text = config_text(kind, backend, default)  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    temporary.write_text(text, encoding="utf-8")
    try:
        load_config(temporary, env={}, must_exist=True)
    except ConfigError as exc:  # never leave a config the server would reject
        temporary.unlink()
        say(f"The config init built was rejected: {exc}")
        return 1
    temporary.replace(path)
    say(f"Wrote {path}. Next: sage100-mcp doctor, then add the server to your MCP client (README).")
    return 0


def _company_label(company: DiscoveredCompany) -> str:
    return f"{company.code} ({company.name})" if company.name else company.code


def _default_company(answers: Answers, prompter: Prompter, companies: list[DiscoveredCompany]) -> str | None:
    codes = [company.code for company in companies]
    if answers.company:
        chosen = answers.company
    elif len(codes) == 1:
        return codes[0]
    else:
        chosen = prompter.ask(f"Default company ({', '.join(codes)}; empty for none)", "")
    if not chosen.strip():
        return None
    code = parse_company_code(chosen)
    if code not in codes:
        raise ValueError(f"{code} is not among the companies this login can open ({', '.join(codes)})")
    return code


class Unattended:
    """init --yes: a question without a flag takes its default, or stops init with what to pass."""

    def ask(self, question: str, default: str | None = None) -> str:
        if default is None:
            raise ValueError(f"no answer for: {question}; give it as a flag")
        return default

    def secret(self, question: str) -> str:
        raise ValueError(f"no answer for: {question}; name an environment variable with --password-env")


class TerminalPrompter:
    """Questions on the terminal; a password is read without echo."""

    def ask(self, question: str, default: str | None = None) -> str:
        shown = f" [{default}]" if default else ""
        answer = input(f"{question}{shown}: ").strip()
        return answer or (default or "")

    def secret(self, question: str) -> str:
        import getpass

        return getpass.getpass(f"{question}: ")
