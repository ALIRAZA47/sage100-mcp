# Setting up sage100-mcp

This guide goes from a clean machine to Claude answering a question about your Sage 100 data. The target
is 15 minutes with the prerequisites ready (SPEC.md §18, M6). Steps 1 to 4 are the same for everyone.
Step 5 is only for a server that other machines reach over HTTP.

- **Sage 100 Standard or Advanced (ProvideX).** Use a Windows machine on the Sage network, with the MSI.
- **Sage 100 Premium (SQL Server).** Use the MSI on Windows, or the Python package on Windows, Linux or
  macOS.

## Before you start

Have these ready. They take the longest, and none of them is sage100-mcp's to do.

**For Standard or Advanced (ProvideX):**
- A Windows Server 2019 or later, or Windows 10 or 11, machine on the Sage LAN.
- The Sage 100 workstation, installed on that machine from the Sage server's share
  (`\\SAGESRV\Sage\Sage 100\MAS90\Wksetup`). This installs the `MAS 90 4.0 ODBC Driver`.
  - Sage 2025 and later install the 64-bit build of the driver.
  - Older versions may install only the 32-bit build. That is fine: the server loads it through its 32-bit
    helper.
- A Sage user for the server, such as `mcp_reader`. In Role Maintenance, give its role read access to the
  modules people will ask about. With ODBC Security on, that is the role's ODBC Security tab.
- To run as a Windows service, a domain account such as `CORP\svc-sage100mcp`. It needs read access to the
  `MAS90` share, both share and NTFS permissions. It needs no admin rights.

**For Premium (SQL Server):**
- The SQL Server instance that holds the `MAS_` databases, such as `SQLSRV\SAGE`.
- A read-only login, which a DBA creates with
  [`scripts/premium_readonly.sql`](../scripts/premium_readonly.sql) (README, "Set up Sage 100 Premium").
- Off Windows: unixODBC, and Microsoft's ODBC Driver 18 for SQL Server.

**In every case:** an MCP client, such as Claude Desktop or Claude Code, on this machine or on a Mac that
reaches it.

## 1. Install

**Windows: the MSI.** Download `sage100-mcp-x64.msi` from the release and check it:

```powershell
Get-FileHash .\sage100-mcp-x64.msi -Algorithm SHA256     # compare with SHA256SUMS.txt in the release
Get-AuthenticodeSignature .\sage100-mcp-x64.msi          # Valid, when the release is signed
msiexec /i sage100-mcp-x64.msi                           # or /qn for no questions
```

The MSI does the following:
- Installs to `C:\Program Files\sage100-mcp`, and puts that folder on the system PATH.
- Creates the server's home, `C:\ProgramData\sage100-mcp`, open to Administrators and SYSTEM only.
- Includes the 32-bit driver helper, `odbc32\sage100-mcp-odbc32.exe`.

Open a new terminal so it sees the new PATH, then check:

```powershell
sage100-mcp --version
```

**Any OS: the Python package.**

```bash
uv tool install sage100-mcp              # or: pipx install sage100-mcp
uv tool install ./sage100_mcp-1.0.0rc1-py3-none-any.whl   # from a release file, before it is on PyPI
```

Some ProvideX installs have only Sage's 32-bit driver. The Python package has no frozen helper, so install
a 32-bit Python 3.9 or later with `pip install pyodbc`. Then name it in the backend's `helper` setting (step
2; see also [decisions D67](decisions.md)).

## 2. Configure: `sage100-mcp init`

On Windows, run it in a terminal opened as Administrator: it writes to `C:\ProgramData\sage100-mcp`.

```powershell
sage100-mcp init
```

`init` asks the following:
- The edition.
- For ProvideX, the `MAS90` folder. It offers the folder from Sage's own `SOTAMAS90` data source, so
  usually you just confirm it. If the server will run as a service, use the UNC path
  (`\\SAGESRV\Sage\Sage 100\MAS90`): a service has no mapped drives.
- For Premium, the SQL Server instance.
- The Sage user, or the SQL login, and its password.
- The default company.

`init` then does four things:
1. Tests the login.
2. Lists the companies the login can open.
3. Stores the login in Windows Credential Manager (the keyring) under `sage100-mcp/sage` or
   `sage100-mcp/premium`.
4. Writes `config.yaml`. It refuses to write a config the server would reject.

For a script, give every answer as a flag and the password in an environment variable:

```powershell
$env:SAGE_PW = "..."
sage100-mcp init --yes --type providex --directory "\\SAGESRV\Sage\Sage 100\MAS90" --user mcp_reader --password-env SAGE_PW --company ABC
sage100-mcp init --yes --type sqlserver --server "SQLSRV\SAGE" --user sage100_mcp --password-env SAGE_PW --company ABC
```

**Whose keyring.** Credential Manager keeps a login per Windows account. The login `init` stores can be
read only by the account that ran `init`:
- **Claude on this machine, as you.** Run `init` as yourself. That is the case above.
- **The Windows service.** The service's own account must hold the login. Run `init` as that account,
  after step 5 has given it access to the server's home (decisions D70).

**No keyring.** Linux servers without a desktop session, and containers, have no keyring. There `init` says
which two variables to set in the server's environment, such as `SAGE100_MCP_CRED_PREMIUM_USER` and
`SAGE100_MCP_CRED_PREMIUM_PASSWORD`. It still writes `config.yaml`.

[`config.example.yaml`](config.example.yaml) shows every setting. These two matter for ProvideX:
- `driver_bitness: auto` (the default) loads Sage's 64-bit driver in the server. Where only the 32-bit
  driver is installed, it loads that one through the 32-bit helper. `32` or `64` forces one build.
- `helper`, for a Python package install only, names the 32-bit `python.exe` that has pyodbc.

## 3. Check: `sage100-mcp doctor`

```powershell
sage100-mcp doctor
```

Every line should read `OK`, `INFO` or `SKIP`. `FAIL` lines say what to fix. Doctor checks the following:
- The config.
- That the driver can be loaded: the bitness, or the 32-bit helper.
- The Sage share, as the account running doctor.
- A `Silent=1` connection and a one-row read for each company.
- What the probe found the driver accepts.
- For Premium, the login and database access.
- The clock.
- The HTTP and TLS settings.
- The audit folder.

`sage100-mcp probe` prints the ProvideX driver's capabilities check by check. Common failures:

| Doctor says | Do this |
| --- | --- |
| `MAS 90 4.0 ODBC Driver: not registered` | Run the Sage 100 workstation setup from the Sage server's share on this machine |
| `registered for 32-bit only ... the 32-bit helper is not installed` | Reinstall from the MSI, or set `helper` to a 32-bit `python.exe` with pyodbc |
| `... is not reachable as <account>` | Use the UNC path. Give that account read access to the share. |
| `AUTH_FAILED` | Check the Sage user and password, and that the user isn't locked in User Maintenance. Run `init --force` to store the login again. |
| `COMPANY_NOT_FOUND` | The company code is not set up in this Sage install |
| `CS_ODBC_UNREACHABLE` | Start the Sage 100 Client/Server ODBC service (PVXIOSVR) on the Sage server, and open its port (20222) |

## 4. The first answer

**Claude Desktop on this machine.** Add the server to `claude_desktop_config.json` (Settings, Developer,
Edit Config), then restart Claude Desktop:

```json
{ "mcpServers": { "sage100": { "command": "sage100-mcp", "args": ["stdio"] } } }
```

**Claude Code on this machine:**

```powershell
claude mcp add sage100 -- sage100-mcp stdio
```

Then ask: *"Which Sage companies can you see, and what is ABC's AR aging as of today?"* Claude calls
`list_companies`, then `ar_aging`. The first call for a company builds its catalog, which takes a few
seconds. Later answers come from the cached catalog.

## 5. Optional: a server for other machines (Windows service, HTTP and TLS)

Run these in an Administrator terminal on the server.

```powershell
$env:SVC_PW = "..."                                   # the service account's Windows password
sage100-mcp service install --account CORP\svc-sage100mcp --password-env SVC_PW
runas /user:CORP\svc-sage100mcp /profile "sage100-mcp init --force"   # the login, in the service account's keyring
sage100-mcp token create --principal ann-macbook      # prints the token once; give it to Ann
sage100-mcp service start
sage100-mcp service status
```

`service install` does the following:
- Registers the service to start automatically once the network is up, and to restart after a failure.
- Grants the account "Log on as a service".
- Gives the account modify access to `C:\ProgramData\sage100-mcp`.

The service runs `sage100-mcp serve` and logs to `C:\ProgramData\sage100-mcp\logs\sage100-mcp.log`, with
passwords and tokens redacted. Remove it with `sage100-mcp service remove`.

`runas` opens a window as the service account, where `init` asks its questions again. Use the UNC path.
If policy bars that account from logging on interactively, a domain admin can allow it for the setup.
Group managed service accounts can't use runas at all (decisions D70).

Set `http.bind`, and TLS or a proxy in front of the server, in `config.yaml`.
[docs/remote.md](remote.md) covers the following:
- The proxy and TLS setups.
- OAuth instead of static tokens.
- `scripts/remote_check.py`, which checks the connection from the Mac.
- Adding the server to Claude on the Mac.

## Upgrade and remove

- **Upgrade.** Install the new MSI over the old one. The service, `config.yaml`, the tokens and the audit
  files stay. Restart the service with `sage100-mcp service stop`, then `service start`.
- **Remove.** Run `sage100-mcp service remove` first, then uninstall sage100-mcp in Settings, Apps.
  `C:\ProgramData\sage100-mcp` stays, because it holds your config and audit trail. Delete it yourself if you
  are done with them.

## Timing the M6 exit gate

The gate is a clean-VM install that reaches a first answer within 15 minutes, following this guide. On a
fresh Windows VM, with the prerequisites above in place, time each step:

| Step | Minutes |
| --- | --- |
| 1. Download, check and install the MSI | |
| 2. `sage100-mcp init` | |
| 3. `sage100-mcp doctor`, all OK | |
| 4. Claude configured, and the first answer back | |
| **Total** (gate: under 15) | |

`scripts/clean_install.py` times the automated part of this path on any OS against the Premium test bed:
wheel, fresh environment, `init`, `doctor`, and a first answer over stdio. It doesn't replace the
Windows run.
