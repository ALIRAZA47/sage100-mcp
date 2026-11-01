"""sage100-mcp init (SPEC.md §16.2) against emulated installs: the SOTAMAS90 template, a login tested
before it is stored, the companies it can open, and a config the server accepts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sage100_mcp.backends.providex import ProvideXExecutor
from sage100_mcp.config import CompaniesSection, Config, ProvideXBackend, SqlServerBackend, load_config
from sage100_mcp.credentials import Credential
from sage100_mcp.setup import Answers, Unattended, run_init
from tests import emulator
from tests.emulator.conftest import install_at


class Script:
    """Answers prompts in order, and records the questions."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def ask(self, question: str, default: str | None = None) -> str:
        self.asked.append(question)
        answer = self.answers.pop(0)
        return answer or (default or "")

    def secret(self, question: str) -> str:
        self.asked.append(question)
        return self.answers.pop(0)


@pytest.fixture
def install(tmp_path: Path) -> emulator.Install:
    found = install_at(tmp_path, "pxplus_full")
    emulator.INSTALLS[found.directory.lower()] = found
    return found


def emulated(name: str, settings: ProvideXBackend | SqlServerBackend, credential: Credential, home: Path) -> Any:
    assert isinstance(settings, ProvideXBackend)
    return ProvideXExecutor(name, settings, credential, Config().limits, home=home,
                            companies=CompaniesSection(), odbc=emulator)  # fmt: skip


def run(tmp_path: Path, install: emulator.Install, prompter: Any, answers: Answers | None = None,
        **options: Any) -> tuple[int, list[str], list[tuple[str, str, str]]]:  # fmt: skip
    said: list[str] = []
    stored: list[tuple[str, str, str]] = []
    template = {"Directory": install.directory, "Prefix": f"{install.directory}\\SY\\, {install.directory}\\==\\",
                "ViewDLL": f"{install.directory}\\Home"}  # fmt: skip
    code = run_init(
        tmp_path / "config.yaml", answers or Answers(), prompter, home=tmp_path / "home", connect=emulated,
        store=lambda *entry: stored.append(entry), registry=template.get, say=said.append, **options,
    )  # fmt: skip
    return code, said, stored


def test_init_from_the_sotamas90_template(tmp_path: Path, install: emulator.Install) -> None:
    prompts = Script("", "", "mcp_reader", "reader-pass", "emp")  # edition and folder take the defaults
    code, said, stored = run(tmp_path, install, prompts)
    assert code == 0, said
    assert stored == [("sage100-mcp/sage", "mcp_reader", "reader-pass")]
    config = load_config(tmp_path / "config.yaml", env={}, must_exist=True).config
    backend = config.backends["sage"]
    assert isinstance(backend, ProvideXBackend)
    assert (backend.directory, backend.credential, config.companies.default) == (
        install.directory, "sage100-mcp/sage", "EMP"
    )  # fmt: skip
    assert backend.prefix.endswith("==\\")
    assert backend.viewdll.endswith("Home")
    assert any(line.startswith("Companies: ABC (") and "EMP (" in line for line in said)
    assert "reader-pass" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_a_login_that_fails_is_neither_stored_nor_written(tmp_path: Path, install: emulator.Install) -> None:
    code, said, stored = run(tmp_path, install, Script("providex", "", "mcp_reader", "wrong"))
    assert code == 1
    assert said[-1].startswith("Could not connect: AUTH_FAILED")
    assert stored == []
    assert not (tmp_path / "config.yaml").exists()


def test_an_existing_config_is_kept_unless_forced(tmp_path: Path, install: emulator.Install) -> None:
    (tmp_path / "config.yaml").write_text("limits: {max_rows: 10}\n", encoding="utf-8")
    code, said, _ = run(tmp_path, install, Script())
    assert code == 2
    assert "already exists" in said[0]
    answers = Answers(kind="providex", user="mcp_reader", password="reader-pass", company="ABC")
    code, said, _ = run(tmp_path, install, Unattended(), answers, force=True)
    assert code == 0, said
    assert load_config(tmp_path / "config.yaml", env={}).config.companies.default == "ABC"


def test_unattended_init_stops_for_a_missing_flag(tmp_path: Path, install: emulator.Install) -> None:
    code, said, _ = run(tmp_path, install, Unattended(), Answers(kind="providex", user="mcp_reader"))
    assert code == 1
    assert "--password-env" in said[-1]
    code, said, _ = run(tmp_path, install, Unattended(), Answers(kind="sqlserver"))
    assert (code, "give it as a flag" in said[-1]) == (1, True)


def test_a_drive_letter_gets_a_warning(tmp_path: Path, install: emulator.Install) -> None:
    answers = Answers(kind="providex", directory="S:\\Sage\\MAS90", user="mcp_reader", password="reader-pass")
    code, said, _ = run(tmp_path, install, Unattended(), answers)
    assert any("A Windows service has no mapped drives" in line for line in said)
    assert code == 1, "the emulator knows no S: drive, so the connection fails"


def test_the_default_company_must_be_one_the_login_opens(tmp_path: Path, install: emulator.Install) -> None:
    answers = Answers(kind="providex", user="mcp_reader", password="reader-pass", company="XYZ")
    code, said, stored = run(tmp_path, install, Unattended(), answers)
    assert code == 1
    assert "XYZ is not among the companies" in said[-1]
    assert stored == []


def test_without_a_keyring_init_names_the_environment_variables(tmp_path: Path, install: emulator.Install) -> None:
    """A headless Linux host or a container has no keyring; the server reads the login from its environment."""
    import keyring.errors

    def no_keyring(*entry: str) -> None:
        raise keyring.errors.NoKeyringError("No recommended backend was available.")

    said: list[str] = []
    code = run_init(
        tmp_path / "config.yaml", Answers(), Script("", "", "mcp_reader", "reader-pass", "emp"), home=tmp_path / "home",
        connect=emulated, store=no_keyring, registry={"Directory": install.directory}.get, say=said.append,
    )  # fmt: skip
    assert code == 0, said
    assert any("SAGE100_MCP_CRED_SAGE_USER and SAGE100_MCP_CRED_SAGE_PASSWORD" in line for line in said), said
    assert not any("reader-pass" in line for line in said)
    assert load_config(tmp_path / "config.yaml", env={}, must_exist=True).config.backends["sage"]
