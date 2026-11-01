# sage100-mcp
Read-only MCP server for US Sage 100 (ProvideX ODBC + Premium SQL Server). SPEC.md is the source of truth.

## Non-negotiables
- Never add a write path. No BOI, no VI, no INSERT/UPDATE/DELETE, no {call}. Never relax sql/guard.py without adding rejecting tests first.
- Never put real company data in fixtures. Schemas from MAS_EMP are fine; rows must be synthetic.
- Every new tool: readOnlyHint=true, outputSchema, contract test, error-code tests.
- Money/quantities are Decimal end to end; serialise as strings.
- src/sage100_mcp/bridge/helper.py runs under a bare 32-bit Python: standard library and pyodbc only, Python 3.9 syntax (tests check both).

## Commands
- uv sync
- uv run pytest -m "not windows"            # unit, emulator, contract (any OS)
- uv run pytest -m windows                   # real driver; Windows runner only
- uv run pytest -m premium                   # real SQL Server test bed; needs SAGE100_MCP_TEST_SQLSERVER and _SA_PASSWORD
- uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests scripts
- uv run sage100-mcp doctor
- uv run pytest -m "not windows" --log-file=build/pytest.log --log-file-level=DEBUG --basetemp=build/tmp && uv run python scripts/log_scan.py build/pytest.log build/tmp --testbed   # §15.8 log scan
- uv run python scripts/clean_install.py      # M6: wheel to first answer, timed, on the Premium test bed
- uv run --with pyinstaller==6.22.3 pyinstaller packaging/sage100-mcp.spec --noconfirm --distpath build/dist --workpath build/pyinstaller   # the one-folder build (packaging/odbc32.spec: the 32-bit helper)

## ProvideX rules (the driver will reject otherwise)
- Joins only inside {oj ...} or { IJ ... }; one table per {oj}; no bare JOIN.
- No subqueries, no CASE, no COUNT(DISTINCT); functions only as {fn ...}.
- Dates as {d 'YYYY-MM-DD'}; no trailing semicolon; no [brackets].
- ORDER BY / OR / TOP / string functions: only if tests/capabilities/<ver>.json says pass; else planner goes hybrid.
- Never send SELECT without FROM (health check reads one row from a small table).

## Sage data rules
- Customer key = ARDivisionNo + CustomerNo; vendor key = APDivisionNo + VendorNo.
- AR_OpenInvoice keeps paid rows: always Balance <> 0 for open AR.
- Aging buckets come from AR_Options (AgeInvoicesBy, AgingCategory1-4); never hard-code 30/60/90.
- SO_SalesOrderHeader includes quotes (OrderType Q): exclude Q/M/R from backlog.
- PO_PurchaseOrderHeader keeps completed POs (OrderStatus X): exclude from open POs.
- UDF_* and D###_/AVA234_ columns are site-specific: never reference them in views.

## Workflow
- One milestone per session (SPEC.md section 18). Write emulator tests first, then code.
- Adding a view: views/<name>.yaml + ProvideX and T-SQL plans + reconciliation test + parity test.
- Adding a driver feature: probe case + capability fixture + planner test.
- Record decisions the spec leaves open, and deviations from it, in docs/decisions.md (read it before starting a milestone).
- After changing policy.yaml, regenerate scripts/premium_readonly.sql with scripts/premium_readonly.py (a test checks it).
