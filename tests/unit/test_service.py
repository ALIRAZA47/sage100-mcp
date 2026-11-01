"""The Windows service (SPEC.md §16.2, §16.5), through a stand-in for the service control manager:
the account, the command line it registers, what install grants, the log file, and the serving loop the
service starts and stops. scripts/service_smoke.py runs the real thing on a Windows CI runner."""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sage100_mcp import service
from sage100_mcp.cli import EXIT_OK, EXIT_USAGE, main
from sage100_mcp.http.host import Host
from tests.fake_backend import fake_runtime


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.running = False

    def create(self, command: str, account: str | None, password: str | None, environment: dict[str, str]) -> None:
        self.calls.append(("create", (command, account, password, environment)))

    def delete(self) -> None:
        self.calls.append(("delete", None))

    def start(self) -> None:
        self.calls.append(("start", None))
        self.running = True

    def stop(self) -> None:
        self.calls.append(("stop", None))
        self.running = False

    def state(self) -> str:
        return "running" if self.running else "stopped"

    def grant_logon_right(self, account: str) -> None:
        self.calls.append(("logon right", account))

    def grant_home_access(self, account: str, home: Path) -> None:
        self.calls.append(("home access", (account, home)))


@pytest.mark.parametrize(
    ("text", "name", "needs_password"),
    [
        ("LocalSystem", None, False),
        ("NT AUTHORITY\\NetworkService", "NT AUTHORITY\\NetworkService", False),
        ("CORP\\svc-sage100mcp", "CORP\\svc-sage100mcp", True),
        ("svc@corp.example", "svc@corp.example", True),
        ("sagesvc", ".\\sagesvc", True),
        ("CORP\\gmsa-sage$", "CORP\\gmsa-sage$", False),
    ],
)
def test_accounts(text: str, name: str | None, needs_password: bool) -> None:
    account = service.parse_account(text)
    assert (account.name, account.needs_password) == (name, needs_password)


def test_the_registered_command_line(tmp_path: Path) -> None:
    config = tmp_path / "with space" / "config.yaml"
    frozen = service.command_line(config, executable=r"C:\Program Files\sage100-mcp\sage100-mcp.exe", frozen=True)
    assert frozen.startswith('"C:\\Program Files\\sage100-mcp\\sage100-mcp.exe" service run --config ')
    assert f'"{config.resolve()}"' in frozen
    source = service.command_line(None, executable="python.exe", frozen=False)
    assert source == "python.exe -m sage100_mcp service run"


def test_install_under_a_domain_account(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, said = FakeManager(), list[str]()
    code = service.install(manager, "CORP\\svc-sage100mcp", password="s3cret", config=None, say=said.append)
    assert code == 0
    (create, (command, account, password, environment)), *grants = manager.calls
    assert (create, account, password, environment) == (
        "create",
        "CORP\\svc-sage100mcp",
        "s3cret",
        {"SAGE100_MCP_HOME": str(isolated_home)},
    )  # the test's home is set through the environment
    assert command.endswith("-m sage100_mcp service run")
    assert grants == [("logon right", "CORP\\svc-sage100mcp"), ("home access", ("CORP\\svc-sage100mcp", isolated_home))]
    assert isolated_home.is_dir()
    assert not any("s3cret" in line for line in said)


def test_install_refuses_a_missing_password(tmp_path: Path) -> None:
    manager, said = FakeManager(), list[str]()
    assert service.install(manager, "CORP\\svc", password=None, config=None, say=said.append) == 2
    assert manager.calls == []
    assert "--password-env" in said[0]


def test_localsystem_needs_no_password_and_warns_for_providex(write_config: Any) -> None:
    backend = "{type: providex, directory: 'S:\\\\MAS90', credential: sage100-mcp/sage}"
    config = write_config(f"backends:\n  sage: {backend}\n")
    manager, said = FakeManager(), list[str]()
    assert service.install(manager, "LocalSystem", password=None, config=config, say=said.append) == 0
    assert [name for name, _ in manager.calls] == ["create"]
    assert manager.calls[0][1][1:3] == (None, None)
    assert any("can't read a Sage share over UNC" in line for line in said)
    assert any("has a ProvideX backend" in line for line in said)


def test_the_service_log_redacts_secrets(tmp_path: Path) -> None:
    handler = service.file_logging(tmp_path, "info")
    try:
        logging.getLogger("sage100_mcp.test_service").error("connecting with DRIVER={x};PWD=hunter2")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    text = (tmp_path / "logs" / service.LOG_FILE).read_text(encoding="utf-8")
    assert "PWD=***" in text
    assert "hunter2" not in text


def test_off_windows_the_command_points_to_serve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.platform", "linux")  # on Windows, never reach the real service manager
    assert main(["service", "install", "--account", "LocalSystem"]) == EXIT_USAGE
    assert "sage100-mcp serve" in capsys.readouterr().err
    assert main(["service", "run"]) == 2
    assert "use sage100-mcp serve" in capsys.readouterr().err


def test_the_cli_drives_the_service_manager(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manager = FakeManager()
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(service, "Win32Manager", lambda: manager)
    monkeypatch.setenv("SVC_PASSWORD", "s3cret")
    install = ["service", "install", "--account", "CORP\\svc", "--password-env", "SVC_PASSWORD"]
    assert main(install) == EXIT_OK
    assert main(["service", "start"]) == EXIT_OK
    assert main(["service", "status"]) == EXIT_OK
    assert capsys.readouterr().out.rstrip().endswith("sage100-mcp: running")
    assert main(["service", "remove"]) == EXIT_OK
    assert [name for name, _ in manager.calls] == ["create", "logon right", "home access", "start", "stop", "delete"]
    monkeypatch.delenv("SVC_PASSWORD")
    assert main(install) == EXIT_USAGE


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_the_host_the_service_runs_stops_when_asked(tmp_path: Path) -> None:
    runtime, _ = fake_runtime(tmp_path / "home", start=False, http={"bind": f"127.0.0.1:{free_port()}"})
    host = Host(runtime.loaded, runtime)
    outcome: list[bool] = []
    thread = threading.Thread(target=lambda: outcome.append(host.run()))
    thread.start()
    deadline = time.monotonic() + 20
    while not host.server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert host.server.started
    host.stop()  # what SvcStop does
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert outcome == [True]
