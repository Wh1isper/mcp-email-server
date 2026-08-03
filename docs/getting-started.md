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
newest published PyPI package without a permanent installation.

## Version availability

This page documents the Local Email App V2 contract implemented by this source
tree: the embedded React management UI, SQLite-backed managed catalogs, the
`config` and `account` CLI commands, and a mail-only MCP catalog. Managed secrets
use the same private SQLite database by default on Linux and Windows; macOS uses
the system keyring.

PyPI 0.16.0 and earlier do not contain that contract. Those releases use the
legacy Gradio/TOML editor and MCP still exposes `add_email_account`. For a newer
release, verify that its release notes include “Local Email App V2” before using
the V2 instructions below. The `@latest` selector chooses the newest published
package; it does not select this source branch.

To run V2 from this checkout:

```bash
uv sync
uv run mcp-email-server ui
```

For a published release whose notes include Local Email App V2:

```bash
uvx mcp-email-server@latest ui
```

Use the same source checkout or published release for both `ui` and `stdio`. Do
not pair a V2 managed catalog with a pre-V2 MCP process.

## Upgrading to Local Email App V2

PyPI 0.16.0 and earlier expose the historical `add_email_account` MCP tool,
which accepts account credentials and writes legacy TOML configuration. Local
Email App V2 intentionally removes that tool; it is not renamed or replaced by
another MCP management tool. Account and credential changes must be completed
by the user through the authenticated loopback UI or interactive CLI.

Before upgrading, remove prompts, automation, and client allowlist entries that
invoke `add_email_account`. After upgrading, restart each MCP client so it
refreshes `tools/list`; a stale caller will no longer find that tool.

Existing TOML accounts remain available in `legacy` mode. To adopt managed mode,
initialize the migration destination, preview and explicitly apply `config
import-legacy`, test the accounts, and restart the MCP client when requested.
Preparation keeps legacy selected. A complete successful import automatically
selects managed mode when every source account type is supported; a failure or
unsupported provider keeps legacy selected. Import never modifies the source
TOML or its legacy keyring entries. The preview uses the effective
legacy view, so a complete environment account and environment policy overrides
are included with the same replacement/precedence rules used by legacy runtime.
Credential values remain absent from preview and are read only during confirmed
apply. Do not pass them through MCP or chat.

## Configure an account with the UI

Run one of the V2 commands in [Version availability](#version-availability).

The foreground command binds exactly to `127.0.0.1` on an ephemeral port and
opens one process-specific browser link. The link contains a one-time token in
its URL fragment; the frontend removes the fragment before exchanging it for a
local session. Keep the terminal open while using the UI. Use `--no-open` to
suppress browser launch or `--port PORT` to request a fixed loopback port.

On first use:

1. after the one-time browser authentication, let the UI prepare
   `managed.sqlite3` in the private directory shared by the legacy source and
   its separate bootstrap sidecar; this authenticated
   POST happens automatically only for a truly empty installation, while a
   detected legacy source requires an explicit **Import existing settings**
   preparation and review;
2. stay in **Email accounts** and choose **Add your first account**. Enter the
   email address and password. The UI derives a sender name and account nickname,
   fills known Google, Microsoft, iCloud, Yahoo, Fastmail, and Zoho connection
   settings, and otherwise suggests `imap.<email-domain>`. Review or override the
   editable server, login, port, security, and certificate details when needed.
   Outgoing mail and Sent-folder options remain optional;
3. use **Password** on the saved account to rotate or remove that account's saved
   password. A failed save leaves the current password authority unchanged.
   Provider connectivity testing is intentionally not
   available in the Web UI;
4. under **Settings & help**, add each allowed recipient needed for sending and
   each optional allowed-sender pattern as an individual item. No recipients
   disables sending; no senders leaves reading unrestricted;
5. saved complete accounts are immediately usable by managed runtime; there is
   no catalog activation or second save. Incomplete accounts remain visible in
   diagnostics but do not hide complete accounts;
6. restart every MCP client process after the UI reports that restart is
   required.

The UI has only two primary destinations: **Email accounts** for ordinary setup
and **Settings & help** for importing earlier settings, sending/attachment safety,
and bounded troubleshooting checks. Ordinary labels and errors use task language;
storage and concurrency terms are kept out of the primary workflow. Optional
settings are loaded only when their disclosure
is opened. On Linux and Windows, managed credentials default to the private
`managed_secret` table in the managed SQLite database. macOS uses the
operating-system keyring. Managed mode never falls back to TOML plaintext. The
interface is not a mail
client and never exposes message content. The same cleanup and headless
operations remain available through the managed CLI below.

## Configure an account with the managed CLI

For an explicit SQLite-backed catalog, install the command at a stable path and
run the direct workflow:

```bash
mcp-email-server config init \
  --database ~/.config/mcp-email-server/catalog.sqlite3
mcp-email-server account add work \
  --email john@example.com \
  --full-name "John Doe" \
  --imap-host imap.example.com
mcp-email-server account test work incoming
```

The account command reads the password through user-controlled terminal input
without placing it in argv. `account test` is the retained low-level,
agent-readable connectivity diagnostic; running it does not authorize any other
management operation. Managed mode requires the platform filesystem-security profile described in
[Security](security.md). POSIX uses owner/mode, no-follow, identity, and locking
primitives. Windows requires an ordinary local fixed NTFS drive-letter path and
uses handle-bound reparse/identity/DACL checks to protect both catalog state and
managed secrets. Managed mode does not fall back to legacy TOML plaintext or
weaker filesystem checks. Fresh initialization selects managed immediately unless existing v1
configuration needs reviewed import. Restart the MCP client when `config status`
reports that it is required. See
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
   `list_available_accounts`; select an entry with `can_receive=true` and require
   `can_send=true` before sending. If the list is empty, the agent should hand
   setup back to you and must not ask for a credential.
2. Ask it to list recent messages for the configured account. This calls
   `list_emails_metadata`.
3. If SMTP is configured through the managed workflow, make a non-destructive
   connectivity check with `mcp-email-server account test ACCOUNT outgoing`
   before asking the client to send. This CLI diagnostic remains available even
   though the Web UI has no Test connection action or route. Add the intended
   address to the allowed-recipient policy first; an empty recipient collection
   disables sending. `send_email` is always present in the static MCP tool
   catalog.

If the account is listed but a mail operation fails, check the IMAP or SMTP
host, port, TLS mode, username, and password. See
[Troubleshooting](troubleshooting.md) for common failures.

## Install a published V2 release permanently

Use this path only for a release whose notes state that it includes Local Email
App V2. Instead of `uvx`, install that package into a managed environment:

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

On Windows, use `where mcp-email-server` instead. Managed storage must remain on
a local fixed NTFS drive under a validated parent directory; do not place the
catalog or configuration authority directly in the volume root or on a UNC path,
mapped network drive, FAT/exFAT volume, device namespace, or alternate data
stream.

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
