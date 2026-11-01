"""Text renderings that accompany ``structuredContent`` for clients that only read text (SPEC.md §11.1)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

TEXT_ROW_LIMIT = 50
_CELL_WIDTH = 60


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    # One line per row, and no stray pipes, so a cell's text cannot change the table's shape.
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= _CELL_WIDTH else text[: _CELL_WIDTH - 1] + "…"


def text_table(columns: Sequence[str], rows: Sequence[Sequence[object]], *, limit: int = TEXT_ROW_LIMIT) -> str:
    """A Markdown table of the first ``limit`` rows, with a footer when rows were left out."""
    lines = ["| " + " | ".join(_cell(c) for c in columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"Showing {limit} of {len(rows)} rows; all rows are in structuredContent.")
    return "\n".join(lines)


def text_fields(fields: Iterable[tuple[str, object]]) -> str:
    """``label: value`` lines for server-side values (not Sage rows), skipping empty ones."""
    lines = []
    for label, value in fields:
        if value in (None, "", [], ()):
            continue
        lines.append(f"{label}: " + str(value).strip().replace("\n", "\n  "))
    return "\n".join(lines)
