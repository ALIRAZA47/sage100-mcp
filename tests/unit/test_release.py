"""What a release publishes agrees with itself (SPEC.md §16.3): server.json is a valid MCP registry entry
for this version, names commands the CLI has, and the README carries the name the registry checks."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from importlib.metadata import version
from typing import Any

import jsonschema

from sage100_mcp import __version__
from sage100_mcp.cli import build_parser
from tests.paths import REPO

SERVER_JSON = REPO / "server.json"
SCHEMA = REPO / "tests" / "fixtures" / "registry" / "server.schema.2025-12-11.json"


def project() -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return data


def semver(pep440: str) -> str:
    """1.0.0rc1 -> 1.0.0-rc.1, as the registry's version field reads."""
    found = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", pep440)
    assert found, f"{pep440} is not a plain release or pre-release"
    base, kind, number = found.groups()
    names = {"a": "alpha", "b": "beta", "rc": "rc"}
    return base if kind is None else f"{base}-{names[kind]}.{number}"


def entry() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    return data


def test_server_json_is_a_valid_registry_entry() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    data = entry()
    jsonschema.validate(data, schema)
    assert data["$schema"] == schema["$id"]


def test_versions_agree() -> None:
    release = project()["version"]
    assert __version__ == version("sage100-mcp") == release
    data = entry()
    assert data["version"] == semver(release)
    assert {package["version"] for package in data["packages"]} == {release}
    assert {package["identifier"] for package in data["packages"]} == {project()["name"]}


def commands() -> set[str]:
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("the CLI has no commands")


def test_the_entry_names_real_commands() -> None:
    known = commands()
    for package in entry()["packages"]:
        command = package["packageArguments"][0]["value"]
        assert command in known
        assert (command == "serve") == (package["transport"]["type"] == "streamable-http")


def test_the_readme_carries_the_registry_name() -> None:
    """The registry checks a PyPI package's description for its server name before it accepts it."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {entry()['name']}" in readme
