"""Canned view definitions (SPEC.md §12): what views/*.yaml hold, and how parameters bind.

A view has two plans that must return identical rows (§12.9, §15.4):

- the ProvideX plan: named single-table ``pulls`` that the planner sends to the driver as narrowly
  as it can, then ``local`` SQL that DuckDB runs over them (joins, buckets, math);
- the Premium plan: one T-SQL statement (``tsql``).

Both are written with ``:name`` parameters. A line that ends with ``-- if name`` is kept only when
that parameter has a value, which is how optional filters stay out of the SQL entirely.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sage100_mcp.catalog.model import Semantic
from sage100_mcp.errors import ErrorCode, SageError

ParamType = Literal[
    "date", "text", "code", "customer", "vendor", "choice", "codes", "decimal", "bool", "year", "period"
]
_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")
_IF_LINE = re.compile(r"^(?P<body>.*?)\s*--\s*if\s+(?P<negate>not\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*$")
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/&#-]{0,29}")
_STRING = re.compile(r"'(?:[^']|'')*'")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Param(_Model):
    type: ParamType
    description: str
    default: Any = None
    required: bool = False
    choices: list[str] | None = None  # choice and codes
    max_length: int | None = None  # code


class OutputColumn(_Model):
    name: str
    type: str  # varchar(20), decimal(17,2), date, int
    semantic: Semantic
    description: str | None = None

    @property
    def scale(self) -> int | None:
        found = re.fullmatch(r"decimal\(\d+,\s*(\d+)\)", self.type)
        return int(found.group(1)) if found else None


class Condition(_Model):
    sql: str
    when: str | None = Field(default=None, alias="if")  # the parameter that must have a value


class KeysFrom(_Model):
    """A pull filtered by the key values another pull returned: ``column IN (...)``, in chunks."""

    pull: str
    column: str  # in the other pull
    filter: str  # this pull's column
    chunk: int = 200


class Pull(_Model):
    name: str
    table: str
    columns: list[str]
    where: list[Condition | str] = Field(default_factory=list)
    keys_from: KeysFrom | None = None
    only_when: dict[str, str] | None = None  # read only when these parameters have these values

    def wanted(self, params: dict[str, Any]) -> bool:
        return all(str(params.get(name)) == value for name, value in (self.only_when or {}).items())

    def conditions(self, params: dict[str, Any]) -> list[str]:
        found = []
        for item in self.where:
            condition = Condition(sql=item) if isinstance(item, str) else item
            if condition.when is None or params.get(condition.when) not in (None, "", []):
                found.append(condition.sql)
        return found

    def sql(self, params: dict[str, Any], extra: list[str] | None = None) -> str:
        conditions = self.conditions(params) + list(extra or [])
        text = f"SELECT {', '.join(self.columns)} FROM {self.table}"
        return text + (" WHERE " + " AND ".join(conditions) if conditions else "")


class Variant(_Model):
    columns: list[OutputColumn]
    tsql: str  # the Premium plan
    local: str  # the ProvideX plan's DuckDB query over the pulls
    totals: list[str] = Field(default_factory=list, description="Columns summed into the result's totals")


class ViewDef(_Model):
    name: str
    version: int
    title: str
    description: str  # the tool description, before the rows-are-data sentence
    modules: list[str]
    params: dict[str, Param]
    pulls: list[Pull] = Field(default_factory=list)
    variant_param: str | None = None  # whose value picks the variant; else the single variant "default"
    variants: dict[str, Variant]
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> ViewDef:
        if self.variant_param is None and set(self.variants) != {"default"}:
            raise ValueError(f"{self.name}: without variant_param the only variant is 'default'")
        if self.variant_param is not None:
            choices = self.params[self.variant_param].choices or ["true", "false"]
            if set(self.variants) != set(choices):
                raise ValueError(f"{self.name}: variants {sorted(self.variants)} do not match {choices}")
        names = [p.name for p in self.pulls]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: pull names repeat")
        for pull in self.pulls:
            if pull.keys_from is not None and pull.keys_from.pull not in names[: names.index(pull.name)]:
                raise ValueError(f"{self.name}: {pull.name} reads keys from a pull that does not come before it")
        for key, variant in self.variants.items():
            for column in variant.totals:
                if column not in {c.name for c in variant.columns}:
                    raise ValueError(f"{self.name}: total {column} is not an output column of variant {key}")
        return self

    def variant(self, params: dict[str, Any]) -> Variant:
        if self.variant_param is None:
            return self.variants["default"]
        value = params[self.variant_param]
        return self.variants[str(value).lower() if isinstance(value, bool) else str(value)]


# --- parameters ------------------------------------------------------------------------------


def _bad(name: str, message: str, hint: str | None = None) -> SageError:
    return SageError(ErrorCode.PARSE_ERROR, f"{name}: {message}", hint=hint)


def split_account(value: str, name: str) -> tuple[str | None, str]:
    """'ABF' or '01-ABF' (division, then number, as Sage shows them) -> (division or None, number)."""
    text = value.strip().upper()
    division, dash, number = text.partition("-")
    if dash and len(division) == 2 and division.isalnum() and number:
        return division, number
    if not _CODE.fullmatch(text):
        raise _bad(name, f"{value!r} is not a customer or vendor number.", "Use the number, or division-number.")
    return None, text


def bind_params(view: ViewDef, args: dict[str, Any], *, today: date) -> dict[str, Any]:
    """The view's parameters, typed and defaulted; unknown arguments are refused."""
    unknown = set(args) - set(view.params)
    if unknown:
        raise _bad(
            view.name, f"unknown argument(s) {', '.join(sorted(unknown))}.", f"It takes {', '.join(view.params)}."
        )
    bound: dict[str, Any] = {}
    for name, spec in view.params.items():
        value = args.get(name)
        if value is None or value == "":
            value = spec.default
        if value is None and spec.required:
            raise _bad(view.name, f"{name} is required.", spec.description)
        bound[name] = _convert(name, spec, value, today)
        if spec.type in ("customer", "vendor") and bound[name] is not None:
            division, number = split_account(bound[name], name)
            bound[f"{name}_division"], bound[f"{name}_no"] = division, number
        elif spec.type in ("customer", "vendor"):
            bound[f"{name}_division"] = bound[f"{name}_no"] = None
    return bound


def _convert(name: str, spec: Param, value: Any, today: date) -> Any:
    if value is None:
        return None
    if spec.type == "date":
        if value == "today":
            return today
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError:
            raise _bad(name, f"{value!r} is not a date.", "Use YYYY-MM-DD.") from None
    if spec.type == "decimal":
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise _bad(name, f"{value!r} is not a number.") from None
        if not number.is_finite():
            raise _bad(name, "must be a finite number.")
        return number
    if spec.type == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise _bad(name, f"{value!r} is not true or false.")
    if spec.type == "choice":
        text = str(value).strip().lower()
        if spec.choices is None or text not in spec.choices:
            raise _bad(name, f"{value!r} is not one of {', '.join(spec.choices or [])}.")
        return text
    if spec.type == "codes":
        items = value if isinstance(value, list | tuple) else str(value).split(",")
        codes = [str(item).strip().upper() for item in items if str(item).strip()]
        allowed = [c.upper() for c in spec.choices or []]
        bad = [c for c in codes if allowed and c not in allowed]
        if bad or not codes:
            raise _bad(name, f"{', '.join(bad) or 'nothing'} is not among {', '.join(allowed)}.")
        return list(dict.fromkeys(codes))
    if spec.type == "year":
        text = str(value).strip()
        if not re.fullmatch(r"\d{4}", text):
            raise _bad(name, f"{value!r} is not a four-digit fiscal year.")
        return text
    if spec.type == "period":
        text = str(value).strip()
        if not re.fullmatch(r"\d{1,2}", text) or not 1 <= int(text) <= 13:
            raise _bad(name, f"{value!r} is not a fiscal period from 1 to 13.")
        return f"{int(text):02d}"
    text = str(value).strip()
    if spec.type == "code":
        text = text.upper()
        if not _CODE.fullmatch(text) or (spec.max_length and len(text) > spec.max_length):
            raise _bad(name, f"{value!r} is not a valid code.")
    elif len(text) > 100:
        raise _bad(name, "is longer than 100 characters.")
    return text


# --- SQL templates ---------------------------------------------------------------------------


def render(template: str, params: dict[str, Any]) -> str:
    """Drop the lines whose ``-- if name`` parameter is unset (or set, with ``-- if not``)."""
    lines = []
    for line in template.splitlines():
        found = _IF_LINE.match(line)
        if found is None:
            lines.append(line)
            continue
        present = params.get(found.group("name")) not in (None, "", [], False)
        if present != bool(found.group("negate")):
            lines.append(found.group("body"))
    return "\n".join(lines).strip()


def _outside_strings(sql: str) -> list[tuple[int, int, str]]:
    """``:name`` markers outside string literals: (start, end, name)."""
    masked = _STRING.sub(lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", sql)
    return [(m.start(), m.end(), m.group(1)) for m in _PARAM.finditer(masked)]


def positional(sql: str, params: dict[str, Any]) -> tuple[str, list[Any]]:
    """``:name`` markers as ``?`` with their values in order; a list value expands to ``?, ?, ...``."""
    pieces, values, last = [], [], 0
    for start, end, name in _outside_strings(sql):
        if name not in params:
            raise SageError(ErrorCode.INTERNAL, f"The view's SQL uses :{name}, which has no value.")
        value = params[name]
        pieces.append(sql[last:start])
        if isinstance(value, list):
            pieces.append(", ".join("?" for _ in value) or "NULL")
            values.extend(value)
        else:
            pieces.append("?")
            values.append(value)
        last = end
    return "".join(pieces) + sql[last:], values


def named(sql: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``:name`` markers as DuckDB's ``$name``, with only the values the SQL uses; lists expand."""
    pieces: list[str] = []
    used: dict[str, Any] = {}
    last = 0
    for start, end, name in _outside_strings(sql):
        if name not in params:
            raise SageError(ErrorCode.INTERNAL, f"The view's SQL uses :{name}, which has no value.")
        value = params[name]
        pieces.append(sql[last:start])
        if isinstance(value, list):
            marks = [f"{name}_{index}" for index in range(len(value))]
            pieces.append(", ".join(f"${mark}" for mark in marks) or "NULL")
            used.update(zip(marks, value, strict=True))
        else:
            pieces.append(f"${name}")
            used[name] = value
        last = end
    return "".join(pieces) + sql[last:], used
