# Remote access over Streamable HTTP

Mac and Linux clients reach sage100-mcp over Streamable HTTP (SPEC.md §10.2). `sage100-mcp serve` runs
the same tools as `stdio`, behind TLS, a bearer token or an OAuth access token, and Origin and Host checks.
[decisions.md](decisions.md) D50 to D57 record the details.

## On the server

The server runs next to Sage on Windows, or on any OS for a Premium-only site. Pick one of two layouts:

- **Behind a TLS proxy (recommended).** Bind `127.0.0.1:8765` and let Caddy or IIS terminate TLS. Set
  `public_url` to the URL clients use. The proxy must pass paths through unchanged:

  ```yaml
  http:
    bind: 127.0.0.1:8765
    public_url: https://sage-mcp.corp.example/mcp
  ```

  ```text
  # Caddyfile: Caddy obtains and renews the certificate itself
  sage-mcp.corp.example {
      reverse_proxy 127.0.0.1:8765
  }
  ```

- **TLS directly.** Bind the LAN address and give the certificate and key, both PEM:

  ```yaml
  http:
    bind: 10.0.0.5:8765
    tls_cert: C:\ProgramData\sage100-mcp\tls\server.crt
    tls_key: C:\ProgramData\sage100-mcp\tls\server.key
  ```

`serve` refuses to listen beyond loopback without TLS, and refuses `0.0.0.0` without a `public_url`.

Issue one token per person or machine. Only its hash is stored, so copy the printed token now:

```bash
uv run sage100-mcp token create --principal ann-macbook
uv run sage100-mcp token create --principal month-end-bot --companies ABC   # sees only ABC
uv run sage100-mcp token list
uv run sage100-mcp token revoke 3f9a1c2e                                    # takes effect at once
```

Then check the setup and start the server:

```bash
uv run sage100-mcp doctor     # adds: TLS certificate expiry, token count or the OAuth issuer's keys
uv run sage100-mcp serve
```

### OAuth instead of static tokens

With an identity provider that issues JWT access tokens, set:

```yaml
http:
  auth: oauth
  oauth: {issuer: "https://login.corp.example", audience: "sage100-mcp"}
  public_url: https://sage-mcp.corp.example/mcp
```

Tokens must be signed with the issuer's published keys, carry `aud: sage100-mcp` and the `sage:read`
scope, and may carry `sage:company:ABC` scopes to limit the companies. The server publishes its Protected
Resource Metadata at `https://sage-mcp.corp.example/.well-known/oauth-protected-resource/mcp`, which tells
clients where to sign in.

## On the Mac

1. Run the pre-flight from a checkout of this repository. It prints counts and codes only:

   ```bash
   export SAGE100_MCP_TOKEN='s1mcp_...'
   uv run python scripts/remote_check.py https://sage-mcp.corp.example/mcp
   ```

   Add `--cafile corp-ca.pem` when the certificate comes from a private CA or is self-signed, and
   `--company ABC` to check a particular company. Every line should read `PASS`.

2. Add the server to Claude Code:

   ```bash
   claude mcp add --transport http sage100 https://sage-mcp.corp.example/mcp --header "Authorization: Bearer $SAGE100_MCP_TOKEN"
   ```

   For a private CA, start Claude Code with `NODE_EXTRA_CA_CERTS=/path/to/corp-ca.pem` set.

3. In Claude Code, `/mcp` should list `sage100` as connected. Ask it to call `sage_status`.

## The M4 exit gate: the eval set over TLS

SPEC.md §18 closes M4 when a Mac Claude Code client completes the eval set over TLS. The eval set
(§15.6, `evals/tasks.yaml`) holds 40 questions with golden answers from the ABC sample company, so it needs
a Sage install with ABC and arrives with M5's model evals. Once it does:

1. Serve ABC as above, and pass `remote_check.py` from the Mac.
2. Ask Claude Code each question in a fresh conversation, and record whether the answer matches the
   golden answer, how many tool calls it took, and any errors.
3. Note it in decisions.md D57. M5 sets the bar for scores (§15.6); M4 needs the run to complete over
   TLS without transport or authentication errors.

## Operating notes

- **Rate limit:** 30 tool calls a minute per principal (`limits.rate_per_min`). `RATE_LIMITED` says how
  many seconds to wait.
- **Active company:** `set_company` is per principal and lapses after 8 hours. Two clients sharing a
  token share it.
- **Paging:** a truncated `run_query` result carries `next_cursor`. Cursors last 10 minutes and work only
  for the principal that received them.
- **Audit:** every tool call is written to `<home>\audit\YYYY-MM-DD.jsonl`, kept for 90 days. A line holds
  the principal, client, tool, argument names, company, the SQL with literals redacted, rows, time and
  outcome. It never holds argument values or tokens.
