"""search_catalog: rank tables and columns against plain words (SPEC.md §7.4)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sage100_mcp.catalog.model import Catalog, Table

_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SYNONYMS = {"qty": "quantity", "amt": "amount", "desc": "description", "cust": "customer", "inv": "invoice"}
_STOP = {"the", "a", "an", "of", "for", "by", "and", "with", "to", "in", "is", "are", "what", "which", "show", "me"}


def words(text: str) -> list[str]:
    """CamelCase and snake_case split into lower-case words: QuantityOnBackOrder -> quantity on back order."""
    return [w.lower() for part in re.split(r"[^A-Za-z0-9]+", text) for w in _WORD.findall(part)]


@dataclass(frozen=True)
class Hit:
    kind: Literal["table", "column"]
    table: str
    column: str | None
    module: str
    score: float
    purpose: str | None


def _match(query: str, tokens: list[str], joined: str) -> float:
    if query in tokens:
        return 1.0
    if any(t.startswith(query) or (query.startswith(t) and len(t) >= 4) for t in tokens):
        return 0.8
    if len(query) >= 4 and query in joined:
        return 0.7
    return 0.0


def _score(query: list[str], name: str, context: str = "") -> float:
    """Average match over the query words; 0 unless every word matches somewhere.

    Words may match the name itself or, at a discount, ``context`` (a table's purpose, or a
    column's table and description); at least one must match the name itself.
    """
    own, own_joined = words(name), name.lower().replace("_", "")
    around, around_joined = words(context), context.lower().replace("_", "").replace(" ", "")
    total, own_hits = 0.0, 0
    for q in query:
        found = _match(q, own, own_joined)
        if found:
            own_hits += 1
            total += found
            continue
        found = _match(q, around, around_joined) if context else 0.0
        if not found:
            return 0.0
        total += 0.9 * found
    return total / len(query) if own_hits else 0.0


def _weight(table: Table) -> float:
    return (0.2 if table.curated else 0.0) - (0.5 if table.work else 0.0) - (0.15 if table.third_party else 0.0)


def search(catalog: Catalog, text: str, *, limit: int = 50, include_work: bool = False) -> list[Hit]:
    query = [_SYNONYMS.get(w, w) for w in words(text) if w not in _STOP]
    if not query:
        return []
    hits: list[Hit] = []
    for table in catalog.tables.values():
        if table.work and not include_work:
            continue
        weight = _weight(table)
        score = _score(query, table.name, table.purpose or "")
        if score:  # a table that matches outright outranks its own columns
            hits.append(Hit("table", table.name, None, table.module, round(score + weight + 0.15, 3), table.purpose))
        # A column ranks higher when its table's name also matches: CustomerName in AR_Customer first.
        context = 0.1 * sum(1 for q in query if q in words(table.name)) / len(query)
        for column in table.columns:
            if "udf" in column.tags:
                continue
            score = _score(query, column.name, f"{table.name} {column.description or ''}")
            if score:
                total = round(score + weight + context, 3)
                hits.append(Hit("column", table.name, column.name, table.module, total, table.purpose))
    hits.sort(key=lambda h: (-h.score, h.kind != "table", len(h.table), h.table.lower(), (h.column or "").lower()))
    return hits[:limit]
