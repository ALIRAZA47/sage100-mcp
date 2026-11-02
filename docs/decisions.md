# Decisions and spec deviations

SPEC.md is the source of truth. This file records choices the spec leaves open, places where the
build differs from it, and facts found while building that the spec should absorb. Newest milestone first.

## M6 — Release (Nov 1, 2026)

M6 packages the server:
- the version and its MCP registry entry;
- a Windows service;
- a one-folder build and the MSI around it;
- signing, and a release workflow;
- the setup guide.

It also settles D3, the 32-bit driver, with a helper process (D67). The exit gate needs a clean Windows
VM, which this environment lacks. D74 says what was measured instead and what stays open.

**D67. Sage's 32-bit driver runs in a 32-bit helper process (D3, option 3).**
- **Why.** A process loads only ODBC drivers of its own bitness. The server's own dependencies (DuckDB,
  cryptography) no longer ship 32-bit builds, so §10.1's 32-bit server can't be built (D3). Sage
  installs older than 2025 may have only the 32-bit driver.
- **What it is.** `src/sage100_mcp/bridge/helper.py` is one file that needs only the standard library
  and pyodbc. It runs under any 32-bit CPython 3.9 or later, or frozen as `sage100-mcp-odbc32.exe`
  (`packaging/odbc32.spec`).
- **The protocol.** The 64-bit server sends the helper the DB-API calls it would make on pyodbc:
  connect, cursor, execute, fetch, tables, columns, primaryKeys, getinfo, cancel and close. They travel
  as length-prefixed JSON over the helper's stdin and stdout. Decimals, dates, times, bytes and
  description type codes travel tagged, so they arrive as the same Python types.
- **Concurrency.** Each connection's calls run in order on their own thread in the helper. A cancel runs
  at once, alongside the call it cancels.
- **The helper's lifetime.**
  - It exits when its input ends, even with a driver call still running.
  - If it dies, the waiting calls fail with `BACKEND_UNREACHABLE`, and pooled connections from that
    helper are dropped. The next connect starts a new one.
  - A helper that can't start is `DRIVER_NOT_INSTALLED`, with the helper's own hint (errors.yaml).
  - The connection string, password included, goes through the pipe, never on a command line. The
    helper's environment has no `SAGE100_MCP_*` or `PYTHON*` variables.
- **The executor is unchanged.** `bridge/client.py` gives it pyodbc's surface: `connect`, `Error` with
  `(sqlstate, message)` args, the getinfo codes, and rows whose columns are attributes. So the
  executor, the probe and doctor run through it unchanged.
- **Configuration.** A ProvideX backend takes two new settings, which are not in §16.1:
  - `driver_bitness: auto | 32 | 64`. `auto` uses the helper only where Sage's driver is registered
    for 32-bit alone.
  - `helper`, for a pip install: a 32-bit `python.exe` with pyodbc. The MSI's helper,
    `odbc32\sage100-mcp-odbc32.exe`, is found without it.
- **Probe cache.** The capability cache is kept per bitness (`<version>-x86.json` for the helper),
  because the two builds of a version differ (§4.1). `sage_status` and `doctor` name the helper.
- **Deviation from §10.1, §15.4 and §16.3.** There is no 32-bit server and no 32-bit MSI. One x64 MSI
  serves both driver builds. The "32-bit Python" CI leg is the 64-bit server with the helper
  (`SAGE100_MCP_TEST_PROVIDEX_HELPER` on the Windows runner).
- **Tested.**
  - Every executor and probe emulator test runs both directly and through the helper (conftest.py
    parametrizes the route). So does a whole-server comparison of six tool answers.
  - The child-process tests cover: exit at the end of input, a killed helper replaced, a helper that
    can't start, one that never answers, one of another protocol, and redacted stderr.
  - With the real pyodbc on SQL Server, values, descriptions, catalog rows and errors are identical to
    calling pyodbc directly, and a cancel (HY008) and the query timeout (HYT00) stop a running statement.
    That holds under Python 3.12 and under 3.9.25 with pyodbc 5.2.
  - The frozen helper, built from the spec on Linux, served the same queries. The frozen server ran its
    bundled `helper.py` through a configured Python.
  - CI's `helper32` job has the 64-bit server read Windows' 32-bit-only Jet text driver through a
    32-bit Python (`scripts/helper_smoke.py`).
- **Found by the first run on Windows.** pyodbc sets `SQL_ATTR_CONNECTION_TIMEOUT` as soon as
  `connection.timeout` is assigned, even to 0. Jet's text driver refuses that attribute (HYC00), and
  the helper had been assigning the timeout before every cursor.
  - The bridge now forwards an assignment when the server makes it, as its own request (protocol 2), so a
    refusal arrives where it would in-process.
  - The ProvideX probe had the same flaw: it assigned 0 before each check. Sage's driver already refuses
    the login timeout (§14.2), so it may refuse this attribute too. The probe now clears a timeout only
    when one is set, and reads a refusal at assignment as `query_timeout` unsupported.
  - The emulator models the attribute (`accepts_connection_timeout`), and the probe is tested with it
    refused, directly and through the helper.
- **Cost.** About 3 µs a row, measured on 50,000 four-column rows over SQL Server: 0.10 s directly,
  0.25 s through the helper. A 250,000-row hybrid snapshot pays about 0.75 s. ProvideX reading over a
  share is far slower than that.

**D68. The MSI installs; it doesn't register the service (deviation from §16.3).**
- **Why not.** The service needs its account's password. An MSI would carry it in a property, and
  properties can surface in verbose installer logs. `sage100-mcp service install` reads the password from
  an environment variable instead.
- **What the MSI does.** It installs the one-folder build, with the helper as `odbc32\`, to Program
  Files, and puts that folder on the system PATH. It creates `%ProgramData%\sage100-mcp` with an
  inheritance-protected ACL: Administrators and SYSTEM, full control.
  - `service install` then adds the service account, with modify access. That is §16.3's
    "Administrators and the service account".
- **What it keeps.** The home folder is permanent: uninstalling keeps the config, the tokens and the
  audit trail. The UpgradeCode is fixed, so a newer MSI replaces an older one in place.
- **Version number.** The MSI's version is the numeric part of the package version: 1.0.0rc1 installs as
  1.0.0. Same-version upgrades are allowed, so the final 1.0.0 replaces the release candidate.

**D69. `init` on a host with no keyring (found by the clean-install check).**
- Linux without a desktop session, and containers, have no keyring backend, and `init` crashed when it
  stored the login there.
- Now `init` names the two environment variables the server reads instead, such as
  `SAGE100_MCP_CRED_PREMIUM_USER` and `_PASSWORD`, and still writes `config.yaml`.

**D70. The service reads the login from its own account's keyring.**
- Windows Credential Manager keeps a login per account. `init` stores it for whoever runs `init`, but the
  service runs as its own account.
- The guide (docs/setup.md, step 5) has the service account run `init`, with `runas /profile`, after
  `service install` has given it the home folder.
- **Open:** a setup path for accounts that can't log on interactively, such as group managed service
  accounts. For example, the service could import a one-time file from its protected home on first start.

**D71. The Windows service (§16.2, §16.5).**
- **Registration.** `service install` registers `<this program> service run`, which is
  `python -m sage100_mcp service run` from a pip install. It starts automatically once the network is up
  (delayed auto start), and restarts after a failure: twice, a minute apart, with the count reset daily.
- **The account.** The account gets "Log on as a service" and modify access to the home.
  - A password comes from `--password-env`. A group managed service account (`CORP\svc$`) needs none.
  - LocalSystem is allowed, with a warning: it can't read a Sage share over UNC, so it suits only a
    Premium-only host. A config with a ProvideX backend makes the warning say so.
- **Running.** The service runs the same `Host` as `serve`, and stops it the same way. It logs to
  `<home>\logs\sage100-mcp.log` (rotated, with secrets redacted), and the event log gets start and
  failure lines.
- **Tested.** `tests/unit/test_service.py` uses a fake service manager. `scripts/service_smoke.py`
  installs, starts, queries over HTTP and removes the real service on windows-latest. That CI job hasn't
  run yet.

**D72. Version 1.0.0rc1 and the MCP registry entry.**
- **The version.** The package is `1.0.0rc1`. `server.json` says `1.0.0-rc.1`, because the registry
  wants semantic versions. A test checks the two agree, and validates `server.json` against the
  registry's schema, vendored at its 2025-12-11 version.
- **The name.** It is `com.outova/sage100-mcp`. That namespace is proven by DNS, an Ed25519 key in a TXT
  record on outova.com. The README carries the `mcp-name` marker that PyPI ownership checks read.
- **The two entry points.** Both run from PyPI through `uvx`: stdio, and Streamable HTTP with a bearer
  token. `_meta` says ProvideX needs Windows.
- **Not published.** Publishing needs the user's decisions on two things: a license (the repository has
  none yet) and the product name under Sage's trademark guidelines (§19.1, ask a13).

**D73. The release workflow (.github/workflows/release.yml).**
- **What it builds for a v* tag:**
  - the wheel and sdist, with the wheel installed into a fresh environment that must answer sage_status;
  - the 32-bit helper, under 32-bit Python;
  - the 64-bit one-folder server, with the helper copied in. Both are smoke-tested.
- **Signing.** When the signing secrets exist (`WINDOWS_SIGNING_CERT_PFX_BASE64` and
  `_PASSWORD`), both programs and the MSI are signed with Authenticode and an RFC 3161 timestamp.
  Without them the build is unsigned, and says nothing.
- **The MSI** is built with WiX 5.0.2. WiX 6 and later need the Open Source Maintenance Fee EULA
  accepted. The runner installs the MSI, checks the program, the helper, the PATH and the home folder's
  ACL, and uninstalls it.
- **For a tag**, a draft GitHub release gets the artifacts and their SHA-256 sums.
- **PyPI (trusted publishing) and the MCP registry** run only when the repository variables
  `PUBLISH_PYPI` and `PUBLISH_REGISTRY` are `true`. The registry job downloads the `mcp-publisher`
  version named in `MCP_PUBLISHER_VERSION`; v1.8.7 was current when this was written.
- **Pinned versions.** PyInstaller is 6.22.3. The actions keep ci.yml's major versions, which all
  exist: checkout v5, setup-uv v7, setup-python v6, upload-artifact v5, download-artifact v6. Newer
  majors are out (checkout v7, setup-uv v10, upload v7, download v8), and moving to them is a separate
  change.

**D74. The M6 gate: what was measured, and what stays open.**
- **Measured here (Linux):**
  - `scripts/clean_install.py` went from wheel to first answer on the Premium test bed in 6.5 s, with
    no package cache and no keyring: build 0.1 s, install 1.1 s, `init` 0.7 s, `doctor` 1.9 s, first
    answer 2.7 s.
  - The PyInstaller builds of the server and the helper both ran.
  - The bridge suites passed, including the real-driver ones.
- **Open, each needing Windows:**
  - the timed clean-VM run of docs/setup.md, the gate itself;
  - the MSI build and install, and signing with a real certificate;
  - the service on a real host (CI's service job);
  - the helper with the Jet text driver (CI's helper32 job);
  - the helper with Sage's own 32-bit driver (the Windows runner, a03).

## M5 — Hardening (Oct 31, 2026)

M5 adds §15's remaining checks and the setup commands: the log scan (§15.8), a load and soak driver
(§15.7), the model eval set with its runner (§15.6), `init`, and more `doctor` checks (§16.2). Measuring
D16's deferred question found a real slowdown on Premium, fixed in D59. The log scan found a password in a
logged traceback, fixed in D60. D66 lists which §15 bars are met and which need the Windows runner.

**D59. Premium binds string parameters as varchar (D16, measured).**
- **The slowdown.** pyodbc sends a Python string as `nvarchar`. Sage's keys are `varchar` under
  `SQL_Latin1_General_CP1_CI_AS`, so SQL Server converts the column (`CONVERT_IMPLICIT`). On the test bed,
  `AR_OpenInvoice WHERE ARDivisionNo = ? AND CustomerNo = ?` ran as a clustered index scan, and as a seek
  once the parameters were `varchar`. On MAS_EMP's 103,715 open invoices every customer filter would have
  read the whole table.
- **The rule.** A string the cp1252 code page can hold, of up to 8,000 characters, is bound as
  `varchar(8000)`. One size for every length keeps one cached plan. Any other string keeps `nvarchar`, so
  it still compares exactly, and other types keep pyodbc's binding.
- **Tests.** `tests/premium` reads the cached plan back from SQL Server and requires a seek.

**D60. The log scan (§15.8), and the leak it found.**
- **What it looks for.** `scripts/log_scan.py` reads every text file under the paths it is given,
  including gzipped audit days. It fails on:
  - `PWD=` or `PASSWORD=` with any value but `***`;
  - a bearer token;
  - the passwords the tests use, and the SA password;
  - with `--testbed`, any value of six or more characters that the synthetic data keeps in a masked column.
- **Where it looks.** CI runs the suite with a DEBUG log file and a known temporary directory, then scans
  both: the log, every audit file, and the logs of the servers that tests start.
- **Error details.** The server now logs each tool error's envelope at DEBUG, so error `detail` text is
  scanned too.
- **Planted fixtures.** A directory holding `.log-scan-fixture` is a planted test of the scan itself, and
  is skipped when scanned from above.
- **What it found.** An unexpected exception whose message held a connection string was logged with its
  traceback, password included. The client was never sent it; the contract test that simulates it only
  checked the client's side.
- **The fix.**
  - Tracebacks are logged through `logs.failure_text()`, which removes connection-string passwords and
    bearer tokens and keeps everything else readable.
  - A `RedactSecrets` filter on the server's log handlers, and on uvicorn's, does the same to every line
    (§9.4: "redacted from every log line").
- **A second finding, in the eval runner (D62).** It copied every `SAGE100_MCP_*` variable into the MCP
  config it hands to Claude Code, the test SA password among them. It now passes only what a stdio server
  reads: `SAGE100_MCP_HOME`, `_CONFIG`, `__` overrides and `_CRED_` logins. The config file is private to
  the run and deleted after it.
- **Writes refused by the driver itself.**
  - Premium: the M1 test connects as the reader login, outside the server, and gets error 229 for an
    `UPDATE`.
  - ProvideX: `tests/windows/test_security.py` does the same on the Windows runner, with an `UPDATE` that
    matches no row, so even a driver that took writes would change nothing.

**D61. Memory and pools in `sage_status`, and the soak (§15.7, §13.5).**
- **New in `sage_status`.**
  - `process`: pid, memory, threads and uptime. Memory is the working set on Windows, RSS on Linux, and
    the peak on macOS.
  - Each backend's `pools`: cap, connections in use, and idle ones.
- **The driver.** `scripts/soak.py` runs N workers, 20 by default.
  - They cycle through 13 tools, mostly the views.
  - Each worker uses one token, since the server limits each token to 30 calls a minute (D54). A
    `RATE_LIMITED` answer is waited out and counted, not failed.
  - It samples `sage_status` every 30 s.
  - With `SAGE100_MCP_SOAK_SQLSERVER`, the connection string for a login with VIEW SERVER STATE, it also
    counts the server's sessions as SQL Server sees them, so a leaked connection can't hide behind the
    pools.
- **It passes when:**
  - no view hit `QUERY_TIMEOUT`;
  - no call failed with `INTERNAL` or a transport error;
  - memory moved less than 10% after the first fifth of the run;
  - no pool held more connections than its cap;
  - sessions stayed within the caps.
- **The suite** runs 12–15 s soaks on the emulator and on SQL Server.
- **A 25-minute run.** Against the real `serve` process on the test bed:
  - 91,061 calls by 20 workers, none failed, p95 of 0.83 s or less;
  - memory from 105.8 to 123.6 MB, +0.2% after warm-up;
  - pools at most at their cap of 4, and at most 6 SQL Server sessions.

  It started before D59's binding change.
- **Still open:** the two-hour run against ABC, with a scripted Sage user whose screen timings must stay
  within 10%. That needs the Windows runner.

**D62. The model eval set and its runner (§15.6).**
- **The questions.** `evals/tasks.yaml` holds 40:
  - AR 12, AP 8, sales orders 6, purchase orders 4, inventory 4, GL 3, sales history 2;
  - one request to change data, which must be refused.

  Each names its company and dates, and says how its answer is written: money, number, count, code,
  codes, month, yes or no, or refused.
- **The golden answers.**
  - `golden.testbed` is worked out in plain Python from the synthetic rows. Tests require it to equal both
    the stored value and the server's own tools, on the emulator and on SQL Server.
  - `golden.abc` waits for ABC's reports (docs/evals.md).
- **The runner.** `scripts/run_evals.py` starts `claude -p` for each question:
  - with `--tools ""` (no built-in tools), only `mcp__sage100` allowed, `--strict-mcp-config` and
    `--no-session-persistence`;
  - in an empty working directory;
  - with the launching session's variables (`CLAUDE_CODE_*`, `CLAUDECODE`, `CCR_*` and the like) removed.

  It can reach the server over stdio or HTTPS. It scores the last `ANSWER:` line, and reports tool calls,
  tool errors, `WRITE_BLOCKED`, tokens and cost, against the bar. A scripted stand-in for Claude Code
  tests it.
- **Not run in this session.** The Claude Code available here belongs to the session building the project.
  Its variables would tie a nested run to that session, and without them it has no credentials. The run
  needs an API key or a login on a developer machine or CI. A one-line probe cost $0.08, mostly Claude
  Code's own prompt; a question costs a few cents more.
- So the tier 7 bar is not yet measured. Nor is D44's question, whether trimming the tool schemas is worth
  doing; the runner's token counts will answer it.

**D63. `init` (§16.2).**
- **Questions.** It asks for the edition: providex, defaulting to it when SOTAMAS90 exists or on Windows,
  or sqlserver. Then:
  - **ProvideX:** the MAS90 folder (SOTAMAS90's Directory by default) and the Sage login. It warns when the
    folder is a drive letter, which a service can't see. It copies SOTAMAS90's Prefix and ViewDLL only when
    the folder is the DSN's own.
  - **Premium:** the SQL Server and its login.
- **Order of work.** It connects with the login and lists the companies before it stores anything. It
  stores the login in the keyring under `sage100-mcp/sage` or `sage100-mcp/premium`. It writes
  `config.yaml` only after loading it back as the server would.
- **Unattended.** `--yes` with flags asks nothing; a missing answer stops `init` and names the flag. A
  password comes from a prompt or `--password-env`, never from the command line.
- **An existing config** is kept unless `--force`.

**D64. `doctor` (§16.2): clocks and the audit directory.**
- **Clock skew** is measured against SQL Server (`SYSUTCDATETIME()`, round trip halved) and against the
  OAuth issuer (its HTTP `Date` header). Within 30 s is OK; up to 5 minutes is a warning; beyond that it
  fails. 30 s is the JWT leeway; past five minutes, Kerberos logins fail and "today" defaults can fall on
  the wrong date.
- **The audit directory** must take a write, because a failed audit write is only logged (D55).
- ProvideX has no clock to read: the share is never written to.

**D65. Audit files: earlier days are gzipped (amends D55).**
- **Why.** The 25-minute soak wrote 105 MB, about 1.1 KB a call, since each line keeps the SQL sent. Kept
  for 90 days, a busy server's files would fill a disk.
- **What changed.** The daily prune now gzips every earlier day's file (`YYYY-MM-DD.jsonl.gz`), which
  shrinks JSON text about tenfold, and deletes files past 90 days whether gzipped or not.
- **The log scan** reads the gzipped days too.

**D66. The M5 gate: which §15 bars are met.**
- **Met:**
  - tiers 1, 2 and 6;
  - tier 5 on the test bed: every view's rows are identical on SQL Server and both emulated drivers;
  - §15.8's log scan, with no finding after D60's two fixes;
  - Premium's direct-driver write test;
  - tier 8's server-side bars on the test bed: no view timeouts, flat memory, pools within their caps, no
    leaked sessions.
- **Not met yet; each needs something this environment lacks:**
  - tiers 3 and 4, the real ProvideX driver and reconciliation to ABC's reports (the Windows runner, a03);
  - tier 5's ABC conversion and the run on a restored MAS_EMP copy (a01);
  - tier 7's run, which needs credentials for Claude Code and ABC's golden answers;
  - tier 8's two-hour ABC run with a scripted Sage user;
  - ProvideX's direct-driver write test, written and waiting for the Windows runner.

## M4 — Remote (Oct 30, 2026)

M4 adds Streamable HTTP with bearer and OAuth authentication, Origin and Host checks, per-principal state,
run_query cursors, the rate limit and the audit log. D50 to D57 record what the spec left open and where
the build differs from it. `uvicorn`, `starlette`, `pyjwt[crypto]` and `cryptography` are now declared
directly (D2); they already came with `mcp`, so the lock file gained no package. Tests use `httpx2`, the
SDK's HTTP client.

**D50. Principals and what is kept per principal (§9.3, §10.3).**
- A principal is the stdio process (`stdio`), a static token (`token:<id>`) or an OAuth issuer and subject
  (`oauth:<issuer>#<subject>`). The issuer is part of it, so equal subjects from two issuers stay apart.
  Two clients using the same token are one principal and share its active company.
- The active company is kept per principal, in memory. Set over HTTP it lapses 8 hours after
  `set_company`; over stdio it lasts as long as the process. A restart forgets it, as application state
  may (§10.3).
- A token with `sage:company:ABC` scopes sees only those companies, wherever a company is named or listed:
  `list_companies`, `sage_status`, `compare_companies`, the companies a view's module error names, and
  company resolution. A configured default outside the scope is ignored, and a token scoped to one company
  needs no `company` argument. Naming a company outside the scope gives `COMPANY_NOT_FOUND` with the
  in-scope choices, the same answer as for a company that doesn't exist.
- A server middleware binds the principal to every request in a context variable that worker threads
  inherit, so the tools did not change. It fails closed. A request without an authenticated user sees no
  company, and so does any call that reaches a server built for HTTP without an HTTP request behind it.
  Authentication already prevents the first case.

**D51. HTTP settings beyond §16.1: `public_url`, `tls_cert`, `tls_key`.**
- `public_url` is the endpoint URL clients use when a TLS proxy fronts the server, such as
  `https://sage-mcp.corp.example/mcp`. It names the resource in the OAuth metadata, adds its host to the
  allowed `Host` values (D56), and its path is the endpoint's path (`/mcp` without it). The proxy forwards
  paths unchanged, which Caddy does by default.
- `tls_cert` and `tls_key` are PEM files for serving TLS directly, §10.2's "bind the LAN address directly
  with TLS".
- `serve` refuses to start when:
  - it would take connections from other machines without TLS (a non-loopback `bind` with no `tls_cert`
    and no `https` `public_url`);
  - it would listen on every address (`0.0.0.0`, `::`) without a `public_url` to name itself;
  - the certificate or key file is missing;
  - the configuration is rejected. Unlike stdio (D7), whoever starts `serve` sees the error.

**D52. Authentication (§10.3, §16.2).**
- **Static tokens.** `token create --principal NAME [--companies ABC,EMP]` prints `s1mcp_` and 43
  URL-safe random characters (256 bits) once. `<home>/tokens.json` keeps:
  - a short public id;
  - the principal's name and companies;
  - the creation time;
  - the token's SHA-256.

  Unsalted SHA-256 suffices for 256-bit random secrets. The file is replaced atomically and is mode 600 off
  Windows; on Windows the `%ProgramData%` ACL of §16.3 protects it. It is re-read whenever it changes, so
  `token revoke` takes effect on the next request. `token list` and `token revoke` go beyond §16.2.
- **OAuth 2.1.** The server is a resource server for JWT access tokens from `http.oauth.issuer`.
  - Keys come from the issuer's JWKS, found through its RFC 8414 metadata (the issuer's path follows the
    well-known segment) or OpenID Connect discovery.
  - `iss`, `aud` (= `http.oauth.audience`), `exp` and `sub` are required, with 30 s of leeway.
  - Only asymmetric algorithms are accepted (RS, PS, ES, EdDSA): no shared-secret HS256, no `none`.
  - Scopes come from `scope` (space-separated) or `scp` (a list).
  - Opaque tokens, which need introspection, are not supported.
  - The Protected Resource Metadata of RFC 9728 is published at
    `/.well-known/oauth-protected-resource<path>` and names the issuer. `WWW-Authenticate` points to it.
  - The SDK's RFC 8707 resource check stays off, because the verifier checks the audience itself.
- **Both kinds.** A missing, unknown, revoked or expired token gets 401 with `WWW-Authenticate: Bearer`.
  A token without `sage:read` gets 403 `insufficient_scope`. Static tokens always carry `sage:read`.

**D53. Cursors (§13.3), and `CURSOR_INVALID`, a new error code.**
- **The token.** A cursor is signed with HMAC-SHA256 under a key made at startup. It carries:
  - the principal and the company;
  - a hash of the query (sql, params, mode and dialect);
  - the offset of the next row, and a digest of the rows already shown;
  - an expiry.

  `CURSOR_INVALID`, which §14.2 lacks, refuses a cursor that was altered or predates a restart, belongs to
  another principal or another query, has expired, or names a different company from a `company` argument.
  The company comes from the cursor, not the active company.
- **Deviation: when the snapshot is taken.** §13.3 keeps the full result on the first call. Here the first
  call costs what it did before cursors. The first use of a cursor runs the query again, keeping up to
  `max(max_pushdown_rows, offset + page size)` rows, and the rows already shown must come back first, in
  any order (a multiset digest). Otherwise `CURSOR_INVALID` says the rows changed and suggests ordering by
  a unique key. Agents usually narrow a truncated query rather than page through it, so only paging pays
  for the snapshot. Later pages read the snapshot and send nothing to the database; their `sent_sql` is
  empty.
- **Deviation: Premium pages from the same snapshot**, not with `ORDER BY <key> OFFSET … FETCH`. That would
  need a unique ordering, which joins, aggregates and set operations lack, and a page read later could skip
  or repeat rows that Sage posted in between. One mechanism serves both backends.
- **How snapshots are kept.**
  - A snapshot lives 10 minutes from when it was taken, and at most 16 are kept (the oldest is dropped).
  - They are held in an in-memory DuckDB with a 1 GB limit, spilling to `<home>/tmp`.
  - A page repeats the snapshot's columns, mode, warnings and `as_of` (when the rows were read).
  - Its `RESULT_TRUNCATED` says which rows of how many it holds. If the result outgrew the snapshot, the
    last page says so and carries no cursor.
- Only `run_query` pages. The views have their own `max_rows` and total every row.

**D54. Rate limit (§9.3, §14.2).** Every `tools/call` counts against its principal over a sliding 60 s
window at `limits.rate_per_min` (30). The stdio process is one principal. Discovery tools count too: §14.2
says "calls". A refused call is not counted. `RATE_LIMITED` adds `retry_after_s`, a new envelope field:
the seconds until the oldest call leaves the window, at least 1.

**D55. Audit log (§9.4).**
- **Files.** One file per UTC day, `<audit_dir>/YYYY-MM-DD.jsonl` (default `<home>/audit`). Files older
  than 90 days are deleted on the day's first write.
- **Who called.**
  - `ts`.
  - `principal`: its stable id.
  - `name`: the token's principal name, the OAuth subject, or `local`.
  - `transport` and `client`: the client's name and version, from the handshake or the 2026-07-28 `_meta`.
- **What, and how it ended.**
  - `tool`, `args` (argument names, never values) and `company`.
  - `sql` as sent; with `audit_redact_literals`, quoted literals become `'…'` and numbers become `?`.
  - `mode`, `rows`, `elapsed_ms`, `outcome` and `code`.
  - `correlation_id`, for `INTERNAL` errors.
- §9.4's "session" is the principal: 2026-07-28 has no protocol sessions.
- **Mcp-Name.** `tool` comes from the request body. The SDK refuses a 2026-07-28 request whose `Mcp-Name`
  differs (`HEADER_MISMATCH`), so the two agree; 2025-11-25 requests carry no `Mcp-Name`.
- **What is logged.** Every tool call, over stdio too, including rate-limited calls and calls whose
  arguments the SDK refused (`INVALID_ARGUMENTS`). Headers and argument values are never read into the
  log, so tokens can't reach it. A failed write is logged as an error and never fails the call.

**D56. Origin and Host (§10.3).** An ASGI guard runs before authentication.
- An `Origin` outside `allowed_origins` gets 403, with or without a good token; `null` does too. A request
  with no Origin, as Claude Code sends, passes.
- A `Host` that does not name this server gets 421. The names it accepts are the bind address, the
  loopback names when bound to loopback, and `public_url`'s host. This stops DNS-rebinding pages from
  reaching a local server.
- The SDK's own transport-security check would run after authentication, so it is off.

**D57. The M4 gate: HTTP contract tests green; the Mac and eval-set half is open.**
- **Green.**
  - `tests/contract/test_http.py`:
    - the 22 tools over HTTP for both protocol revisions;
    - 401 with `WWW-Authenticate`, 403 for a foreign Origin, 421 for an unknown Host;
    - revocation;
    - a company-scoped token, the active company per principal, and a cursor refused for another
      principal;
    - the rate limit per principal, and audit lines without secrets or values;
    - Protected Resource Metadata, and JWTs from local RSA keys: good, wrong audience or issuer, expired,
      no `exp` or `sub`, another key, no `sage:read`.
  - `tests/contract/test_tools.py` now runs each tool's happy path and each error code over Streamable
    HTTP as well as in process (§15.5).
  - `tests/contract/test_tls.py` runs the real `token create` and `serve` commands with a self-signed
    certificate, and connects with the SDK client over HTTPS for both revisions.
  - `tests/contract/test_remote_check.py` runs the Mac pre-flight against an emulated ProvideX company.
- **Open:** "a Mac Claude Code client completes the eval set over TLS". The eval set (§15.6) has golden
  answers from ABC, which needs a Sage install (the Windows runner) and is M5's model-eval work.
  `scripts/remote_check.py` is the pre-flight to run from the Mac: TLS, the token, Origin, both revisions,
  company state, paging and a view. `docs/remote.md` is the runbook.

**D58. The guard reads back what it will send (§9.1).**
- **Found by the fuzz test during M4.** The ANSI fallback of D17 read `SELECT TOP FROM t`, which T-SQL
  refuses, as selecting a column named TOP. The T-SQL regenerated from it does not parse.
- **Fix.** The guard now regenerates every accepted statement as T-SQL and parses it again. Unless that
  gives one statement of the kind it checked, the statement is refused with `PARSE_ERROR`. This tightens
  the guard; two rejecting cases went into the corpus first.
- **Nothing unsafe could have been sent.** The resolver refuses an unknown column TOP, and SQL Server
  would refuse the text.

## M3 — Canned views (Oct 29, 2026)

The four questions §18 leaves to M3 were settled on MAS_EMP through the Outova connector, with counts and
yes/no checks only (no balances or names), and against Sage's help. D37 to D41 record the answers.

**D37. `BeginningBalance` is the year-opening balance, on period 01 only (§12.6).**
- On MAS_EMP no `GL_PeriodPostingHistory` row after period 01 carries a `BeginningBalance`. Every
  balance-sheet account's period 01 balance equals its prior-year closing balance, and income and expense
  accounts start the year at zero (the close moves net income to retained earnings).
- So §12.6's `ending(p) = BeginningBalance(p) + DebitAmount(p) − CreditAmount(p)` holds for period 01 only.
  The view uses `ending(p) = Σ q≤p (BeginningBalance + DebitAmount − CreditAmount)`. A period without a row
  adds nothing, which is §12.6's carry-forward.
- Columns: `beginning_balance` (at the start of the period), `period_debits`, `period_credits`,
  `ending_balance` (debit positive), and `debit` and `credit`, the ending balance in its own column. The
  totals check compares `debit` with `credit`. Accounts with nothing in any column are left out unless
  `include_zero` is true.

**D38. Sales order quantities and value (§12.3).**
- Posting an invoice leaves `QuantityOrdered` alone and adds to `QuantityShipped`, and a partly shipped order
  becomes a back order (B). On MAS_EMP, partly shipped open lines keep their ordered quantity, and
  `IM_ItemWarehouse.QuantityOnSalesOrder` and `QuantityOnBackOrder` equal the open quantities of the S and B
  orders. So `open_qty = QuantityOrdered − QuantityShipped`, as §12.3 has it.
- Deviation: `open_value` is the line's stored `ExtensionAmt`, not `open_qty × UnitPrice`. On every open S
  and B line on MAS_EMP, partly shipped and discounted ones included,
  `ExtensionAmt = ROUND((QuantityOrdered − QuantityShipped) × UnitPrice × (1 − LineDiscountPercent/100), 2)`.
  The spec's product overstates discounted lines.
- Line types: on MAS_EMP's open orders, charges are ItemType 3 with an amount and no quantity, and comments
  are type 4 with neither. Charge (3), comment (4) and miscellaneous (5) lines are left out by default.
  `include_misc` keeps them, including the quantity-free ones that the open-quantity test would drop. The
  test bed's charge line has the same shape.
- `QuantityBackordered` is shown, as §12.3's pull lists it. `order_types` defaults to S and B; Q, M and R
  are opt-in.

**D39. Purchase order statuses (§12.4).** Sage's Purchase Order Entry help lists New, Open, Change, Back
Order and Completed. Change is an existing PO being modified, with a change order to print, so it is open
([help](https://help-sage100.na.sage.com/2023/Subsystems/PO/POMainFields/Purchase_Order_Entry_-Fields.htm)).
- Open statuses are N, O, B and C; §12.4 has N, O and B.
- R, received but not yet invoiced, is left out: on MAS_EMP no R line has a quantity still to receive.
- X is completed.
- `open_value` stays `(QuantityOrdered − QuantityReceived) × UnitCost`, as §12.4 has it. Unlike SO lines,
  a PO line's `ExtensionAmt` keeps the full ordered value after a partial receipt (checked on MAS_EMP).
- `QuantityBackordered` is shown, as §12.4 lists it.

**D40. Available quantity follows Sage's own option (§12.5).**
- `views.inventory_available_formula` gains the value `sage`, now the default. It reads
  `IM_Options.QuantityAvailableToInclude` as Item Inquiry does. N gives
  `onhand − so − bo − wo_required`, which is §12.5's formula. P adds purchase orders, W adds work orders,
  and B adds both.
- Explicit formulas still work, and `po` and `wo` are now allowed as terms.
- `QuantityInShipping` is never subtracted. It is part of the sales order quantity until the invoice posts:
  on MAS_EMP no warehouse row has more in shipping than it has committed.
- `projected` is always `onhand + po + wo − so − bo − wo_required`, so supply is never counted twice.
- Deviation: `docs/config.example.yaml` sets `sage` where §16.1's example sets the N formula. The config test
  applies this one difference before comparing the two.

**D41. Credit memos are not sign-flipped (§12.7).**
- Deviation: on MAS_EMP, credit memo history lines for returned items are stored with negative amounts and
  quantities. The few positive lines are mostly charges on the memo, such as restocking fees, which do add
  to sales.
- `sales_history` therefore sums `ExtensionAmt` as stored. Flipping by `InvoiceType`, as §12.7 says, would
  turn returns into sales.
- Debit memos count. Finance charges and adjustments are not sales and are left out.
- Revenue is the line amount. Cost is `QuantityShipped × UnitCost`, rounded per line.

**D42. Aging rules (§12.1, §12.2).**
- Category starts are inclusive. With MAS_EMP's 1, 16, 31 and 61 the buckets are current (under 1),
  days_1_15, days_16_30, days_31_60 and days_61_plus. Labels are generated from the options, and the bucket
  columns are renamed to them.
- Unit: `AR_Options.AgingCategory` and `AP_Options.AgingCategoriesToUseForInv` say days (D) or months (M).
  Months are calendar months, as `DATEDIFF(month)` and DuckDB's `date_diff('month')` both count them. The
  labels and the `days` column then read `months`. No company in reach ages by month, so this awaits a
  reconciliation on one.
- Fixed: `AgingCategoriesToUseForInv` is that unit, not an aging basis, which an earlier M3 draft and M1's
  profile assumed. AP stores no basis, because its aging report asks each time. So `ap_aging` takes `age_by`
  (due or invoice, default due), and `ar_aging` takes it too, overriding `AgeInvoicesBy`.
- An invoice without a due date ages from its invoice date. One without either date goes into the oldest
  bucket, with `days` null.
- Without usable categories (four, rising, the first at least 1), 30/60/90/120 days apply with
  `AGING_DEFAULTS_USED`.
- `as_of` moves the aging date only, as §12.1 says for v1.
- A `division` filter while divisions are off gives `DIVISION_DISABLED`.

**D43. How a view is defined and run (§12, §12.9).**
- Each `views/<name>.yaml` holds:
  - parameters (typed, with defaults and choices);
  - ProvideX pulls, each a single-table SELECT. A pull can be conditional, keyed by another pull's values
    in 200-key `IN` chunks, or read only for one variant;
  - per-variant output columns, totals, a DuckDB query over the pulls, and one T-SQL statement.
- A line ending `-- if name` is sent only when `name` has a value. `{{name}}` takes SQL that
  `views/rules.py` writes from the company's options.
- ProvideX pulls go through the M2 planner, so pushdown follows the driver's probe. Premium runs its one
  statement through the same guard and binding as `run_query`.
- Both plans are formatted by the declared output columns:
  - money is half-up to cents, never `-0.00`;
  - quantities are trimmed of trailing zeros;
  - unit prices and costs keep at least cents;
  - Premium's 1753-01-01 becomes null.
  The T-SQL plans `ISNULL`/`NULLIF` what ProvideX returns as blanks, so cells match exactly.
- Totals cover every row read, up to `max_rows_hard`. Rows shown stop at `max_rows` (default 1000), with
  `RESULT_TRUNCATED`. A pull over `max_pushdown_rows` fails with `PUSHDOWN_LIMIT`.
- Lookups match in Python (case- and accent-blind, phone by digits) over a master read cached for 10 minutes
  (§13.4). They read figures for at most 20 matches with one `IN` list, and declare fixed output types.

**D44. The M3 tool surface (§11.1, §11.4, §11.5).**
- Eleven view tools, each returning one envelope: §11.2's fields without `mode`, plus `view`, `version`,
  `parameters` as applied, and `totals`. The text block shows the first 50 rows, the totals and the
  warnings.
- Deviations in arguments:
  - `sales_history` takes `from_date` and `to_date`, because `from` is a Python keyword;
  - both aging tools take `age_by` (D42);
  - `open_sales_orders` takes `include_misc`, which §12.3 names;
  - every view tool takes `max_rows`.
- `sage://views` holds each view's parameters, pulls, local SQL and T-SQL.
- The four §11.5 prompts arrive with the views they script.
- The 22 tools' names, descriptions and input schemas come to about 5,500 tokens by a characters/4
  estimate, against §11.1's 2,500. Most of that is the schemas' optional-argument boilerplate (`anyOf`
  with null, titles). M5's evals can show whether trimming it is worth a schema post-processor.

**D45. `compare_companies` (§8.3, §11.1).**
- It resolves every company, and checks every company has the view's modules, before it queries any of
  them.
- Rows get a leading `company` column. `backend` is `mixed` when the companies span both kinds.
- When the companies' columns differ, as aging buckets do between companies with different categories,
  columns are named `column_0`… with a `COLUMNS_DIFFER` warning per company saying what each holds, and
  totals are dropped.

**D46. `COLUMNS_DIFFER` is a new warning code**, for D45. Like D21's, it is not in §14.2.

**D47. Test data and the parity test (§15.4).**
- The synthetic test bed gained what the views need:
  - open invoices a day either side of every AR and AP bucket edge, plus blank-date invoices;
  - PCC aging AP by month;
  - partial shipments, line discounts, comment and charge lines, and M and R orders;
  - every PO status;
  - warehouse quantities derived from the open orders;
  - two balanced fiscal years of GL history;
  - posted invoices with negative credit memos, a debit memo, a finance charge and an adjustment;
  - `QuantityAvailableToInclude = P`.
- `tests/emulator/test_views.py` checks each view against figures worked out in plain Python from those
  rows, under both driver profiles.
- `tests/premium/test_view_parity.py` runs 41 view cases and 3 comparisons on SQL Server and on both
  emulated drivers. Columns, rows, totals, parameters and warning codes must be identical.

**D48. The M3 gate: parity met; reconciliation waits for the Windows runner.**
- The Premium/ProvideX parity diff is empty: 88 of 88 checks on this host's SQL Server 2025 test bed.
- "ABC reconciles to Sage's reports to the cent" needs Sage with the ABC sample company.
  `tests/windows/test_m3_reconciliation.py` checks it two ways:
  - against the figures Sage keeps on ABC's own records, with `scripts/verify_views.py` (D49). This needs
    no exports.
  - against report exports (goldens), laid out as `docs/reconciliation.md` describes. Every row and total
    is compared to the cent, and the comparison is itself tested on emulated results, including a
    one-cent change.
- Open: the runner (§19.1), the report exports, and a company that ages by month (D42).

**D49. The views reconcile to the figures Sage keeps, on MAS_EMP (Oct 30, 2026).**
Sage maintains several of the views' figures on its master records as it posts and ages. Each one was
compared with the views' rules on the real MAS_EMP through the Outova connector, using match counts only:
- **AR aging.** Each customer was aged as of its own `AR_Customer.DateLastAging` and compared with
  `CurrentBalance` and `AgingCategory1-4`. `CurrentBalance` is the not-yet-due bucket, not the whole
  balance; the overlay now says so.
  - Every customer whose open invoices were untouched since the aging run, and whose stored aging still
    adds up to its open invoices, agrees bucket for bucket, to the cent. The mismatches with matching
    totals are all customers with invoice changes on the aging day itself.
  - The view's rules fit far better than the alternatives tried: edges a day later or earlier, credit
    memos in the current bucket, aging by invoice date.
  - For a few customers the stored aging no longer adds up to their open invoices, with no activity
    since. No invoice type, date, sold-to, retention, overpayment or credit hold sets them apart, so the
    cause is outside the open invoices, and the check counts them rather than compares them.
- **Sales orders.** Each order header's merchandise (`TaxableAmt + NonTaxableAmt`) is the sum of its
  lines' `ExtensionAmt` (D38). `AR_Customer.OpenOrderAmt` is, for every customer, the sum over its S and B
  orders of merchandise + freight + sales tax − discount, counting only orders that still have open,
  charge or comment lines. So the one-row-per-order variant now also shows `merchandise`, `freight`,
  `sales_tax`, `discount` and `order_total`, and `order_total` is totalled.
- **AP.** Every vendor's open-invoice total equals `AP_Vendor.BalanceDue`.
- **Items.** Every item's on-hand total across warehouses equals `CI_Item.TotalQuantityOnHand`.
- **Purchase orders.**
  - The view's open value, `(QuantityOrdered − QuantityReceived) × UnitCost` over lines with something
    to receive, equals header merchandise less `ReceivedAmt` on every PO in status O.
  - It differs from that on some back-order POs, only through fully received lines whose receipt cost
    differed from the line's. Those lines have nothing left to receive, so the view is unchanged.
  - `QuantityBackordered` is 0 on POs where nothing has been received yet.
  - No purchase line uses a unit-of-measure conversion.
- **Trial balance.** It balances in every period of every year whose posting history balances. In a few
  periods of three earlier years MAS_EMP's own period debits and credits differ, and the view's
  `TB_OUT_OF_BALANCE` warning reports exactly that.

`scripts/verify_views.py` makes these checks repeatable on any company, ProvideX or Premium, through the
server's own code:
- it runs ar_aging per aging date, open_sales_orders (orders, charges kept), ap_aging,
  inventory_availability per warehouse, and gl_trial_balance per period;
- customers changed on or after their aging date, or whose stored aging is out of step, are counted
  apart;
- it prints counts and codes only.

The test bed now stores these figures as Sage would, with one customer of each set-apart kind. The script
runs in the emulator tests (both driver profiles, plus a one-cent fault it must name), the Premium tests,
and the Windows gate on ABC.

## M2 — ProvideX backend (Oct 29, 2026)

**D24. `EnforceNullDate=1` is sent only on a retry.** §5.4 sends it whenever the probe accepts it, and
marks every result `null_dates_coerced: true` either way. Always on, the flag would be true on every
ProvideX result and mean nothing, and every bad date would quietly read as NULL. So a query runs without
it; after `[ProvideX][ODBC Driver]Internal error` it is retried once on a new, unpooled connection with
`EnforceNullDate=1`, if the probe accepted that keyword. Only results that needed the retry carry the flag
and a `NULL_DATES_COERCED` warning. A table with bad dates costs one failed attempt per query.

**D25. The probe checks answers, not only syntax (§5.3).**
- It runs about 100 one-row statements rather than "about 30": 41 features, 28 functions in both the
  `{fn}` and bare forms, and 4 keywords. Each has 5 s and a cancel watchdog.
- A check passes only when the driver runs it and the answer is right. `LIMIT 1` must return one row, an
  `AND` must exclude a row, `{fn UCASE('abc')}` must return `ABC`, and a `SUM` of a decimal column must
  come back as a decimal. A driver that accepts syntax and ignores it would otherwise get native SQL that
  returns wrong rows.
- The probe table is the first with a row among `AP_Division`, `AR_Division`, `GL_Account`, `CI_Item`,
  then `AR_Customer` and the one-row `*_Options` tables. "The smallest non-empty table" would need a
  `COUNT(*)`, which is a full scan on ProvideX. With no row anywhere the baseline profile stays, uncached,
  and the next start probes again.
- Keywords: `ReadOnly=1` and `EnforceNullDate=1`, the values the server sends; `Compression=0` and
  `SSL=0`, which are probed for presence only.
- The profile is cached per `SQL_DRIVER_VER` in `<home>/capabilities`. Its notes keep the driver's
  reason for each failure, with literals redacted.
- Join checks are self-joins, so they use table aliases. A driver without table aliases fails them too.

**D26. ProvideX generator choices.**
- Select lists never carry aliases; output names come from the resolver.
- `ORDER BY` together with `TOP`, `LIMIT` or `OFFSET` never runs natively. The PxPlus grammar puts
  `LIMIT` before `ORDER BY`, so the driver may cut rows before sorting them.
- Negative numbers are written `(0 - n)` unless the probe passed negative literals.
- A `WHERE` on a LEFT-joined table goes hybrid, because Sage's driver returns inner-join rows (§5.2).
- Inner joins use `{IJ ...}` for one or two joins (nested for two), then `{oj ... INNER JOIN ...}` for one,
  then comma joins with the `ON` conditions moved into `WHERE`.
- `a = 1 OR a = 2` becomes `IN`; text `+` becomes `{fn CONCAT}`; `ORDER BY` uses select-list positions;
  T-SQL's implicit `CAST(x AS TEXT)` around string arguments is dropped.
- Every missing feature is recorded, not only the first, so `explain_query` can list them all.

**D27. Hybrid pushdown.**
- Only conditions that remove rows and test one table against constants are pushed: comparisons, `IN`
  lists, `BETWEEN`, `LIKE` and `NOT LIKE`, `IS NOT NULL`. `IS NULL` never is. Nor are `ON` conditions on
  the preserved side of a LEFT or RIGHT JOIN, any `ON` condition of a FULL JOIN, or an `OR` across columns.
- Without compound `AND` support only the first pushable condition is sent.
- DuckDB applies every condition again, so pushdown changes the cost of a query, never its result.
- A table read twice is pulled twice, each read with its own filters; tables inside `WITH` get theirs.
- Masked columns are constants before planning (D14), so they are never pulled.
- The local engine is an in-memory DuckDB per query: `memory_limit` 1 GB, spill under `<home>/tmp`,
  exact decimals, right-trimmed strings. Rows load as column arrays (`UNNEST`) at about 0.45 s per 100,000
  rows; `executemany` was far too slow.

**D28. Raw `dialect='providex'` (§9.1).** The `{oj}` and `{IJ}` escapes are opened so the guard and the
resolver can check the statement; sqlglot reads `{d}`, `{ts}`, `{t}` and `{fn}` itself. Table policy and
`FILTER_REQUIRED` apply. The text is then sent exactly as written, so masked columns can't be replaced
by a constant: naming one is `PERMISSION_DENIED`. `mode` doesn't apply (`MODE_IGNORED`). The statement is
logged as raw; the audit log itself is M4.

**D29. `FILTER_REQUIRED` on ProvideX (§13.1).** A table the overlay marks `requires_filter` needs a
pushable filter on a key column or a date column, in native and hybrid mode alike. The error suggests a
one-year date range. Premium doesn't apply it (D17).

**D30. Catalog, companies and profiles on ProvideX (§7.1, §8.1, §8.2).**
- Tables come from `SQLTables` (type `TABLE`). Columns come from one `SQLColumns` call, with per-table
  calls and then a result's description as fallbacks. Keys come from `SQLPrimaryKeys` where the probe
  passed it, else from the overlay. No row counts: a count is a full scan.
- Dates have no time part on ProvideX, so the catalog types them `date`.
- `SY_*` tables are left out of company catalogs, as on Premium, where they live in `MAS_SYSTEM`.
- Discovery connects with `companies.default`, the allow list, the `backend_map` entries and the
  `MAS_xxx` folders, in that order, and reads `SY_Company` (`CompanyCode`, `CompanyName`). If
  `SY_Company` can't be read, the folders are the list, without names. Whether it can on a real install
  is §19.1's open question; `tests/windows/test_m2_gate.py` reports it.
- Module settings are read with catalog functions plus one `SELECT` per `*_Options` table. Both backends
  now implement `read_options`, so the runtime no longer sends T-SQL for profiles.

**D31. ProvideX connections and calls (§4.3, §13.2, §14.2).**
- `autocommit=True` at connect: pyodbc's default sets an attribute the driver refuses with `HYC00`. The
  login timeout is 15 s; on `HYC00` the connect is tried once more without it. A Client/Server failure
  is retried once after 2 s.
- The string is §4.3's example byte for byte. `Prefix` and `ViewDLL` derive from the directory, using
  its own path separator. Values are braced only for `;`, `{`, `}` or edge spaces: Sage's own strings
  carry `Prefix=...\==\` unbraced. An optional keyword is appended once the probe accepts it.
- Each company has a pool of `per_company_concurrency.providex` connections. Idle connections are tagged
  with their connection string, so the after-hours profile or a newly accepted keyword retires older ones.
- Every driver call runs on a worker thread. At the deadline a watchdog cancels it. Where the driver
  enforces `SQL_ATTR_QUERY_TIMEOUT` itself, the watchdog waits 2 s longer so the driver's timeout wins.
  A call still running 5 s after the cancel is abandoned: its slot is freed at once so the pool can
  replace the connection, as §13.2 says, and the connection is closed when the call returns. For that
  while a company can have more than two connections.

**D32. ProvideX error mapping (§14.3).** Rules match message fragments, because Sage's driver seldom
reports native error numbers; the only one seen in a source is 1017, for `Unexpected extra token`. Each
fixture in `tests/fixtures/errors/providex.yaml` cites where its message was seen. `[FILEIO]Table is not
accessible` maps to `PERMISSION_DENIED`, with a hint naming its three known causes: module not set up,
ODBC Security role, share permissions. The emulator's own messages say "(emulator)" and match no rule.

**D33. The M2 gate: met on the emulator; the real driver needs the Windows runner.**
- `tests/sample_queries.py` holds 50 samples. Each runs through `run_query` and is compared with an
  oracle that reads every named table whole through the same driver and runs the query in DuckDB. The
  oracle shares no code with the generator, the pushdown or the hybrid engine.
- On the emulator all 50 match under the all-pass, PxPlus-full and Calvin Graham profiles, with 16, 16
  and 30 of them hybrid. 16 samples use SQL no Sage driver build runs natively (`WITH`, window
  functions, `EXCEPT`, `INTERSECT`, RIGHT and FULL joins, `IIF`, `REVERSE` and others), so at least 15
  go hybrid on any driver.
- `tests/windows/test_m2_gate.py` runs the probe, the same 50 samples against ABC, and checks the real
  driver's message texts. CI runs it when `SAGE_RUNNER` is set.
- Committed probe files are `tests/capabilities/<SQL_DRIVER_VER>-x64.json` and `-x86.json`, where §15.3
  has `<driver_ver>.json`. The 64-bit build has known gaps (§4.1) that the same version number would hide.
- Open: the runner itself (§19.1), and the 32-bit leg. duckdb has no win32 wheel (D3), so a 32-bit server
  process can't run hybrid queries. The executor takes any DB-API module (`odbc=`), so option 3 of D3, a
  32-bit helper process that only fetches rows, can plug in without touching the planner.

**D34. The M2 tool surface (§11).**
- `explain_query` takes `mode` and `dialect` as well as `sql` and `params`, so it explains the call the
  model is about to make.
- `refresh_capabilities` on a Premium company returns `status: not_applicable`, not an error.
- `sage://capabilities` lists each ProvideX backend's profile and every profile cached on this host.
  `sage://dialect-guide` is Markdown, with this server's driver profiles appended.
- `sage_status` reports `capabilities` as `not_probed`, `cached` or `probed`, plus a `capability_summary`.
  A ProvideX backend that has discovered companies isn't reported `unsupported_platform` off Windows,
  which is how the emulator runs it.
- `probe` is implemented in the CLI, and `doctor` checks the share as the service account, the C/S port,
  the Sage login, and a `Silent=1` connect plus one row per company.
- The eleven tools' descriptions and input schemas come to about 2,100 tokens.

**D35. Results on ProvideX.** The 1753-01-01 blank date is a Premium sentinel and isn't applied to
ProvideX, whose driver returns a blank date as NULL. `NULL_DATES_COERCED` there means the Null Date
retry (D24). Flags stay as Sage stores them (`'Y'`/`'N'`), where §7.2 says "flag a boolean": a result
that showed `true` for a column the model filters with `= 'Y'` would contradict its own SQL. The canned
views (M3) can present booleans.

**D36. Facts found while building M2.**
- sqlglot 30 stores `NOT LIKE` as `Like(negate=True)`. The generator first dropped the `NOT`, which in
  hybrid mode would have pulled the wrong rows. It is fixed and covered by goldens.
- sqlglot attaches a `WITH` clause to its `SELECT` after parsing, so a tree walk visits a marker inside
  `WITH` last. `?` values are now matched by the order the written SQL shows them.
- The CTE argument is `with_` in sqlglot 30, not `with`.
- In `mxODBC`'s report of MAS 90's `LIMIT 5` failure, `Unexpected extra token` carries native error 1017.
- A review of the pushdown rules found that a RIGHT JOIN's `ON` condition on its preserved (right) table
  was pushed into that table's read, dropping rows that must appear. It is fixed, with planner tests and
  a sample query the old rule fails against the oracle (102 rows instead of the test bed's 150).

## M1 — Catalog + Premium (Oct 29, 2026)

**D13. The SQL guard (§9.1) is an allowlist where the spec gives a denylist.** It walks the sqlglot AST
and rejects any write, DDL, session or transaction node, `INTO`, `NEXT VALUE FOR`, every table hint
(`NOLOCK` included: a dirty read is a choice for the DBA, not the model) and every query hint
(`OPTION (...)`). Functions sqlglot doesn't recognise must be on a short list of T-SQL built-ins, so an
unknown function fails closed. Variables are refused, `@@VERSION` included (the spec allows that one for
diagnostics; `test_connection` and `sage_status` already report versions). Three-part names, `#temp`,
`##temp`, schemas other than `dbo` and SQL over 100,000 characters are refused. A statement that doesn't
parse is classified from its tokens, so `SELECT 1; DROP TABLE x` reports `MULTI_STATEMENT` rather than a
parse error. The corpus has 214 attacks in 7 spellings each (1,498 cases), plus 46 statements that must
pass and four hypothesis properties. It found one real gap while being written: `##global` temp tables.

**D14. Policy (§9.2): masked values never leave the database.** A masked column may be selected only as a
whole top-level output; filtering, joining, sorting, grouping or computing on it is `PERMISSION_DENIED`,
since each of those reveals something about the values. The column is then sent as the constant `'***'`
instead of being fetched, which also stops `DISTINCT`, `UNION` and `ORDER BY` on its alias from leaking
row counts or order. A `SELECT *` inside a subquery that would pull a masked column is refused, which is
conservative; naming the columns avoids it. The mask pattern gains `socialsecurity`, which the spec's
pattern misses (`AP_Vendor.TaxpayerIDSocialSecurityNo` only matched through `taxpayer`). Two exceptions
ship: `CI_Item.RoutingNo` (a manufacturing routing) and `IM_Options.NextCountCardNo` (a counter). The
pattern still masks some harmless flags, such as `AP_Vendor.CreditCardVendor`; `mask_exceptions` in the
policy file lifts those per site. The policy file is now read with the strict YAML loader (D5), because a
repeated `deny_tables` key would otherwise silently drop the first list.

**D15. `BACKEND_UNREACHABLE` is a new error code.** §14.2 has `CS_ODBC_UNREACHABLE` for ProvideX but
nothing for a SQL Server that is down or unreachable (SQLSTATE 08001/08S01, or HYT00 at login). The
server retries it once after 1 s, like `LOCK_TIMEOUT`. Two error-mapping details came from real messages
(tests/fixtures/errors/sqlserver.yaml): unixODBC reports a missing driver as `01000 Can't open lib`, not
`IM002`; and for errors 207/208/209/229/230 the quoted text is always an object or column name, so it
stays in `detail`, while quoted text in any other message is redacted as a possible value.

**D16. SQL Server connections and logins.** Connections are DSN-less with `ApplicationIntent=ReadOnly`,
`APP=sage100-mcp`, autocommit, `SET LOCK_TIMEOUT 5000` and READ COMMITTED. Each company database has a
pool capped at `per_company_concurrency.sqlserver`; idle connections close after 10 minutes, and a
connection that raised is discarded. A keyring entry is named by the backend's `credential`, with the
login as its user name: `keyring set "sage100-mcp/sage_premium" sage100_mcp`. Without a keyring the server
reads `SAGE100_MCP_CRED_<NAME>_USER` and `_PASSWORD`, where NAME is the part after the slash, upper-cased.
Two config keys were added: `trust_server_certificate` (default false, for self-signed test servers) and
`connect_timeout_s` (default 15, §13.2). String parameters bind as nvarchar, pyodbc's default; against
varchar keys that can turn a seek into a scan, which M5's load test should measure.

**D17. Premium reads SQL as T-SQL first, whatever `dialect` says.** The T-SQL reader parsed every ANSI
probe identically to the ANSI reader, but the ANSI reader silently misreads T-SQL:
`DATEDIFF(day, a, b)` becomes `DATEDIFF(GETDATE, a, day)`. So `ansi` and `tsql` both read T-SQL first on
Premium; `providex` is `DIALECT_UNSUPPORTED` there. Premium has one mode, so `mode='hybrid'` runs natively
with a `MODE_IGNORED` warning. `FILTER_REQUIRED` (§13.1) is not applied on Premium: SQL Server has indexes
and the statement timeout, and the rule exists for ProvideX file scans; `requires_filter` still shows in
`describe_table`. ODBC escapes `{d}`, `{ts}` and `{t}` become `CAST`s, since SQL Server has no `DATE()`.

**D18. The result envelope (§11.2) in detail.**
- Decimals are strings at the column's scale (`"1500.00"`); integers stay JSON numbers.
- A datetime at midnight is a date (`"2024-12-01"`); others are ISO date-times.
- Strings lose trailing spaces only.
- Premium stores a blank date as 1753-01-01, so those values come back as null with
  `null_dates_coerced: true` and a `NULL_DATES_COERCED` warning that says to filter on `= '1753-01-01'`,
  not `IS NULL`. The spec defines the flag for ProvideX's `EnforceNullDate`; the meaning, "a null here is
  not a NULL in Sage", is the same.
- Each column carries `masked` and `source` (`Table.Column` for a plain catalog column).
- A computed column is named after its own text, such as `COUNT(*)`, where SQL Server would leave it
  nameless.
- Warnings are strings that start with their code (`"RESULT_TRUNCATED: ..."`).
- `TOP max_rows+1` is added only to a plain SELECT without TOP or OFFSET. Set operations are capped by the
  fetch alone, since wrapping them would move their ORDER BY.
- `next_cursor` waits for cursors in M4, so a truncated result says to filter, aggregate or raise
  `max_rows`.

**D19. Catalog, overlay and profiles (§7, §8.2).** Keys come from the primary key, else the clustered
index, else the overlay. Row counts come from `sys.partitions`. The live layer is cached as
`<home>/catalog/<CODE>.json` and reused while every module's `DataVersion` and the backend are unchanged;
the overlay and policy are applied on every load. A module counts as installed when its `<MOD>_Options`
table has a row. `DataVersion` is `decimal(4,2)`, so 7.30 reads as "7.3". A profile older than an hour is
re-read in the background while the old catalog keeps answering (`catalog_status: stale`). A failed
rebuild also keeps the old catalog and reports its error, and a first build that failed is retried by the
next call after 30 s. Tables the login can't read never reach `INFORMATION_SCHEMA`, so on the test bed
MAS_EMP shows 903 of its 908 tables, matching the policy. The overlay covers 46 core tables and is checked
against the pinned MAS_EMP schema on every test run, with no drift.

**D20. `scripts/premium_readonly.sql` denies tables, not columns.** It is generated from the policy file
by `scripts/premium_readonly.py`; a unit test fails when they disagree. It takes the login, password and
company codes as sqlcmd variables, can be run again safely, and never revokes a DENY the policy no longer
asks for. A column-level DENY was tried on the test bed and dropped: SQL Server then refuses even
`SELECT COUNT(*)` on the whole table (error 230). Masked columns stay protected by D14, so the database is
never asked for their values.

**D21. Three warning codes beyond §14.2.** `COLUMNS_PRUNED` (a `SELECT *` was narrowed to core columns,
§13.1), `LIMIT_CAPPED` (`max_rows` or `timeout_s` above the server's limit was lowered) and `MODE_IGNORED`
(D17).

**D22. The M1 tool surface.** M1 adds `list_companies`, `list_tables`, `describe_table`, `search_catalog`
and `run_query`, as §18 lists. It also adds `set_company`, which the §8.3 resolution order depends on, and
`refresh_catalog` and `test_connection`, which come almost free with the catalog and executor. The
resources are `sage://companies`, `sage://policy`, `sage://{company}/catalog` and
`sage://{company}/tables/{table}`. `sage_status` and `list_companies` take no `company`, because they
aren't about one company. `explain_query` waits for the planner (M2). Rate limits and the audit log
are M4 work in §18. The nine tools' descriptions and input schemas come to about 2,000 tokens. Their
output schemas are larger, but clients don't send those to the model.

**D23. How the M1 gates were met.**
- **Gate 1:** all 1,498 corpus cases are rejected (D13).
- **Test bed:** SQL Server 2025 Developer in the build container (CI uses a 2022 service container), built
  by `tests/premium_testbed.py` from the pinned MAS_EMP and MAS_PCC structures with synthetic rows.
- **Gate 2:** MAS_EMP's 908-table structure builds from the database in 1.3 s, bypassing the cache. The
  bar is 5 minutes.
- **Gate 3:** checked three ways.
  - On the test bed, 29 `premium` tests check exact figures, joins, CTEs, set operations, masking, lock
    and query timeouts, server-side cancellation, and a login that cannot write.
  - On the real MAS_EMP, through the Outova connector and reading counts only, 13 of 15 generated
    statements returned exactly what the plain T-SQL returned. The other two (a bracketed CTE name, and
    `DATEDIFF`'s `day`) are refused by the connector's own SQL pre-check, not by SQL Server; both pass on
    the test bed. Every vendor came back `***` for the masked tax ID, and the 143-column sales order
    header's `SELECT *` expansion ran.
  - `scripts/verify_premium.py` runs gates 2 and 3 with the server's own executor. Running it against
    MAS_EMP needs a read-only login (§19.1), which this build environment doesn't have.

## M0 — Scaffold (Oct 29, 2026)

**D1. MCP SDK pinned at `mcp==2.3.0`.** The latest 2.x on PyPI (Oct 2, 2026). The API matches §10.1:
`from mcp.server.mcpserver import MCPServer`. A tool annotated `-> Annotated[CallToolResult, Model]`
publishes `Model` as its `outputSchema` while returning its own text block, which is how every tool
returns `structuredContent` plus a compact text table (§11.1). Error results skip output validation.

**D2. Runtime dependencies arrive with the milestone that imports them.** `pyodbc` (M1), `sqlglot` (M1),
`keyring` (M1/M2) and `duckdb` (M2) are not in `pyproject.toml` yet, so M0 installs fast on every OS.
`pyodbc` 5.3 has wheels for Windows (x64 and x86), manylinux and macOS; on Linux and macOS it also needs
unixODBC at runtime.

**D3. Settled in M6 by option 3 (D67) — the 32-bit Windows build (§10.1) can't be built as specified.**
Neither dependency below ships win32 wheels:

- `duckdb`, the hybrid engine: no win32 wheel since 0.9.2 (Nov 2023); 2.0.0 has win_amd64 only.
- `cryptography`: win32 wheels stopped at 48.0.1. 49.0 (Jun 2026) and later have none. `mcp` needs it
  through `pyjwt[crypto]`.

Options:

1. Ship 64-bit only. Every Sage version since 2013 offers the 64-bit driver, and 2025 and later install it
   automatically. This loses the sites that hit the 64-bit gaps in §4.1 (ViewDLL view tables; C/S `Internal
   error` on second-file tables).
2. Keep a 32-bit build with `cryptography<49` pinned for that build only. It falls behind on security
   fixes, and it needs a hybrid engine other than DuckDB, such as the stdlib `sqlite3`.
3. Run the 32-bit driver out of process, for example a 32-bit helper that only fetches rows, behind the
   64-bit server.

Recommendation: decide at M2, once the probe shows whether the 64-bit gaps affect real sites. Until then
the lock targets win_amd64, manylinux x86_64 and macOS arm64 (`[tool.uv] required-environments`), and CI
has no x86 leg.

**D4. Config location off Windows.** §16.1 gives only `%ProgramData%\sage100-mcp`. Elsewhere, for example a
Premium-only host on Linux or macOS (§10.2), the home is `$XDG_DATA_HOME/sage100-mcp`, defaulting to
`~/.local/share/sage100-mcp`. `SAGE100_MCP_HOME` overrides the home and `SAGE100_MCP_CONFIG` the file.
`--config` works before or after the command.

**D5. The YAML loader keeps scalars as text and rejects duplicate keys.** YAML 1.1 turns company code `001`
into `1`, `YES` into `true` and `19:00` into `1140`. Pydantic does the typing instead. A repeated top-level
key would otherwise silently replace the first, for example dropping `companies.allow`. Unknown keys are
errors (`extra="forbid"`), so a password typed into the file is refused and not echoed.

**D6. `driver_options` accepts only `DirtyReads`, `BurstMode` and `CacheSize`.** These are matched
case-insensitively and given canonical casing. Every other §4.3 keyword is managed by the server (`Silent`,
`ReadOnly`, the Remote keywords and others), comes from the keyring (`UID`, `PWD`), or is never sent
(`SID`, `LegacyNULL` and others), and each is refused with that reason.

**D7. A missing or rejected config doesn't stop the server.** stdio clients rarely show a server's startup
error. So `stdio` starts with no backends (fail closed: nothing is reachable), logs the error to stderr,
and `sage_status` reports it. `doctor` exits 1 on the same config.

**D8. Error results.** `isError` is the `CallToolResult` flag. `structuredContent` holds the §14.1 fields
(`code`, `message`, `hint`, `detail`, `company`, `choices`, `retryable`) without repeating `isError`.
`retryable` is true only for `LOCK_TIMEOUT`, `RATE_LIMITED` and `CATALOG_BUILDING`; the server's own
retries (§14.2) happen before the model sees an error. An unexpected exception becomes `INTERNAL` with a
correlation id that matches the log; its text never reaches the client.

**D9. `sage_status` publishes the full enums now.** Backend `state`, `capabilities` and `catalog_status`
list every value the spec defines, so M1–M2 fill them in without changing the output schema. The
tools/list snapshot (`tests/contract/snapshots/tools_list.json`) makes any schema change deliberate.

**D10. Unimplemented CLI commands exist.** They exit with code 3 and name the milestone that delivers them.
`doctor` implements the M0 subset of §16.2: config, process bitness, and driver registration in both
registry views. It predicts IM014 before a connection is tried.

**D11. Contract tests negotiate 2026-07-28 with the client's `auto` mode.** That mode runs `server/discover`,
as a 2026-07-28 client does. A client pinned to the version skips discovery and gets no `server_info`.
`legacy` mode forces the 2025-11-25 initialize handshake.

**D12. CI.** `ubuntu-latest` and `windows-latest` (x64) run ruff, mypy, tiers 1/2/6 and the exit gate
(`uvx --from . sage100-mcp stdio` answers `sage_status`). The self-hosted Sage job (tiers 3–5) runs
`pytest -m windows` when the repository variable `SAGE_RUNNER` is `true`. That runner is the §19.1 open
question "a licensed Sage 100 2026 test install for CI", which is still open and is needed for M2's exit gate.
