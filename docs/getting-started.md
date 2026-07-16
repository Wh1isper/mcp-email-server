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

The command opens a local configuration interface in the browser. Add an email
account with the following information:

- A unique account name used by MCP tools, such as `work`.
- The display name and email address.
- IMAP host, port, username, and password.
- Optional SMTP host, port, username, and password.

Leave the SMTP host empty to create an IMAP-only account. IMAP-only accounts can
still modify mailboxes; see [IMAP-only accounts](guides.md#imap-only-accounts).

The UI covers common implicit-TLS IMAP and SMTP configurations. Use TOML or
environment variables for advanced settings such as IMAP STARTTLS, custom
certificate verification, allowlists, or a custom Sent mailbox.

By default, settings are stored at:

```text
~/.config/mcp-email-server/config.toml
```

Credentials use the operating system keyring when a usable backend is
available. See [Credential storage](security.md#credential-storage).

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

The UI also includes an installer for Claude Desktop on supported desktop
platforms. The explicit JSON configuration above works with Claude Desktop and
other clients that use the same `mcpServers` format.

## Verify the connection

After restarting the client:

1. Ask it to list available email accounts. This calls
   `list_available_accounts`.
2. Ask it to list recent messages for the configured account. This calls
   `list_emails_metadata`.
3. If SMTP is configured, ask which email tools are available and confirm that
   `send_email` is present.

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
it out of version control and diagnostic output. Do not use the UI or
`add_email_account` in the same secret-bearing process unless persisting the
effective environment settings is intended.

## Next steps

- [Configure multiple accounts or advanced TLS settings](configuration.md)
- [Review the available MCP tools](tools.md)
- [Apply recipient or sender allowlists](security.md)
- [Run an HTTP transport](transports.md)
