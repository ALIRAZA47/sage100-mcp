"""Stable error codes and the error envelope every tool returns (SPEC.md §11.3, §14)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    WRITE_BLOCKED = "WRITE_BLOCKED"
    MULTI_STATEMENT = "MULTI_STATEMENT"
    FORBIDDEN_FUNCTION = "FORBIDDEN_FUNCTION"
    PARSE_ERROR = "PARSE_ERROR"
    UNKNOWN_TABLE = "UNKNOWN_TABLE"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    TABLE_DENIED = "TABLE_DENIED"
    FILTER_REQUIRED = "FILTER_REQUIRED"
    DIALECT_UNSUPPORTED = "DIALECT_UNSUPPORTED"
    DRIVER_INTERNAL = "DRIVER_INTERNAL"
    DRIVER_NOT_INSTALLED = "DRIVER_NOT_INSTALLED"
    BITNESS_MISMATCH = "BITNESS_MISMATCH"
    DRIVER_NOT_CAPABLE = "DRIVER_NOT_CAPABLE"
    AUTH_FAILED = "AUTH_FAILED"
    COMPANY_NOT_FOUND = "COMPANY_NOT_FOUND"
    COMPANY_REQUIRED = "COMPANY_REQUIRED"
    MODULE_NOT_INSTALLED = "MODULE_NOT_INSTALLED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    CS_ODBC_UNREACHABLE = "CS_ODBC_UNREACHABLE"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    PUSHDOWN_LIMIT = "PUSHDOWN_LIMIT"
    RATE_LIMITED = "RATE_LIMITED"
    CATALOG_BUILDING = "CATALOG_BUILDING"
    INTERNAL = "INTERNAL"
    # Not in SPEC.md §14.2 (docs/decisions.md D15): the SQL Server backend cannot be reached.
    BACKEND_UNREACHABLE = "BACKEND_UNREACHABLE"
    # Not in SPEC.md §14.2 (docs/decisions.md D53): a run_query cursor that is expired, altered, or
    # issued to another caller, company or query.
    CURSOR_INVALID = "CURSOR_INVALID"


class WarningCode(StrEnum):
    RESULT_TRUNCATED = "RESULT_TRUNCATED"
    COLUMNS_MASKED = "COLUMNS_MASKED"
    NULL_DATES_COERCED = "NULL_DATES_COERCED"
    ORDER_APPLIED_LOCALLY = "ORDER_APPLIED_LOCALLY"
    HYBRID_MODE = "HYBRID_MODE"
    TB_OUT_OF_BALANCE = "TB_OUT_OF_BALANCE"
    DIVISION_DISABLED = "DIVISION_DISABLED"
    AGING_DEFAULTS_USED = "AGING_DEFAULTS_USED"
    # Not in SPEC.md §14.2 (docs/decisions.md D21): SELECT * was narrowed to core columns (§13.1),
    # a requested limit was lowered to the server's cap, and mode='hybrid' does not apply on Premium.
    COLUMNS_PRUNED = "COLUMNS_PRUNED"
    LIMIT_CAPPED = "LIMIT_CAPPED"
    MODE_IGNORED = "MODE_IGNORED"
    # Not in SPEC.md §14.2 (docs/decisions.md D46): compare_companies got differently named columns
    # (each company's own aging categories) and shows them by position.
    COLUMNS_DIFFER = "COLUMNS_DIFFER"


# Codes the model may usefully retry later. Retries the server makes itself (§14.2) happen
# before the model sees the error, so those codes are not retryable from the model's side.
_RETRYABLE = {ErrorCode.LOCK_TIMEOUT, ErrorCode.RATE_LIMITED, ErrorCode.CATALOG_BUILDING}


class ErrorEnvelope(BaseModel):
    """``structuredContent`` of a result with ``isError: true``."""

    code: ErrorCode
    message: str
    hint: str | None = None
    detail: str | None = None  # raw driver text, secrets and literals redacted
    company: str | None = None
    choices: list[str] | None = None
    retryable: bool = False
    retry_after_s: int | None = None  # RATE_LIMITED: seconds until a call is admitted again


class SageError(Exception):
    """A failure the model can act on; tools turn it into an error result, never a protocol error."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        detail: str | None = None,
        company: str | None = None,
        choices: list[str] | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail
        self.company = company
        self.choices = choices
        self.retry_after_s = retry_after_s

    def envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(
            code=self.code,
            message=self.message,
            hint=self.hint,
            detail=self.detail,
            company=self.company,
            choices=self.choices,
            retryable=self.code in _RETRYABLE,
            retry_after_s=self.retry_after_s,
        )
