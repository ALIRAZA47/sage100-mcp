"""The curated overlay, catalog/sage100.yaml (SPEC.md §7.2)."""

from __future__ import annotations

from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sage100_mcp.catalog.model import Semantic
from sage100_mcp.config import ConfigError, parse_yaml


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnNote(_Strict):
    semantic: Semantic | None = None
    enum: dict[str, str] | None = None
    observed: list[str] | None = None
    description: str | None = None


class JoinNote(_Strict):
    table: str
    on: list[str] | dict[str, str]

    def pairs(self) -> tuple[tuple[str, str], ...]:
        if isinstance(self.on, dict):
            return tuple(self.on.items())
        return tuple((name, name) for name in self.on)


class TableNote(_Strict):
    purpose: str
    module: str | None = None
    key: list[str] = Field(default_factory=list)
    core: list[str] = Field(default_factory=list)
    joins: dict[str, JoinNote] = Field(default_factory=dict)
    semantics: dict[str, Semantic | ColumnNote] = Field(default_factory=dict)
    default_filters: list[str] = Field(default_factory=list)
    requires_filter: bool = False
    notes: list[str] = Field(default_factory=list)

    def column_note(self, name: str) -> ColumnNote | None:
        for column, note in self.semantics.items():
            if column.lower() == name.lower():
                return ColumnNote(semantic=note) if isinstance(note, str) else note
        return None


class Overlay(_Strict):
    version: int
    modules: dict[str, str] = Field(default_factory=dict)
    third_party_modules: list[str] = Field(default_factory=list)
    tables: dict[str, TableNote] = Field(default_factory=dict)

    @field_validator("tables")
    @classmethod
    def _unique(cls, value: dict[str, TableNote]) -> dict[str, TableNote]:
        lowered = [name.lower() for name in value]
        if len(set(lowered)) != len(lowered):
            raise ValueError("a table appears twice")
        return value

    def table(self, name: str) -> TableNote | None:
        lowered = name.lower()
        return next((note for table, note in self.tables.items() if table.lower() == lowered), None)


def load_overlay(text: str | None = None) -> Overlay:
    """The overlay shipped with the package, or ``text`` in the same format."""
    if text is None:
        text = files("sage100_mcp.catalog").joinpath("sage100.yaml").read_text(encoding="utf-8")
    try:
        return Overlay.model_validate(parse_yaml(text))
    except ValidationError as exc:
        problems = "\n".join(f"  {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_url=False))
        raise ConfigError(f"invalid catalog overlay:\n{problems}") from None
