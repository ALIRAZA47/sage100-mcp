## What this changes

<!-- One or two sentences: the behaviour before and after. Link the issue it closes, if any. -->

## Why

<!-- The problem, and why this is the fix. Cite SPEC.md sections or docs/decisions.md entries where they apply. -->

## How it was tested

<!-- The commands you ran and what they showed. Delete the ones that don't apply. -->

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests scripts`
- [ ] `uv run pytest -m "not windows"` (unit, emulator, contract)
- [ ] `uv run pytest -m premium` against a SQL Server test bed, for Premium or view changes
- [ ] `uv run pytest -m windows` on a Windows host with Sage, for ProvideX driver changes
- [ ] The log scan, for anything that logs or handles errors (CLAUDE.md, "Commands")

## The project's rules (CLAUDE.md)

- [ ] No write path: no BOI, no VI, no INSERT/UPDATE/DELETE, no `{call}`.
- [ ] `sql/guard.py` is only relaxed after new rejecting cases went into `tests/fixtures/guard/reject.yaml`.
- [ ] Fixtures hold no real company data: schemas may come from MAS_EMP, rows must be synthetic.
- [ ] A new tool has `readOnlyHint=true`, an `outputSchema`, a contract test and error-code tests.
- [ ] Money and quantities stay `Decimal` end to end, serialised as strings.
- [ ] A new view has `views/<name>.yaml`, ProvideX and T-SQL plans, a reconciliation test and a parity test.
- [ ] A driver feature has a probe case, a capability fixture and a planner test.
- [ ] `src/sage100_mcp/bridge/helper.py` still needs only the standard library and pyodbc.
- [ ] After a `policy.yaml` change, `scripts/premium_readonly.sql` was regenerated.
- [ ] Decisions the spec leaves open, and deviations from it, are in `docs/decisions.md`.

## Anything a reviewer should look at first
