"""The catalog a company's tools work from: tables, columns, keys, joins and meanings (SPEC.md §7)."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal

Semantic = Literal["code", "text", "money", "quantity", "rate", "number", "date", "time", "flag", "key"]

_TIME = re.compile(r"(?i)^time[A-Za-z]*$|time$")
_QUANTITY = re.compile(r"(?i)quantity|qty")
_RATE = re.compile(r"(?i)rate|percent|pct|factor|convfctr|prcnt")
_MONEY = re.compile(
    r"(?i)amt|amount|balance|cost|price|value|limit|charge|sales|payment|deposit|freight|tax|discount|debit|credit|"
    r"commission|retention|aging|prepaid|invoiced|received|total"
)
_CODE_SUFFIX = re.compile(r"(?i)(no|code|key|type|status|class|level|id|sort|cycle|method|seqno)$")


def infer_semantic(name: str, sql_type: str) -> Semantic:
    """A best guess from the name and type; the overlay overrides it for core tables."""
    base, _, size = sql_type.partition("(")
    base = base.lower()
    if base in {"datetime", "date", "datetime2", "smalldatetime"}:
        return "date"
    if base in {"decimal", "numeric", "int", "smallint", "bigint", "tinyint", "float", "real", "money"}:
        precision, _, scale = size.rstrip(")").partition(",")
        if _QUANTITY.search(name):
            return "quantity"
        if _RATE.search(name):
            return "rate"
        if base in {"decimal", "numeric"} and scale.strip() == "0" and precision.isdigit() and int(precision) <= 7:
            return "number"  # counts and day numbers such as AgingCategory1 = 30
        if _MONEY.search(name):
            return "money"
        return "number"
    if _TIME.search(name) and size.startswith("8"):
        return "time"
    if name.endswith("Key"):
        return "key"
    if _CODE_SUFFIX.search(name):
        return "code"
    return "text"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    semantic: Semantic
    tags: tuple[str, ...] = ()  # udf, third_party, masked, audit
    description: str | None = None
    enum: dict[str, str] | None = None
    observed: tuple[str, ...] | None = None

    @property
    def masked(self) -> bool:
        return "masked" in self.tags


@dataclass(frozen=True)
class Join:
    name: str
    table: str
    on: tuple[tuple[str, str], ...]  # (column here, column there)


@dataclass(frozen=True)
class Table:
    name: str
    module: str
    columns: tuple[Column, ...]
    key: tuple[str, ...] = ()
    key_source: Literal["live", "overlay", "none"] = "none"
    row_hint: int | None = None
    work: bool = False
    third_party: bool = False
    purpose: str | None = None
    core: tuple[str, ...] = ()
    joins: tuple[Join, ...] = ()
    default_filters: tuple[str, ...] = ()
    requires_filter: bool = False
    notes: tuple[str, ...] = ()

    @property
    def curated(self) -> bool:
        return self.purpose is not None

    def column(self, name: str) -> Column | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)

    def default_columns(self) -> tuple[Column, ...]:
        """What describe_table and SELECT * show unless all columns are asked for."""
        if self.core:
            wanted = {name.lower() for name in (*self.key, *self.core)}
            return tuple(c for c in self.columns if c.name.lower() in wanted)
        return tuple(c for c in self.columns if not {"udf", "third_party"} & set(c.tags))


@dataclass
class Catalog:
    company: str
    backend: str
    data_versions: dict[str, str]
    built_at: str
    tables: dict[str, Table] = field(default_factory=dict)  # keyed by lower-cased name
    drift: list[str] = field(default_factory=list)  # overlay entries the live layer contradicts

    def table(self, name: str) -> Table | None:
        return self.tables.get(name.lower())

    def suggest(self, name: str, limit: int = 3) -> list[str]:
        """Close table names, for "did you mean" (CI_ItemWarehouse -> IM_ItemWarehouse)."""
        lowered = name.lower()
        names = {t.name.lower(): t.name for t in self.tables.values()}
        matches = difflib.get_close_matches(lowered, names, n=limit, cutoff=0.6)
        if not matches and "_" in lowered:  # same entity, another module prefix
            entity = lowered.split("_", 1)[1]
            matches = [low for low in names if low.split("_", 1)[-1] == entity][:limit]
        return [names[m] for m in matches]

    def suggest_column(self, table: Table, name: str, limit: int = 3) -> list[str]:
        names = {c.name.lower(): c.name for c in table.columns}
        return [names[m] for m in difflib.get_close_matches(name.lower(), names, n=limit, cutoff=0.6)]
