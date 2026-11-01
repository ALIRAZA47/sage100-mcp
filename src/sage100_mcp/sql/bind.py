"""Query parameters (SPEC.md §5.5 step 5): the model's JSON values, bound to ``?`` placeholders.

Premium binds every value as a driver parameter, never as SQL text. Inlining typed literals, the
ProvideX fallback when the driver rejects ``?``, arrives with the ProvideX backend in M2.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlglot import exp

from sage100_mcp.errors import ErrorCode, SageError

Param = str | int | float | bool | None
Bound = str | int | bool | Decimal | date | datetime | None
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?")


def bind_value(value: object, position: int) -> Bound:
    """A JSON value as the driver should receive it: ISO dates become dates, floats exact decimals."""
    if value is None or isinstance(value, bool | int | Decimal | date):
        return value  # a canned view's own typed values (date covers datetime)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SageError(ErrorCode.PARSE_ERROR, f"params[{position}] must be a finite number.")
        return Decimal(repr(value))  # 0.1 stays 0.1 rather than 0.1000000000000000055511151231257827
    if isinstance(value, str):
        try:
            if _ISO_DATE.fullmatch(value):
                return date.fromisoformat(value)  # a date, so SQL Server never reads it per DATEFORMAT
            if _ISO_DATETIME.fullmatch(value):
                return datetime.fromisoformat(value)
        except ValueError:
            raise SageError(
                ErrorCode.PARSE_ERROR, f"params[{position}] is not a valid date: {value!r}.", hint="Use YYYY-MM-DD."
            ) from None
        return value
    raise SageError(
        ErrorCode.PARSE_ERROR,
        f"params[{position}] must be text, a number, true, false or null, not {type(value).__name__}.",
    )


def bind(params: Sequence[object] | None, placeholders: int) -> list[Bound]:
    """One bound value per ``?``, in order."""
    values = list(params or ())
    if len(values) != placeholders:
        raise SageError(
            ErrorCode.PARSE_ERROR,
            f"The SQL has {placeholders} ? placeholder{'s' if placeholders != 1 else ''} "
            f"but params has {len(values)} value{'s' if len(values) != 1 else ''}.",
            hint="Pass one params value per ?, in the order they appear.",
        )
    return [bind_value(value, position) for position, value in enumerate(values)]


def _written_order(tree: exp.Expr, dialect: str) -> list[exp.Placeholder]:
    """The ? markers in the order ``dialect``'s SQL text writes them.

    Not the tree's walk order: a WITH clause is attached to its SELECT after parsing, so its markers
    come last in a walk although they come first in the text.
    """
    markers = [node for node in tree.find_all(exp.Placeholder) if node.this is None]
    for number, node in enumerate(markers):
        node.set("this", f"sagemarker{number:06d}x")
    try:
        text = tree.sql(dialect=dialect)
    finally:
        for node in markers:
            node.set("this", None)
    positions = {id(node): text.find(f"sagemarker{number:06d}x") for number, node in enumerate(markers)}
    return sorted((node for node in markers if positions[id(node)] >= 0), key=lambda node: positions[id(node)])


def number_placeholders(tree: exp.Expr, dialect: str = "tsql") -> int:
    """Tag each ? with its position in the statement, so values stay matched when clauses move."""
    markers = _written_order(tree, dialect)
    for position, node in enumerate(markers):
        node.meta["param"] = position
    return len(markers)


def ordered_params(tree: exp.Expr, values: Sequence[Bound], dialect: str) -> list[Bound]:
    """The bound values in the order ``dialect``'s SQL for ``tree`` (after any rewriting) writes the markers."""
    order = [node.meta.get("param") for node in _written_order(tree, dialect)]
    return [values[index] for index in order if isinstance(index, int)]
