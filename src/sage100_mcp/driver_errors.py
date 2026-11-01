"""Map ODBC driver exceptions to stable error codes (SPEC.md §14.3), redacting what they echo."""

from __future__ import annotations

import re
from functools import cache
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sage100_mcp.config import parse_yaml
from sage100_mcp.errors import ErrorCode, SageError

Phase = Literal["connect", "execute"]
_NATIVE = re.compile(r"\((\d+)\)\s*(?:\(SQL[A-Za-z]+\))?\s*$")
_QUOTED = re.compile(r"N?'(?:[^']|'')*'")
_IDENTIFIER = re.compile(r"'[A-Za-z_][A-Za-z0-9_]{0,127}'")
# SQL Server errors whose quoted text is only ever an object, column or database name, never a value.
_NAME_ERRORS = {207, 208, 209, 229, 230}
_CONNECTION_SECRET = re.compile(r"(?i)\b(PWD|PASSWORD)\s*=\s*(\{(?:[^}]|\}\})*\}|[^;]*)")
_LINE_SECRET = re.compile(r"(?i)\b(PWD|PASSWORD)\s*=\s*(\{(?:[^}]|\}\})*\}|[^;\r\n]*)")  # one line at a time
_BEARER = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
_STATIC_TOKEN = re.compile(r"s1mcp_[A-Za-z0-9_-]+")


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    sqlstate: list[str] = Field(default_factory=list)
    native: list[int] = Field(default_factory=list)
    message: str | None = None
    phase: Phase | None = None
    hint: str | None = None

    def matches(self, sqlstate: str, native: int | None, text: str, phase: Phase) -> bool:
        if self.phase and self.phase != phase:
            return False
        return (
            sqlstate in self.sqlstate
            or (native is not None and native in self.native)
            or (self.message is not None and re.search(self.message, text) is not None)
        )


@cache
def rules(backend: str) -> tuple[Rule, ...]:
    data = parse_yaml(files("sage100_mcp").joinpath("errors.yaml").read_text(encoding="utf-8"))
    return tuple(Rule.model_validate(rule) for rule in data.get(backend, []))


def redact_secrets(text: str) -> str:
    """Connection-string passwords and bearer tokens out of any text, such as a traceback (§9.4).

    Unlike redact(), quoted literals stay: a traceback needs them to be read.
    """
    text = _LINE_SECRET.sub(r"\1=***", text)
    text = _BEARER.sub(r"\1 ***", text)
    return _STATIC_TOKEN.sub("s1mcp_***", text)


def redact(text: str, secrets: tuple[str, ...] = (), *, keep_names: bool = False) -> str:
    """Remove passwords and quoted literals (values, login names) from a driver message.

    With ``keep_names``, quoted identifiers stay: for the errors in _NAME_ERRORS they name the
    table or column at fault, which the model needs in order to fix its query.
    """
    text = _CONNECTION_SECRET.sub(r"\1=***", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _QUOTED.sub(lambda m: m.group(0) if keep_names and _IDENTIFIER.fullmatch(m.group(0)) else "'…'", text)


def split(exc: BaseException) -> tuple[str, str]:
    """(SQLSTATE, message) from a pyodbc-style exception, whose args are (sqlstate, message)."""
    args: tuple[object, ...] = exc.args
    if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
        return args[0], args[1]
    return "", str(exc)


def map_driver_error(
    exc: BaseException, *, backend: str, phase: Phase, company: str | None = None, secrets: tuple[str, ...] = ()
) -> SageError:
    sqlstate, message = split(exc)
    found = _NATIVE.search(message)
    native = int(found.group(1)) if found else None
    detail = redact(message, secrets, keep_names=native in _NAME_ERRORS)
    for rule in rules(backend):
        if rule.matches(sqlstate, native, message, phase):
            text = detail.rsplit("]", 1)[-1].strip() or rule.code.value
            return SageError(rule.code, text, hint=rule.hint, detail=detail, company=company)
    return SageError(
        ErrorCode.INTERNAL,
        "The database returned an unexpected error.",
        hint="The redacted driver message is in detail.",
        detail=f"[{sqlstate}] {detail}" if sqlstate else detail,
        company=company,
    )
