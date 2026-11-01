"""SQL guard, layer 3 of SPEC.md §9: only one read-only SELECT reaches a backend.

The guard parses with sqlglot and inspects the tree, never the text, with one exception the
spec requires: ODBC procedure escapes (``{call ...}``) are refused on the raw text, before
parsing. Anything that does not parse is refused. Callers send SQL regenerated from the checked
tree (`to_sql`), so whatever the parser skipped, such as comments, never reaches a driver.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect as SqlglotDialect
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokenizer_core import TokenType

from sage100_mcp.errors import ErrorCode, SageError

# sqlglot logs unsupported statements in full at WARNING; their literals must not reach our logs (§9.4).
logging.getLogger("sqlglot").setLevel(logging.ERROR)

Dialect = Literal["ansi", "tsql"]
_READERS: dict[Dialect, str | None] = {"ansi": None, "tsql": "tsql"}

MAX_SQL_CHARS = 100_000
_PROC_ESCAPE = re.compile(r"\{\s*(?:\?\s*=\s*)?call\b", re.IGNORECASE)
# Words that start a non-SELECT statement. Used only to pick a clearer code for a statement that has
# already failed to parse; the rejection itself does not depend on them.
_STATEMENT_WORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "REPLACE", "CREATE", "DROP", "ALTER", "TRUNCATE",
    "RENAME", "GRANT", "REVOKE", "DENY", "EXEC", "EXECUTE", "CALL", "BULK", "BACKUP", "RESTORE", "WAITFOR",
    "SHUTDOWN", "RECONFIGURE", "KILL", "DBCC", "SET", "USE", "DECLARE", "BEGIN", "COMMIT", "ROLLBACK",
    "SAVE", "OPEN", "FETCH", "CLOSE", "DEALLOCATE", "WRITETEXT", "UPDATETEXT", "READTEXT", "CHECKPOINT",
    "LOAD", "COPY", "ATTACH", "DETACH", "INSTALL", "PRAGMA", "VACUUM", "ANALYZE", "LOCK", "UNLOCK",
    "ENABLE", "DISABLE",
}  # fmt: skip

# Node kinds that write, change session or server state, or take locks, wherever they appear.
_WRITES: tuple[type[exp.Expr], ...] = (
    exp.DDL, exp.DML, exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.TruncateTable, exp.Grant, exp.Revoke, exp.Command, exp.Execute, exp.Set, exp.Use, exp.Declare,
    exp.Transaction, exp.Commit, exp.Rollback, exp.Into, exp.Lock, exp.Kill, exp.Pragma,
    exp.NextValueFor,  # advances a sequence: a write hidden inside a SELECT
)  # fmt: skip
_HINTS: tuple[type[exp.Expr], ...] = (exp.WithTableHint, exp.QueryOption)

_FORBIDDEN_FUNCTION = re.compile(r"^(?:OPEN(?:ROWSET|QUERY|DATASOURCE|XML)$|XP_|SP_|FN_|DBCC)", re.IGNORECASE)
_FILE_OR_NETWORK = re.compile(
    r"^(?:READ_|SCAN_|GLOB$|LIST_FILES|HTTP|S3_|LOAD|INSTALL|ATTACH|DETACH|COPY|PRAGMA)", re.IGNORECASE
)
_DENIED_TYPED: tuple[type[exp.Expr], ...] = (exp.ReadCSV, exp.ReadParquet, exp.ObjectId, exp.CurrentUser)
# Built-ins sqlglot does not model as typed functions; anything else unknown is refused.
_ANONYMOUS_ALLOWED = frozenset({
    "PATINDEX", "QUOTENAME", "DATENAME", "DATEFROMPARTS", "DATETIMEFROMPARTS", "DATETIME2FROMPARTS",
    "SMALLDATETIMEFROMPARTS", "TIMEFROMPARTS", "DATEDIFF_BIG", "SYSDATETIME", "SYSUTCDATETIME", "GETUTCDATE",
    "EOMONTH", "CHOOSE", "SOUNDEX", "DIFFERENCE", "REPLICATE", "REVERSE", "SPACE", "STR", "TRANSLATE",
    "UNICODE", "NCHAR", "CHAR", "ASCII", "ISNUMERIC", "ISDATE", "SQUARE", "SIGN", "PI", "LOG10", "CHECKSUM",
    "BINARY_CHECKSUM", "COUNT_BIG", "STDEV", "STDEVP", "VAR", "VARP", "GROUPING", "GROUPING_ID", "NTILE",
    "CUME_DIST", "PERCENT_RANK", "PERCENTILE_CONT", "PERCENTILE_DISC", "TRY_PARSE", "PARSE", "CONCAT_WS",
    "STRING_ESCAPE", "LTRIM", "RTRIM", "TRIM", "FORMAT", "ISNULL", "IIF", "NULLIF", "LEN", "DATALENGTH",
    "CHARINDEX", "DATEADD", "DATEDIFF", "DATEPART", "GETDATE", "TRY_CAST", "TRY_CONVERT", "ROUND", "CEILING",
    "FLOOR", "ABS", "POWER", "SQRT", "EXP", "LOG", "RAND", "NEWID",
})  # fmt: skip


@dataclass(frozen=True)
class CheckedQuery:
    """A statement the guard accepted: one SELECT, or a set operation of SELECTs."""

    tree: exp.Query
    dialect: Dialect  # the reader that accepted it
    placeholders: int  # number of ? markers, bound positionally from params
    cte_names: frozenset[str]  # lower-cased; references to these are not catalog tables

    def tables(self) -> list[exp.Table]:
        """Table references that must exist in the company catalog (CTE names excluded)."""
        return [t for t in self.tree.find_all(exp.Table) if t.name and t.name.lower() not in self.cte_names]


def to_sql(tree: exp.Expr, dialect: str = "tsql") -> str:
    """Regenerate SQL from a checked tree, without comments."""
    return tree.sql(dialect=dialect, comments=False)


def _reject(code: ErrorCode, message: str, hint: str | None = None, detail: str | None = None) -> SageError:
    return SageError(code, message, hint=hint, detail=detail)


def _parse(sql: str, dialect: Dialect) -> tuple[list[exp.Expr], Dialect]:
    """Parse with the requested reader, falling back to the other one if it fails."""
    order: list[Dialect] = [dialect, "ansi" if dialect == "tsql" else "tsql"]
    first_error: Exception | None = None
    for reader in order:
        try:
            statements = sqlglot.parse(sql, read=_READERS[reader])
        except (ParseError, TokenError, RecursionError, ValueError) as exc:
            first_error = first_error or exc
            continue
        return [s for s in statements if s is not None], reader
    raise _parse_failure(sql, first_error)


def _first_words(sql: str) -> list[str]:
    """The first word of each ;-separated statement, by sqlglot's tokenizer (comments skipped)."""
    try:
        tokens = SqlglotDialect.get_or_raise("tsql").tokenize(sql)
    except (TokenError, ValueError, RecursionError):
        return []
    words: list[str] = []
    expecting = True
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            expecting = True
        elif expecting:
            words.append(token.text.upper())
            expecting = False
    return words


def _parse_failure(sql: str, error: Exception | None) -> SageError:
    words = _first_words(sql)
    if len(words) > 1:
        return _reject(
            ErrorCode.MULTI_STATEMENT,
            f"Only one statement is allowed; found {len(words)}.",
            hint="Send each SELECT in its own call.",
        )
    if words and words[0] in _STATEMENT_WORDS:
        return _reject(
            ErrorCode.WRITE_BLOCKED,
            f"Only SELECT statements are allowed; this one starts with {words[0]}.",
            hint="This server is read-only. Ask for the data with a single SELECT.",
        )
    where = ""
    if isinstance(error, ParseError) and error.errors:
        first = error.errors[0]
        where = f" near line {first.get('line')}, column {first.get('col')}: {first.get('description')}"
    elif isinstance(error, RecursionError):
        where = ": the statement is nested too deeply"
    return _reject(
        ErrorCode.PARSE_ERROR,
        f"The SQL could not be parsed{where}.",
        hint="Send one plain SELECT. T-SQL and ANSI syntax are both accepted on Premium.",
    )


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name)
    return str(node.sql_name())


def _check_node(node: exp.Expr) -> None:
    if isinstance(node, _WRITES):
        what = "SELECT ... INTO" if isinstance(node, exp.Into) else node.key.upper().replace("_", " ")
        raise _reject(ErrorCode.WRITE_BLOCKED, f"{what} is not allowed: this server only reads.")
    if isinstance(node, _HINTS):
        raise _reject(
            ErrorCode.WRITE_BLOCKED,
            "Table and query hints are not allowed.",
            hint="Remove WITH (...) and OPTION (...); the server already reads without blocking Sage users.",
        )
    if isinstance(node, exp.Func):
        name = _function_name(node)
        if _FORBIDDEN_FUNCTION.match(name) or _FILE_OR_NETWORK.match(name) or isinstance(node, _DENIED_TYPED):
            raise _reject(ErrorCode.FORBIDDEN_FUNCTION, f"The function {name} is not allowed.")
        if isinstance(node, exp.Anonymous) and name.upper() not in _ANONYMOUS_ALLOWED:
            raise _reject(
                ErrorCode.FORBIDDEN_FUNCTION,
                f"The function {name} is not allowed.",
                hint="Use built-in SQL functions only; user-defined and system functions are refused.",
            )
        if isinstance(node.parent, exp.Dot):
            raise _reject(ErrorCode.FORBIDDEN_FUNCTION, f"Schema-qualified function {name} is not allowed.")
    if isinstance(node, (exp.Parameter, exp.SessionParameter)):
        raise _reject(
            ErrorCode.FORBIDDEN_FUNCTION,
            "Variables such as @name and @@name are not allowed.",
            hint="Put values in the SQL as ? markers and pass them in params.",
        )
    if isinstance(node, exp.Placeholder) and node.this:
        raise _reject(ErrorCode.PARSE_ERROR, f"Named parameter :{node.this} is not supported.", hint="Use ? markers.")
    if isinstance(node, exp.Table):
        _check_table(node)


def _check_table(table: exp.Table) -> None:
    name = table.name
    if table.args.get("catalog") is not None and table.catalog:
        raise _reject(
            ErrorCode.TABLE_DENIED,
            f"{table.sql(dialect='tsql')} names another database or server.",
            hint="One statement reads one company. Pass company instead, or use compare_companies.",
        )
    if table.db and table.db.lower() != "dbo":
        raise _reject(
            ErrorCode.TABLE_DENIED,
            f"Only Sage tables can be read, not {table.db}.{name}.",
            hint="Write the bare table name, such as AR_Customer.",
        )
    identifier = table.this
    flags = identifier.args if isinstance(identifier, exp.Identifier) else {}
    if flags.get("temporary") or flags.get("global_") or name.startswith("#"):  # #local and ##global
        raise _reject(ErrorCode.TABLE_DENIED, f"Temporary table {table.sql(dialect='tsql')} is not allowed.")


def check(sql: str, *, dialect: Dialect = "tsql") -> CheckedQuery:
    """Accept one read-only SELECT (or UNION/INTERSECT/EXCEPT of SELECTs), or raise SageError."""
    if not isinstance(sql, str) or not sql.strip():
        raise _reject(ErrorCode.PARSE_ERROR, "The SQL is empty.", hint="Send one SELECT statement.")
    if len(sql) > MAX_SQL_CHARS:
        raise _reject(ErrorCode.PARSE_ERROR, f"The SQL is longer than {MAX_SQL_CHARS:,} characters.")
    if _PROC_ESCAPE.search(sql):
        raise _reject(ErrorCode.WRITE_BLOCKED, "ODBC procedure calls ({call ...}) are not allowed.")

    statements, reader = _parse(sql, dialect)
    if not statements:
        raise _reject(ErrorCode.PARSE_ERROR, "The SQL has no statement.", hint="Send one SELECT statement.")
    if len(statements) > 1:
        raise _reject(
            ErrorCode.MULTI_STATEMENT,
            f"Only one statement is allowed; found {len(statements)}.",
            hint="Send each SELECT in its own call.",
        )
    root = statements[0]
    while isinstance(root, exp.Subquery) and isinstance(root.this, exp.Query) and not root.alias:
        root = root.this  # a parenthesised SELECT
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise _reject(ErrorCode.WRITE_BLOCKED, "Only SELECT statements are allowed.", hint="This server is read-only.")

    placeholders = 0
    for node in root.walk():
        _check_node(node)
        if isinstance(node, exp.Placeholder):
            placeholders += 1
    _read_back(sql, root)
    cte_names = frozenset(cte.alias.lower() for cte in root.find_all(exp.CTE) if cte.alias)
    return CheckedQuery(tree=root, dialect=reader, placeholders=placeholders, cte_names=cte_names)


def _read_back(sql: str, root: exp.Expr) -> None:
    """What is sent must read back as T-SQL as one statement of the kind that was checked (D58).

    The ANSI fallback reads some text that T-SQL refuses: in ``SELECT TOP FROM t``, TOP is a column to it.
    Regenerated as T-SQL, that is not one SELECT any more, so it is refused here like any unreadable SQL.
    """
    error: Exception | None = None
    try:
        again = [s for s in sqlglot.parse(to_sql(root), read=_READERS["tsql"]) if s is not None]
    except (ParseError, TokenError, RecursionError, ValueError) as exc:
        error = exc
    else:
        if len(again) == 1 and type(again[0]) is type(root):
            return
    raise _parse_failure(sql, error)
