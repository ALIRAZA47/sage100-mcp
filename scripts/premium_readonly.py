"""Write scripts/premium_readonly.sql from a policy file (SPEC.md §16.4, layer 1 of §9).

The SQL creates a login with db_datareader in each company database in scope, then denies SELECT
on every table the policy hides, so the database itself refuses what the server hides. Masked
columns are not denied: a column-level DENY makes SQL Server refuse even SELECT COUNT(*) on the
table, so masking stays with the server (layer 4), which never fetches a masked value.

    uv run python scripts/premium_readonly.py                # rewrite scripts/premium_readonly.sql
    uv run python scripts/premium_readonly.py --check        # exit 1 if the file is out of date
    uv run python scripts/premium_readonly.py --policy my.yaml --output my_readonly.sql
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sage100_mcp.policy import Policy, load_policy

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "scripts" / "premium_readonly.sql"


def like(pattern: str) -> str:
    """An fnmatch pattern as a T-SQL LIKE pattern: * and ? become % and _, and LIKE's own syntax is escaped."""
    out = []
    for ch in pattern.lower():
        out.append({"*": "%", "?": "_", "_": "[_]", "%": "[%]", "[": "[[]", "'": "''"}.get(ch, ch))
    return "".join(out)


def _table_condition(policy: Policy) -> str:
    rules = [rule for rule in policy.deny_tables if not (rule.module and rule.module.upper() in policy.allow_modules)]
    if not rules:
        return "1 = 0"
    return " OR ".join(f"LOWER(t.name) LIKE N'{like(rule.pattern)}'" for rule in rules)


def render(policy: Policy, source: str) -> str:
    per_company = f"""USE {{db}};
DECLARE @login sysname = N'{{login}}', @deny nvarchar(max) = N'', @statement nvarchar(max);
IF USER_ID(@login) IS NULL
BEGIN
    SET @statement = N'CREATE USER ' + QUOTENAME(@login) + N' FOR LOGIN ' + QUOTENAME(@login);
    EXEC sys.sp_executesql @statement;
END;
SET @statement = N'ALTER ROLE db_datareader ADD MEMBER ' + QUOTENAME(@login);
EXEC sys.sp_executesql @statement;
SELECT @deny += N'DENY SELECT ON dbo.' + QUOTENAME(t.name) + N' TO ' + QUOTENAME(@login) + N';'
FROM sys.tables AS t
WHERE SCHEMA_NAME(t.schema_id) = N'dbo' AND ({_table_condition(policy)});
EXEC sys.sp_executesql @deny;"""
    per_company_sql = per_company.replace("'", "''")
    return f"""-- sage100-mcp: a read-only SQL login for Sage 100 Premium (SPEC.md §16.4, layer 1 of §9).
-- Generated from {source} by scripts/premium_readonly.py; regenerate it rather than editing it.
--
-- Run as a DBA with sqlcmd, naming the login, its password and the company codes in scope:
--   sqlcmd -S SQLSRV\\SAGE -E -b -i premium_readonly.sql
--          -v Login="sage100_mcp" Password="<generated>" Companies="ABC,EMP"
-- The password must not contain a single quote. Run it again after adding a company or changing the policy:
-- it creates what is missing and re-applies the denies. It never revokes a DENY the policy no longer asks for.
--
-- What it does, per company database MAS_<code>: a user for the login in db_datareader, then DENY SELECT on
-- every table the policy hides. In MAS_SYSTEM: SELECT on SY_Company only, which the server reads to name
-- companies. The login gets no other database and no write permission. Masked columns are left to the
-- server: a column-level DENY would make SQL Server refuse even SELECT COUNT(*) on those tables.

SET NOCOUNT ON;
SET XACT_ABORT ON;
USE master;

DECLARE @login sysname = N'$(Login)', @password nvarchar(128) = N'$(Password)';
DECLARE @companies nvarchar(max) = N'$(Companies)';
IF @login = N'' OR @password = N'' OR @companies = N''
    THROW 50000, N'Set the sqlcmd variables Login, Password and Companies.', 1;
DECLARE @db sysname, @sql nvarchar(max);
IF SUSER_ID(@login) IS NULL
BEGIN
    SET @sql = N'CREATE LOGIN ' + QUOTENAME(@login) + N' WITH PASSWORD = ' + QUOTENAME(@password, N'''')
        + N', CHECK_POLICY = ON, DEFAULT_DATABASE = [MAS_SYSTEM]';
    EXEC sys.sp_executesql @sql;
END;
DECLARE databases CURSOR LOCAL FAST_FORWARD FOR
    SELECT N'MAS_' + UPPER(LTRIM(RTRIM(value))) FROM STRING_SPLIT(@companies, N',') WHERE LTRIM(RTRIM(value)) <> N'';
OPEN databases;
FETCH NEXT FROM databases INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF DB_ID(@db) IS NULL
    BEGIN
        DECLARE @missing nvarchar(200) = N'No database ' + @db + N' on this server.';
        THROW 50001, @missing, 1;
    END;
    SET @sql = REPLACE(N'{per_company_sql}', N'{{db}}', QUOTENAME(@db));
    SET @sql = REPLACE(@sql, N'{{login}}', REPLACE(@login, N'''', N''''''));
    EXEC sys.sp_executesql @sql;
    FETCH NEXT FROM databases INTO @db;
END;
CLOSE databases;
DEALLOCATE databases;

SET @sql = N'USE [MAS_SYSTEM];
DECLARE @login sysname = N''' + REPLACE(@login, N'''', N'''''') + N''', @statement nvarchar(max);
IF USER_ID(@login) IS NULL
BEGIN
    SET @statement = N''CREATE USER '' + QUOTENAME(@login) + N'' FOR LOGIN '' + QUOTENAME(@login);
    EXEC sys.sp_executesql @statement;
END;
SET @statement = N''GRANT SELECT ON dbo.SY_Company TO '' + QUOTENAME(@login);
EXEC sys.sp_executesql @statement;';
EXEC sys.sp_executesql @sql;
PRINT N'Read-only login ' + @login + N' is ready.';
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy", type=Path, help="policy file (default: the built-in policy)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail if the output file is out of date")
    args = parser.parse_args(argv)
    source = str(args.policy) if args.policy else "src/sage100_mcp/policy.yaml"
    text = render(load_policy(args.policy), source)
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print(f"{args.output} is out of date; run scripts/premium_readonly.py", file=sys.stderr)
            return 1
        return 0
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
