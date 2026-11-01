"""Builds a company's catalog from the live layer, the overlay and the policy (SPEC.md §7.1).

The live layer (what exists) is read from the backend and cached per company, keyed by the
modules' DataVersion. The overlay and the policy are applied every time the cache is loaded, so a
policy change takes effect on restart without a rebuild.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from sage100_mcp.catalog.model import Catalog, Column, Join, Table, infer_semantic
from sage100_mcp.catalog.overlay import Overlay, TableNote
from sage100_mcp.policy import Policy

CACHE_FORMAT = 1
_WORK_SUFFIXES = ("wrk", "work", "restart", "tmp", "selection")
_THIRD_PARTY_TABLE = re.compile(r"^[A-Z]{2}\d+_|^[A-Z]{2}_\d{2,}", re.IGNORECASE)  # AR234_AVA_, BC_140_, CI_519
_THIRD_PARTY_COLUMN = re.compile(r"^[A-Z]{1,4}_?\d{2,}[A-Z]*_", re.IGNORECASE)  # D519SF_, AVA234_, AVA_234_, A32_
_AUDIT = {"datecreated", "timecreated", "usercreatedkey", "dateupdated", "timeupdated", "userupdatedkey"}


@dataclass(frozen=True)
class RawColumn:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class RawTable:
    name: str
    columns: tuple[RawColumn, ...]
    key: tuple[str, ...] = ()
    row_hint: int | None = None


@dataclass(frozen=True)
class LiveLayer:
    """What a backend reports for one company, before overlay and policy."""

    company: str
    backend: str
    data_versions: dict[str, str]
    built_at: str
    tables: tuple[RawTable, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps({"format": CACHE_FORMAT, **asdict(self)}, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> LiveLayer | None:
        """None when the text is not a cache this version can read."""
        try:
            data = json.loads(text)
            if data.get("format") != CACHE_FORMAT:
                return None
            tables = tuple(
                RawTable(
                    name=t["name"],
                    columns=tuple(RawColumn(**c) for c in t["columns"]),
                    key=tuple(t["key"]),
                    row_hint=t["row_hint"],
                )
                for t in data["tables"]
            )
            return cls(data["company"], data["backend"], dict(data["data_versions"]), data["built_at"], tables)
        except (ValueError, KeyError, TypeError):
            return None


class CatalogStore:
    """Live layers cached as <home>/catalog/<company>.json."""

    def __init__(self, home: Path) -> None:
        self.directory = home / "catalog"

    def path(self, company: str) -> Path:
        return self.directory / f"{company}.json"

    def load(self, company: str) -> LiveLayer | None:
        try:
            return LiveLayer.from_json(self.path(company).read_text(encoding="utf-8"))
        except OSError:
            return None

    def save(self, live: LiveLayer) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path(live.company)
        partial = target.with_suffix(".tmp")
        partial.write_text(live.to_json(), encoding="utf-8")
        partial.replace(target)


def module_of(table: str) -> str:
    return table.split("_", 1)[0].upper()


def is_work_table(table: str) -> bool:
    return table.lower().endswith(_WORK_SUFFIXES)


def _column(table: str, raw: RawColumn, note: TableNote | None, policy: Policy) -> Column:
    tags: list[str] = []
    if raw.name.upper().startswith("UDF_"):
        tags.append("udf")
    if _THIRD_PARTY_COLUMN.match(raw.name):
        tags.append("third_party")
    if raw.name.lower() in _AUDIT:
        tags.append("audit")
    if policy.is_masked(table, raw.name):
        tags.append("masked")
    column_note = note.column_note(raw.name) if note else None
    semantic = (column_note.semantic if column_note else None) or infer_semantic(raw.name, raw.type)
    if column_note and column_note.enum and not (column_note and column_note.semantic):
        semantic = "code"
    return Column(
        name=raw.name,
        type=raw.type,
        nullable=raw.nullable,
        semantic=semantic,
        tags=tuple(tags),
        description=column_note.description if column_note else None,
        enum=dict(column_note.enum) if column_note and column_note.enum else None,
        observed=tuple(column_note.observed) if column_note and column_note.observed is not None else None,
    )


def assemble(live: LiveLayer, overlay: Overlay, policy: Policy) -> Catalog:
    """Apply the policy (denied tables vanish) and the overlay to a live layer."""
    catalog = Catalog(
        company=live.company, backend=live.backend, data_versions=dict(live.data_versions), built_at=live.built_at
    )
    third_party_modules = {m.upper() for m in overlay.third_party_modules}
    visible = [raw for raw in live.tables if policy.denied_by(raw.name) is None]
    names = {raw.name.lower() for raw in visible}
    for raw in visible:
        note = overlay.table(raw.name)
        columns = tuple(_column(raw.name, c, note, policy) for c in raw.columns)
        present = {c.name.lower(): c.name for c in columns}
        if note:
            for wanted in [*note.key, *note.core, *note.semantics]:
                if wanted.lower() not in present:
                    catalog.drift.append(f"{raw.name}.{wanted} is in the overlay but not in the database")
        joins = []
        for join_name, join in note.joins.items() if note else ():
            if join.table.lower() not in names:
                continue  # the other table is denied or this company lacks the module
            joins.append(Join(join_name, join.table, join.pairs()))
        key = tuple(present.get(k.lower(), k) for k in raw.key) if raw.key else ()
        key_source: Literal["live", "overlay", "none"] = "live" if key else "none"
        if not key and note and note.key:
            key, key_source = tuple(present.get(k.lower(), k) for k in note.key), "overlay"
        module = (note.module if note and note.module else None) or module_of(raw.name)
        catalog.tables[raw.name.lower()] = Table(
            name=raw.name,
            module=module,
            columns=columns,
            key=key,
            key_source=key_source,
            row_hint=raw.row_hint,
            work=is_work_table(raw.name),
            third_party=module in third_party_modules or bool(_THIRD_PARTY_TABLE.match(raw.name)),
            purpose=note.purpose if note else None,
            core=tuple(present[c.lower()] for c in note.core if c.lower() in present) if note else (),
            joins=tuple(joins),
            default_filters=tuple(note.default_filters) if note else (),
            requires_filter=note.requires_filter if note else False,
            notes=tuple(note.notes) if note else (),
        )
    return catalog
