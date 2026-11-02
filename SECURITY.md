# Security policy

sage100-mcp reads accounting data from Sage 100 and hands it to an AI client, so we treat security problems
as the most urgent kind of bug.

## Reporting a vulnerability

Report it privately, through GitHub's
[private vulnerability reporting](https://github.com/ALIRAZA47/sage100-mcp/security/advisories/new)
(the Security tab, then "Report a vulnerability"). Never put it in a public issue, a pull request or a
discussion.

Include:
- the version (`sage100-mcp --version`) and the backend: ProvideX, the 32-bit helper, or Premium;
- what an attacker can do, and what they need first: a token, a local account, a crafted row in Sage, or
  something else;
- the steps, a tool call, or a query that shows it.

Don't send real company data. The test bed in `tests/premium_testbed.py` and the driver emulator in
`tests/emulator` can reproduce most problems with synthetic rows.

The maintainer replies in the advisory and fixes the problem in private. They agree a disclosure date with
you, and credit you in the advisory unless you'd rather not be named.

## Supported versions

| Version | Supported |
| --- | --- |
| The latest release (1.0.0rc1 today) | Yes |
| Anything older | No: upgrade first |

## What counts

The server promises the following (README, "What the server will and won't do"). Anything that breaks one
of these promises is a vulnerability.

- **It never writes to Sage.** Any statement it lets through that writes, or that runs a procedure, is a
  vulnerability, whatever the login's rights: INSERT, UPDATE, DELETE, `{call}`, BOI, or anything else.
- **The guard and policy hold.** Reaching a hidden table, unmasking a masked column, or filtering, joining
  or sorting on one, is a vulnerability.
- **No secrets leave.** A password, a token, or a masked value in a log line, an audit file, an error's
  `detail`, or the 32-bit helper's stderr is a vulnerability.
- **Remote callers are checked.**
  - A request must not get past the bearer token or OAuth checks.
  - A token limited to some companies must reach no other.
  - One caller must not use another's cursor or active company.
  - A foreign `Origin` or `Host` must be refused.
- **The 32-bit helper keeps secrets out of sight.**
  - Its connection string never goes on a command line, and it never inherits the server's credentials.
  - Its protocol accepts nothing that turns into anything but a driver call.

## What doesn't count

- **Faults in Sage's own ODBC drivers, in SQL Server, or in an MCP client.** Report those to their vendors.
  Tell us too if the server should defend against them.
- **What an MCP client does with rows the server returned.** The tool descriptions flag rows as data
  written by people. Text in a customer name that looks like an instruction is a risk for the client to
  handle (README).
- **A deployment the guides warn against.** For example a server reachable without TLS, a token shared
  between people, or a Sage login with more than read access.
