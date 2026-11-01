"""Table and column policy, layer 4 of SPEC.md §9 (§9.2).

Denied tables never enter the catalog, so the model cannot see them, and masked columns come back
as "***". The default policy ships in the package; config.yaml's policy_file replaces it.
"""

from __future__ import annotations

import fnmatch
import re
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sage100_mcp.config import ConfigError, parse_yaml

MASK = "***"


class TableRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    module: str | None = None  # a rule with a module is lifted by allow_modules

    def matches(self, table: str) -> bool:
        return fnmatch.fnmatchcase(table.lower(), self.pattern.lower())


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deny_tables: list[TableRule]
    allow_modules: list[str] = Field(default_factory=list)
    mask_columns: str
    mask_exceptions: list[str] = Field(default_factory=list)

    @field_validator("allow_modules")
    @classmethod
    def _upper(cls, value: list[str]) -> list[str]:
        return [module.strip().upper() for module in value]

    @field_validator("mask_columns")
    @classmethod
    def _regex(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"mask_columns is not a valid regular expression: {exc}") from None
        return value

    @field_validator("mask_exceptions")
    @classmethod
    def _qualified(cls, value: list[str]) -> list[str]:
        for name in value:
            if name.count(".") != 1 or not all(name.split(".")):
                raise ValueError(f"mask exception {name!r} must be Table.Column")
        return value

    def denied_by(self, table: str) -> TableRule | None:
        """The rule that hides ``table``, or None if it may be read."""
        for rule in self.deny_tables:
            if rule.module and rule.module.upper() in self.allow_modules:
                continue
            if rule.matches(table):
                return rule
        return None

    def is_masked(self, table: str, column: str) -> bool:
        if f"{table}.{column}".lower() in {name.lower() for name in self.mask_exceptions}:
            return False
        return re.search(self.mask_columns, column) is not None


def load_policy(path: Path | None = None) -> Policy:
    """The policy at ``path``, or the default shipped with the package."""
    source = str(path) if path else "the built-in policy"
    try:
        text = path.read_text(encoding="utf-8") if path else files("sage100_mcp").joinpath("policy.yaml").read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read policy file {path}: {exc.strerror}") from None
    try:
        data = parse_yaml(text)  # a repeated deny_tables key must not silently replace the first
        return Policy.model_validate(data)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source} is not valid YAML:\n{exc}") from None
    except ValidationError as exc:
        problems = "\n".join(f"  {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_url=False))
        raise ConfigError(f"invalid policy in {source}:\n{problems}") from None
