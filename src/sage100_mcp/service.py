"""The Windows service (SPEC.md §16.2, §16.5): ``sage100-mcp serve``, run by the service control manager.

    sage100-mcp service install --account CORP\\svc-sage100mcp --password-env SVC_PASSWORD
    sage100-mcp service start | stop | remove

install registers the service to run ``<this program> service run``, starting automatically once the
network is up, and restarting after a failure. It grants the account "Log on as a service" and modify
access to the server's home (%ProgramData%\\sage100-mcp: config, tokens, caches, audit files), which the
MSI leaves to Administrators and SYSTEM. The password comes from an environment variable, never from the
command line. A named domain account is what §16.5 asks for: LocalSystem can't read a Sage share over
UNC, so it suits only a Premium-only host.

The service logs to <home>\\logs\\sage100-mcp.log, with passwords and tokens redacted.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sage100_mcp import logs
from sage100_mcp.config import HOME_ENV, ConfigError, ProvideXBackend, default_home, load_config

NAME = "sage100-mcp"
DISPLAY = "Sage 100 MCP server (read-only)"
DESCRIPTION = "Serves read-only Sage 100 data to MCP clients over Streamable HTTP (sage100-mcp serve)."
BUILT_IN = {"localsystem": None, "nt authority\\localservice": "NT AUTHORITY\\LocalService",
            "nt authority\\networkservice": "NT AUTHORITY\\NetworkService"}  # fmt: skip
LOG_FILE = "sage100-mcp.log"

logger = logging.getLogger(__name__)


class Manager(Protocol):
    """The service control manager and the account changes install makes (Win32Manager on Windows)."""

    def create(self, command: str, account: str | None, password: str | None, environment: dict[str, str]) -> None: ...

    def delete(self) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def state(self) -> str: ...

    def grant_logon_right(self, account: str) -> None: ...

    def grant_home_access(self, account: str, home: Path) -> None: ...


@dataclass(frozen=True)
class Account:
    """The account the service runs as: None means LocalSystem."""

    name: str | None
    needs_password: bool

    @property
    def label(self) -> str:
        return self.name or "LocalSystem"


def parse_account(text: str) -> Account:
    key = text.strip().lower()
    if key in BUILT_IN:
        return Account(BUILT_IN[key], needs_password=False)
    name = text.strip()
    if "\\" not in name and "@" not in name:
        name = f".\\{name}"  # a local account
    # A group managed service account (CORP\svc$) has no password to give.
    return Account(name, needs_password=not name.endswith("$"))


def command_line(config: Path | None, *, executable: str | None = None, frozen: bool | None = None) -> str:
    """How the service control manager starts the service: this program, with ``service run``."""
    executable = executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    parts = [executable, "service", "run"] if frozen else [executable, "-m", "sage100_mcp", "service", "run"]
    if config is not None:
        parts += ["--config", str(config.resolve())]
    return subprocess.list2cmdline(parts)


def install(
    manager: Manager,
    account_text: str,
    *,
    password: str | None,
    config: Path | None,
    say: Callable[[str], None] = print,
) -> int:
    """0 on success; 2 when a password is needed and missing."""
    account = parse_account(account_text)
    if account.needs_password and not password:
        say(f"service install: {account.label} needs its password; pass --password-env with a variable holding it")
        return 2
    if account.name is None:
        say("Note: as LocalSystem the service can't read a Sage share over UNC. Use a domain account for ProvideX.")
        if _uses_providex(config):
            say("This config has a ProvideX backend, so give the service a domain account with read access to it.")
    home = default_home()
    environment = {HOME_ENV: os.environ[HOME_ENV]} if os.environ.get(HOME_ENV) else {}
    manager.create(command_line(config), account.name, password if account.needs_password else None, environment)
    say(f"Installed service {NAME}, running as {account.label}, starting automatically after the network is up.")
    if account.name is not None and account.name not in BUILT_IN.values():
        manager.grant_logon_right(account.name)
        say(f"Granted {account.label} the right to log on as a service.")
        home.mkdir(parents=True, exist_ok=True)
        manager.grant_home_access(account.name, home)
        say(f"Gave {account.label} modify access to {home}.")
    say("Start it with: sage100-mcp service start")
    return 0


def _uses_providex(config: Path | None) -> bool:
    try:
        loaded = load_config(config, must_exist=config is not None)
    except ConfigError:
        return False
    return any(isinstance(backend, ProvideXBackend) for backend in loaded.config.backends.values())


def file_logging(home: Path, level: str) -> logging.Handler:
    """The service has no console: a rotating log file in the home, with secrets redacted."""
    directory = home / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        directory / LOG_FILE, maxBytes=10 * 2**20, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(logs.RedactSecrets())
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(handler)
    return handler


def run_service(config: Path | None) -> int:  # pragma: no cover - runs only under the Windows service manager
    """``service run``: hand this process to the service control manager, which then calls SvcDoRun."""
    if sys.platform != "win32":
        print("service run: the Windows service runs only on Windows; use sage100-mcp serve", file=sys.stderr)
        return 2
    import pywintypes
    import servicemanager
    import win32service
    import win32serviceutil

    from sage100_mcp.http.app import serving_problems
    from sage100_mcp.http.host import Host

    holder: dict[str, Host] = {}

    class Service(win32serviceutil.ServiceFramework):
        _svc_name_ = NAME
        _svc_display_name_ = DISPLAY
        _svc_description_ = DESCRIPTION

        def SvcStop(self) -> None:  # pywin32's name
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            host = holder.get("host")
            if host is not None:
                host.stop()

        def SvcDoRun(self) -> None:  # pywin32's name
            try:
                loaded = load_config(config, must_exist=config is not None)
                file_logging(default_home(), loaded.config.server.log_level)
                problems = serving_problems(loaded.config.http)
                if problems:
                    servicemanager.LogErrorMsg("sage100-mcp can't serve: " + " ".join(problems))
                    return
                holder["host"] = Host(loaded)
                servicemanager.LogInfoMsg(f"sage100-mcp serving at {loaded.config.http.endpoint}")
                holder["host"].run()
            except Exception:
                servicemanager.LogErrorMsg("sage100-mcp stopped on an error:\n" + logs.failure_text())

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(Service)
    try:
        servicemanager.StartServiceCtrlDispatcher()  # type: ignore[no-untyped-call, unused-ignore]  # untyped stub
    except pywintypes.error:
        print("service run is for the Windows service manager; run sage100-mcp serve in a console", file=sys.stderr)
        return 2
    return 0


class Win32Manager:  # pragma: no cover - Windows only; scripts/service_smoke.py exercises it in CI
    """The service control manager through pywin32, and the account changes install makes."""

    def create(self, command: str, account: str | None, password: str | None, environment: dict[str, str]) -> None:
        import win32service

        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        try:
            service = win32service.CreateService(
                scm, NAME, DISPLAY, win32service.SERVICE_ALL_ACCESS, win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START, win32service.SERVICE_ERROR_NORMAL, command, None, False, None,
                account, password,
            )  # fmt: skip
            try:
                win32service.ChangeServiceConfig2(service, win32service.SERVICE_CONFIG_DESCRIPTION, DESCRIPTION)
                win32service.ChangeServiceConfig2(service, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO, True)
                restart = (win32service.SC_ACTION_RESTART, 60_000)
                win32service.ChangeServiceConfig2(
                    service,
                    win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
                    {"ResetPeriod": 86_400, "RebootMsg": None, "Command": None,
                     "Actions": [restart, restart, (win32service.SC_ACTION_NONE, 0)]},
                )  # fmt: skip
            finally:
                win32service.CloseServiceHandle(service)
        finally:
            win32service.CloseServiceHandle(scm)
        if environment and sys.platform == "win32":  # the service's own variables, such as SAGE100_MCP_HOME
            import winreg

            path = rf"SYSTEM\CurrentControlSet\Services\{NAME}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE) as key:
                values = [f"{name}={value}" for name, value in environment.items()]
                winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, values)

    def delete(self) -> None:
        import win32service

        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        try:
            service = win32service.OpenService(scm, NAME, win32service.SERVICE_ALL_ACCESS)
            try:
                win32service.DeleteService(service)
            finally:
                win32service.CloseServiceHandle(service)
        finally:
            win32service.CloseServiceHandle(scm)

    def start(self) -> None:
        import win32service
        import win32serviceutil

        win32serviceutil.StartService(NAME)
        win32serviceutil.WaitForServiceStatus(NAME, win32service.SERVICE_RUNNING, 60)

    def stop(self) -> None:
        import win32service
        import win32serviceutil

        win32serviceutil.StopService(NAME)
        win32serviceutil.WaitForServiceStatus(NAME, win32service.SERVICE_STOPPED, 60)

    def state(self) -> str:
        import win32service
        import win32serviceutil

        code = win32serviceutil.QueryServiceStatus(NAME)[1]
        names = {
            win32service.SERVICE_RUNNING: "running",
            win32service.SERVICE_STOPPED: "stopped",
            win32service.SERVICE_START_PENDING: "starting",
            win32service.SERVICE_STOP_PENDING: "stopping",
        }
        return names.get(code, f"state {code}")

    def grant_logon_right(self, account: str) -> None:
        import win32security

        sid = win32security.LookupAccountName(None, account)[0]
        policy = win32security.LsaOpenPolicy("", win32security.POLICY_ALL_ACCESS)  # "": this machine
        try:
            win32security.LsaAddAccountRights(policy, sid, ("SeServiceLogonRight",))
        finally:
            win32security.LsaClose(policy)  # type: ignore[arg-type]  # the stubs say int; pywin32 takes the handle

    def grant_home_access(self, account: str, home: Path) -> None:
        subprocess.run(["icacls", str(home), "/grant", f"{account}:(OI)(CI)M", "/T", "/Q"], check=True)
