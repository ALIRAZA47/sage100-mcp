from __future__ import annotations

import re

from sage100_mcp.errors import ErrorCode, SageError, WarningCode
from tests.paths import SPEC

EXTENSIONS = {"BACKEND_UNREACHABLE", "CURSOR_INVALID"}  # codes beyond §14.2, each recorded in docs/decisions.md
WARNING_EXTENSIONS = {"COLUMNS_PRUNED", "LIMIT_CAPPED", "MODE_IGNORED", "COLUMNS_DIFFER"}  # likewise (D21, D46)


def _section(title: str) -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index(title)
    return text[start : text.index("\n### ", start + len(title))]


def test_error_codes_match_the_spec() -> None:
    """Every code in SPEC.md §14.2 exists, and no other."""
    table = _section("### 14.2 Error codes")
    in_spec = set(re.findall(r"^\| `([A-Z_]+)`(?: / `([A-Z_]+)`)?", table, flags=re.M))
    codes = {code for pair in in_spec for code in pair if code}
    assert codes == {code.value for code in ErrorCode} - EXTENSIONS


def test_warning_codes_match_the_spec() -> None:
    sentence = next(line for line in _section("### 14.2 Error codes").splitlines() if line.startswith("Warnings"))
    assert set(re.findall(r"`([A-Z_]+)`", sentence)) == {code.value for code in WarningCode} - WARNING_EXTENSIONS


def test_envelope() -> None:
    error = SageError(
        ErrorCode.COMPANY_REQUIRED,
        "Several companies are exposed and none was chosen.",
        hint="Pass company, or call set_company.",
        company=None,
        choices=["ABC", "EMP"],
    )
    assert error.envelope().model_dump() == {
        "code": ErrorCode.COMPANY_REQUIRED,
        "message": "Several companies are exposed and none was chosen.",
        "hint": "Pass company, or call set_company.",
        "detail": None,
        "company": None,
        "choices": ["ABC", "EMP"],
        "retryable": False,
        "retry_after_s": None,
    }


def test_retryable_codes() -> None:
    retryable = {code for code in ErrorCode if SageError(code, "x").envelope().retryable}
    assert retryable == {ErrorCode.LOCK_TIMEOUT, ErrorCode.RATE_LIMITED, ErrorCode.CATALOG_BUILDING}
