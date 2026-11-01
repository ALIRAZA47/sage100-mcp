# Contributing to sage100-mcp

Thanks for helping. sage100-mcp is a read-only MCP server for US Sage 100, and every change is weighed
against that promise first. [SPEC.md](SPEC.md) is the source of truth. [docs/decisions.md](docs/decisions.md)
records where the build settled what the spec left open, or departed from it.

## Before you start

- **A bug, or something you'd like it to do.** Open an issue, using its form.
- **A security problem.** Never open a public issue: see [SECURITY.md](SECURITY.md).
- **A large change.** Open an issue first, so we can agree on the approach before you write it.
- **License.** The repository has no license yet; the maintainer will choose one. Until then, please talk
  to the maintainer in an issue before starting work you want merged.

## Set up

```bash
uv sync
uv run pytest -m "not windows"                       # unit, ProvideX emulator, MCP contract: any OS
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests scripts
```

Other test runs:
- **Premium.** These tests need a SQL Server the tests may rebuild databases on. Set
  `SAGE100_MCP_TEST_SQLSERVER` and `SAGE100_MCP_TEST_SA_PASSWORD`, then run `uv run pytest -m premium`.
  The README shows a Docker one-liner for the server.
- **Sage's real ProvideX driver.** These run on a Windows host with Sage installed: `uv run pytest -m windows`.
- **The log scan.** [CLAUDE.md](CLAUDE.md), under "Commands", has the command. It must find nothing in the
  test logs.

## The rules every change keeps

These come from [CLAUDE.md](CLAUDE.md), which also guides Claude Code when it works on this repository.

- **No write path, ever.** No BOI, no VI, no INSERT, UPDATE or DELETE, no `{call}`. Before `sql/guard.py`
  is relaxed, cases it must still reject go into `tests/fixtures/guard/reject.yaml`, in the same change.
- **No real company data in fixtures.** Schemas from MAS_EMP are fine. Rows must be synthetic.
- **Every new tool** has `readOnlyHint=true`, an `outputSchema`, a contract test and error-code tests.
- **Money and quantities** are `Decimal` end to end, and serialised as strings.
- **A new view** has `views/<name>.yaml`, a ProvideX and a T-SQL plan, a reconciliation test and a parity
  test.
- **A driver feature** has a probe case, a capability fixture and a planner test.
- **The 32-bit driver helper** (`src/sage100_mcp/bridge/helper.py`) runs under a bare 32-bit Python. It
  imports only the standard library and pyodbc, in Python 3.9 syntax, and tests check both.
- **After changing `policy.yaml`**, regenerate `scripts/premium_readonly.sql` with
  `scripts/premium_readonly.py`. A test checks they match.
- **Decisions the spec leaves open, and deviations from it**, get an entry in `docs/decisions.md`.
- **Tests come first.** Write emulator tests first, then the code.

## Pull requests

- Branch from `main` and open a pull request against it. The pull request template asks what changed,
  why, and how it was tested.
- `main` takes no direct pushes. A pull request merges once the maintainer, who owns every file
  ([CODEOWNERS](.github/CODEOWNERS)), approves its latest push. A later push dismisses the approval.
- Pull requests from forks run CI only once the maintainer approves the run. They never run on the
  self-hosted runner that reaches Sage data.
- Keep a pull request to one change. Format with `uv run ruff format .`, and keep each commit message's
  first line short.

## Releases

Only the maintainer creates `v*` tags. A tag starts `.github/workflows/release.yml`. Its signing and
publishing steps wait for the maintainer's approval in the `release`, `pypi` and `registry` environments
(docs/decisions.md D73, D75).
