# Getting Started

This guide configures one email account and connects it to an MCP client over
stdio. See [Configuration](configuration.md) for headless environments,
multiple accounts, and advanced email server settings.

## Requirements

- Python 3.11 or later.
- IMAP credentials for the email account.
- SMTP credentials if the account must send email.
- An MCP-compatible client.

[`uv`](https://docs.astral.sh/uv/) is recommended because `uvx` can run the
latest package without a permanent installation.

## Configure an account with the UI

Run:

```bash
uvx mcp-email-server@latest ui
```

The foreground command binds exactly to `127.0.0.1` on an ephemeral port and
opens one process-specific browser link. The link contains a one-time token in
its URL fragment; the frontend removes the fragment before exchanging it for a
local session. Keep the terminal open while using the UI. Use `--no-open` to
suppress browser launch or `--port PORT` to request a fixed loopback port.

On first use:

1. initialize a staging catalog at a private local path such as
   `~/.config/mcp-email-server/catalog.sqlite3`;
2. add an account with its IMAP endpoint and optional SMTP endpoint;
3. enter each credential in the password field;
4. test connectivity, review policy and health, then validate and activate;
5. explicitly select managed mode and restart every MCP client process.

Managed credentials require a usable operating-system keyring and never fall
back to plaintext. The UI also supports account lifecycle, credential rotation
and repair, policy, legacy-import preview/apply, doctor, and index health. It is
not a mail client and never exposes message content. The same recovery and
headless operations remain available through the managed CLI below.

## Configure an account with the managed CLI

For an explicit SQLite-backed catalog, install the command at a stable path and
run the staged workflow:

```bash
mcp-email-server config init \
  --database ~/.config/mcp-email-server/catalog.sqlite3
mcp-email-server account add work \
  --email john@example.com \
  --full-name "John Doe" \
  --imap-host imap.example.com
mcp-email-server account test work incoming
mcp-email-server config activate
mcp-email-server config select managed
```

The account command prompts for the password without placing it in argv.
Managed mode requires a working operating-system keyring plus the POSIX
owner/no-follow/locking primitives described in [Security](security.md), and does
not fall back to plaintext or weaker filesystem checks. Restart the MCP client
after selection. See
[Managed CLI setup](configuration.md#managed-cli-setup) for SMTP, stdin,
diagnostics, disablement, and switching back to legacy mode.

## Configure the MCP client

Add this server definition to the MCP client:

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"]
    }
  }
}
```

Restart the client after changing its configuration.

The explicit JSON configuration above works with Claude Desktop and other
clients that use the same `mcpServers` format. Account and credential management
is intentionally absent from MCP; use the local UI or your own terminal.

## Verify the connection

After restarting the client:

1. Ask it to list available email accounts. This calls
   `list_available_accounts`.
2. Ask it to list recent messages for the configured account. This calls
   `list_emails_metadata`.
3. If SMTP is configured through the managed workflow, make a non-destructive
   connectivity check with `mcp-email-server account test ACCOUNT outgoing`
   before asking the client to send. `send_email` is always present in the
   static MCP tool catalog.

If the account is listed but a mail operation fails, check the IMAP or SMTP
host, port, TLS mode, username, and password. See
[Troubleshooting](troubleshooting.md) for common failures.

## Install the package permanently

Instead of `uvx`, install the package into a managed environment:

```bash
pip install mcp-email-server
mcp-email-server ui
```

Then configure the client to invoke the installed executable:

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "mcp-email-server",
      "args": ["stdio"]
    }
  }
}
```

If the executable is not on the client's `PATH`, replace
`mcp-email-server` with the absolute path returned by:

```bash
which mcp-email-server
```

On Windows, use `where mcp-email-server` instead.

## Configure without the UI

For containers, CI, and headless systems, pass account settings as environment
variables in the MCP server definition. A minimal IMAP account looks like this:

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_ACCOUNT_NAME": "work",
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "john@example.com",
        "MCP_EMAIL_SERVER_PASSWORD": "your-password",
        "MCP_EMAIL_SERVER_IMAP_HOST": "imap.example.com"
      }
    }
  }
}
```

Add `MCP_EMAIL_SERVER_SMTP_HOST` to enable sending. See the complete
[environment variable reference](configuration.md#environment-variable-reference)
before deploying credentials this way.

The password in this example remains plaintext in the MCP client configuration
and process environment; `credential_storage` does not protect it. Prefer the
client, CI, or container platform's secret injection mechanism. If a literal
value is unavoidable, restrict the configuration file's permissions and keep
it out of version control and diagnostic output. Environment-composited legacy
accounts are runtime compatibility inputs; migrate them explicitly rather than
pasting their secrets into chat or asking an MCP client to manage accounts.

## Next steps

- [Configure multiple accounts or advanced TLS settings](configuration.md)
- [Review the available MCP tools](tools.md)
- [Apply recipient or sender allowlists](security.md)
- [Run an HTTP transport](transports.md)
