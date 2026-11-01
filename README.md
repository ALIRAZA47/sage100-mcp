# sage100-mcp

<!-- mcp-name: com.outova/sage100-mcp -->

A read-only, Sage-aware [MCP](https://modelcontextprotocol.io) server for **US Sage 100**
(Standard and Advanced through the ProvideX ODBC driver, Premium through SQL Server).
It lets Claude query AR, AP, sales orders, purchase orders, inventory and GL by company
without knowing ProvideX ODBC quirks, table names or company codes.

> Not affiliated with or endorsed by Sage. "Sage 100" names the product this server reads.

**Status: milestone M6 (packaging), release candidate 1.0.0rc1.** Sage 100 Standard and Advanced
(ProvideX) and Premium (SQL Server) companies can be explored, queried and reported on:

- **Canned views.** Eleven views apply each company's own Sage settings: aging, open orders,
  availability, the trial balance, sales history, lookups, and any of these across companies.
- **Remote access.** Mac and Linux clients connect over Streamable HTTP with TLS and a bearer token or
  OAuth access token.
- **Hardening.**
  - `sage100-mcp init` sets a site up and `doctor` checks it.
  - A soak test drives 20 concurrent tool calls.
  - A log scan proves no password, token or masked value reaches a log.
  - An eval set of 40 questions scores Claude's answers through the tools.
- **Packaging.**
  - A Windows MSI holds the server and a 32-bit driver helper. The helper serves sites whose Sage installs
    only the 32-bit ODBC driver.
  - `sage100-mcp service` runs the server as a Windows service.
  - Release builds can be code-signed.
  - `server.json` is the server's MCP registry entry.
  - [docs/setup.md](docs/setup.md) goes from a clean machine to a first answer.

Some exit gates are still open, because they need a Windows test machine with Sage installed and the ABC
sample company:

- M2's ProvideX gate;
- M3's reconciliation to Sage's printed reports;
- M4's eval run from a Mac;
- M5's two-hour soak with a Sage user at work, and the eval set's ABC answers;
- M6's clean-VM install, timed on Windows, and the first MSI and service runs on a real Windows host.

See [SPEC.md §18](SPEC.md#18-claude-code-build-plan) for the plan and [docs/decisions.md](docs/decisions.md)
for the decisions taken along the way.

## Set up

[docs/setup.md](docs/setup.md) is the step-by-step guide: install the MSI or the package, run `init`, then
`doctor`, then connect Claude. In short, `sage100-mcp init` does the setup interactively:

- **ProvideX:** it reads Sage's SOTAMAS90 DSN as a template.
- **Premium:** it asks for the SQL Server.

It tests the login before storing it in the keyring, lists the companies, and writes `config.yaml`. With
`--yes` and flags (`--type`, `--directory` or `--server`, `--user`, `--password-env`, `--company`) it asks
nothing. The sections below do the same by hand.

```bash
uv run sage100-mcp init
uv run sage100-mcp doctor
```

## Set up Sage 100 Standard or Advanced (ProvideX)

1. On the Windows machine that runs the server, install Sage 100 Workstation Setup from the Sage server's
   share. It installs the `MAS 90 4.0 ODBC Driver`. The 64-bit server loads Sage's 64-bit build itself.
   Where only the 32-bit build is installed, the server loads it through its 32-bit helper
   (`driver_bitness` in [`docs/config.example.yaml`](docs/config.example.yaml); decisions D67).
2. In Sage, create a user for the server (for example `mcp_reader`) whose role grants only the tables it
   should read; with ODBC Security on, that is the role's ODBC Security tab.
3. Add a `providex` backend to `config.yaml` with the `MAS90` folder as `directory` (a UNC path when the
   server runs as a service) and a `companies.default`, then store the Sage login:

   ```bash
   uv run keyring set "sage100-mcp/sage_prod" mcp_reader
   ```

4. Check it, and print what this driver build accepts:

   ```bash
   uv run sage100-mcp doctor
   uv run sage100-mcp probe
   ```

The first connection probes the driver (about 100 one-row checks, a few seconds) and caches the result
per driver version. `explain_query` shows what a query would send to the driver and what runs locally.

## Set up Sage 100 Premium

1. As a DBA, create a read-only login with [`scripts/premium_readonly.sql`](scripts/premium_readonly.sql).
   It gives the login `db_datareader` in each company database you name, denies the tables the policy
   hides (payroll, card data, Sage users), and lets it read only `SY_Company` in `MAS_SYSTEM`:

   ```bash
   sqlcmd -S SQLSRV\SAGE -E -b -i scripts/premium_readonly.sql -v Login="sage100_mcp" Password="<generated>" Companies="ABC,EMP"
   ```

2. Add a `sqlserver` backend to `config.yaml` (see [`docs/config.example.yaml`](docs/config.example.yaml))
   and store the login under the backend's `credential` name:

   ```bash
   uv run keyring set "sage100-mcp/sage_premium" sage100_mcp
   ```

   Where no keyring exists (containers), set `SAGE100_MCP_CRED_SAGE_PREMIUM_USER` and
   `SAGE100_MCP_CRED_SAGE_PREMIUM_PASSWORD` instead.

3. Check it, then build the catalogs (the server also builds them in the background on first start):

   ```bash
   uv run sage100-mcp doctor
   uv run sage100-mcp catalog build
   ```

`scripts/verify_premium.py --company EMP` runs the M1 exit gates against a real company: catalog build
time, and `run_query` results compared with plain T-SQL. `scripts/verify_views.py --company EMP` checks
the canned views against the figures Sage keeps itself: customer aging and open order amounts, vendor
balances, item totals, and the trial balance. It works on ProvideX companies too. Both read counts and codes only.

## Run

```bash
uv run sage100-mcp stdio      # MCP over stdio
uv run sage100-mcp doctor     # config, bitness, ODBC drivers, Sage share and login, database access
uv run sage100-mcp probe      # what the ProvideX driver accepts, check by check
```

Claude Code on the same machine:

```bash
claude mcp add sage100 -- uv --directory /path/to/sage100-mcp run sage100-mcp stdio
```

## Serve over HTTP

For Mac and Linux clients, run the server next to Sage with TLS and a token per person or machine. See
[docs/remote.md](docs/remote.md) for the proxy and OAuth setups.

```bash
uv run sage100-mcp token create --principal ann-macbook    # prints the token once; only its hash is kept
uv run sage100-mcp serve                                   # Streamable HTTP on http.bind
```

On Windows, `sage100-mcp service install --account CORP\svc-sage100mcp --password-env SVC_PW` runs `serve`
as a service under that account ([docs/setup.md](docs/setup.md), step 5).

On the Mac, check the connection, then add the server to Claude Code:

```bash
SAGE100_MCP_TOKEN='s1mcp_...' uv run python scripts/remote_check.py https://sage-mcp.corp.example/mcp
claude mcp add --transport http sage100 https://sage-mcp.corp.example/mcp --header "Authorization: Bearer s1mcp_..."
```

Configuration lives in `%ProgramData%\sage100-mcp\config.yaml` on Windows
(`~/.local/share/sage100-mcp/config.yaml` elsewhere; override with `--config` or `SAGE100_MCP_CONFIG`).
Without a file the server starts with no backends and `sage_status` explains what to set up.

## Canned views

For common questions, ask for a view rather than writing SQL. Each one reads the company's own settings
(aging categories and basis, divisions, what counts as available) and returns typed rows with totals.

| Tool | Answers |
| --- | --- |
| `ar_aging`, `ap_aging` | Aging as of a date, per customer/vendor or per invoice, in the company's own categories (by day or by month) |
| `open_sales_orders` | The backlog: open lines or orders, quantity still to ship and its value |
| `open_purchase_orders` | Open PO lines or orders, quantity still to receive and its value |
| `inventory_availability` | On hand, committed, on order, available and projected per item and warehouse, and what is short |
| `gl_trial_balance` | One period's trial balance, with a warning when debits and credits differ |
| `sales_history` | Revenue, cost and margin between two dates, by customer, item, salesperson, product line or month |
| `customer_lookup`, `vendor_lookup`, `item_lookup` | Find a record by part of its number, name, email or phone |
| `compare_companies` | Any of these for several companies at once |

The `sage://views` resource shows each view's parameters and the SQL it sends. Four prompts script common
reviews: `ar_collections_review`, `order_backlog_review`, `month_end_checks` and `explore_module`.

## What the server will and won't do

- **Read-only, six ways over.** The login can't write; the connection asks for read-only intent; the guard
  accepts one SELECT and nothing else; the policy hides payroll and card tables and masks tax IDs, bank and
  card numbers; queries run with row caps, timeouts and a 5 s lock timeout so Sage users aren't blocked.
- **Masked columns never leave the database.** They come back as `***`, and they can't be filtered,
  joined or sorted on.
- **Remote callers are authenticated and recorded.** Over HTTP, a request with a foreign `Origin` is
  refused, and so is one without a valid token. A token can be limited to some companies. Every tool call,
  over stdio too, goes to a daily audit file with the SQL sent but no argument values or tokens.
- **Rows are data written by people.** Customer names and comments can contain text that looks like
  instructions; the server returns them as data and says so in its tool descriptions. With no write
  path, the remaining risk is data leaving through other tools your MCP client has connected. That is
  your call as the deployer.

## Develop

```bash
uv sync
uv run pytest -m "not windows"     # unit, ProvideX emulator, contract; Premium tests skip without a SQL Server
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests scripts
```

The Premium tests build Sage-shaped databases with synthetic rows on a SQL Server you can drop:

```bash
docker run -d -p 1433:1433 -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='<sa password>' mcr.microsoft.com/mssql/server:2022-latest
SAGE100_MCP_TEST_SQLSERVER=localhost SAGE100_MCP_TEST_SA_PASSWORD='<sa password>' uv run pytest -m premium
```

The ProvideX tests run on any OS against `tests/emulator`, a pyodbc-shaped stand-in for Sage's driver
over DuckDB that refuses what a driver profile lacks with the driver's own messages. With a SQL Server,
`tests/premium/test_view_parity.py` runs every view on both backends over the same data and requires
identical results. `tests/windows` runs the 50 sample queries and reconciles the views to Sage's reports
([docs/reconciliation.md](docs/reconciliation.md)) against a real Sage 100 install with the ABC sample
company, on a self-hosted Windows runner.

## Hardening checks (SPEC.md §15)

- **Log scan (§15.8).** Run the suite with a log file, then scan the log and the test directories for
  passwords, tokens and masked values. CI does this after every run.

  ```bash
  uv run pytest -m "not windows" --log-file=build/pytest.log --log-file-level=DEBUG --basetemp=build/tmp
  uv run python scripts/log_scan.py build/pytest.log build/tmp --testbed
  ```

- **Load and soak (§15.7).** `scripts/soak.py` drives 20 concurrent tool calls against a running server.
  It reports timings, errors, memory and connection pools against the bars. Give it one token per worker,
  or raise `limits.rate_per_min` for the run.

  ```bash
  uv run python scripts/soak.py http://127.0.0.1:8765/mcp --tokens-file tokens.txt --minutes 120 --company ABC
  ```

- **Model evals (§15.6).** `scripts/run_evals.py` has Claude Code answer the 40 questions in
  `evals/tasks.yaml` and scores the answers; [docs/evals.md](docs/evals.md) explains it.

## Release checks (M6)

- `scripts/clean_install.py` times a clean install to the first answer against the Premium test bed:
  wheel, fresh environment, `init`, `doctor`, and a first answer over stdio.
- `scripts/service_smoke.py` installs, starts, queries and removes the Windows service, on Windows as
  Administrator.
- `scripts/helper_smoke.py` has the 64-bit server read a 32-bit-only Windows ODBC driver through the 32-bit
  helper.
- `.github/workflows/release.yml` builds the following for a tag: the wheel, the 32-bit helper, the frozen
  server and the MSI. It signs them when the signing secrets are set, and installs the MSI on the runner
  to check it. Publishing to PyPI and the MCP registry stays off until the repository's variables turn it on.

`CLAUDE.md` holds the rules for working on this repo with Claude Code. `SPEC.md` is the source of truth.
