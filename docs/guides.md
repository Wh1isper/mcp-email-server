# Guides

> **Version scope:** Managed-mode workflows in this page are Local Email App V2
> behavior. See [Version availability](getting-started.md#version-availability)
> before using them with a PyPI installation.

These examples cover common configurations that need more control than the
basic UI provides.

## IMAP-only accounts

Remove SMTP configuration when an account must not send email:

```toml
[[emails]]
account_name = "archive"
full_name = "Archive Reader"
email_address = "archive@example.com"

[emails.incoming]
user_name = "archive@example.com"
password = "your-password"
host = "imap.example.com"
port = 993
use_ssl = true
start_ssl = false
verify_ssl = true
```

Or configure one through environment variables without
`MCP_EMAIL_SERVER_SMTP_HOST`:

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_ACCOUNT_NAME": "archive",
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "archive@example.com",
        "MCP_EMAIL_SERVER_PASSWORD": "your-password",
        "MCP_EMAIL_SERVER_IMAP_HOST": "imap.example.com"
      }
    }
  }
}
```

`send_email` remains in the static MCP tool list, but calling it for this
account fails its SMTP capability check before provider access. IMAP mutation
tools remain available, so this is not a strict read-only mode. To limit
mutations, also constrain which MCP tools the client may call or run the server
with an account whose provider permissions are read-only.

## Safe delete and move behavior

Message-scoped deletion never uses mailbox-wide IMAP `EXPUNGE`, which would
remove every message already marked `\Deleted`, including messages selected by
another email client. The server uses `UID EXPUNGE` only when the provider
advertises the RFC 4315 `UIDPLUS` capability.

If a provider lacks `UIDPLUS`, `delete_emails` reports the requested messages as
failed before changing their flags. When the provider also lacks native `MOVE`,
`move_emails` rejects its COPY-and-delete fallback before copying anything. Use
the provider's own client or an IMAP server that supports `MOVE` or `UIDPLUS`.

## ProtonMail Bridge and self-signed TLS

Local bridges commonly expose IMAP through STARTTLS with a locally issued
certificate. A typical environment configuration is:

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_ACCOUNT_NAME": "protonmail",
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "john@example.com",
        "MCP_EMAIL_SERVER_PASSWORD": "bridge-password",
        "MCP_EMAIL_SERVER_IMAP_HOST": "127.0.0.1",
        "MCP_EMAIL_SERVER_IMAP_PORT": "1143",
        "MCP_EMAIL_SERVER_IMAP_SSL": "false",
        "MCP_EMAIL_SERVER_IMAP_START_SSL": "true",
        "MCP_EMAIL_SERVER_IMAP_VERIFY_SSL": "false",
        "MCP_EMAIL_SERVER_SMTP_HOST": "127.0.0.1",
        "MCP_EMAIL_SERVER_SMTP_PORT": "1025",
        "MCP_EMAIL_SERVER_SMTP_SSL": "false",
        "MCP_EMAIL_SERVER_SMTP_START_SSL": "true",
        "MCP_EMAIL_SERVER_SMTP_VERIFY_SSL": "false"
      }
    }
  }
}
```

Equivalent TOML:

```toml
[[emails]]
account_name = "protonmail"
full_name = "John Doe"
email_address = "john@example.com"

[emails.incoming]
user_name = "bridge-username"
password = "bridge-password"
host = "127.0.0.1"
port = 1143
use_ssl = false
start_ssl = true
verify_ssl = false

[emails.outgoing]
user_name = "bridge-username"
password = "bridge-password"
host = "127.0.0.1"
port = 1025
use_ssl = false
start_ssl = true
verify_ssl = false
```

Use the exact credentials and ports shown by the local bridge. Disable
certificate verification only for a bridge running on a trusted local endpoint.

## Separate IMAP and SMTP credentials

Some providers or bridges issue separate credentials. With environment
variables, keep the required shared password and override each protocol:

```bash
MCP_EMAIL_SERVER_EMAIL_ADDRESS='john@example.com'
MCP_EMAIL_SERVER_USER_NAME='john@example.com'
MCP_EMAIL_SERVER_PASSWORD='required-shared-fallback'
MCP_EMAIL_SERVER_IMAP_USER_NAME='imap-user'
MCP_EMAIL_SERVER_IMAP_PASSWORD='imap-password'
MCP_EMAIL_SERVER_IMAP_HOST='imap.example.com'
MCP_EMAIL_SERVER_SMTP_USER_NAME='smtp-user'
MCP_EMAIL_SERVER_SMTP_PASSWORD='smtp-password'
MCP_EMAIL_SERVER_SMTP_HOST='smtp.example.com'
```

The generic `MCP_EMAIL_SERVER_PASSWORD` currently remains required to create an
environment-provided account, even when both protocol-specific passwords are
set.

In TOML, set `user_name` and `password` independently in the incoming and
outgoing tables.

## Save messages to a custom Sent folder

If Sent folder auto-detection does not select the provider's folder, set it
explicitly:

```toml
[[emails]]
account_name = "work"
save_to_sent = true
sent_folder_name = "INBOX.Sent"
```

Before choosing a value, call `list_mailboxes` and inspect the returned names
and flags. Set `save_to_sent = false` if the provider already saves SMTP mail
and a second IMAP append would create duplicates.

## Save a draft

Call `save_to_mailbox` with the account and message fields. The default mailbox
is `Drafts`, and the default flags are `\Draft` and `\Seen`.

Conceptual MCP call:

```python
await save_to_mailbox(
    account_name="work",
    recipients=["alice@example.com"],
    subject="Project update",
    body="Draft content",
    mailbox="Drafts",
)
```

Mailbox names vary by provider. Use `list_mailboxes` first when `Drafts` is not
the correct name.

## Reply with proper threading

First fetch the original message and read its RFC `message_id`:

```python
result = await get_emails_content(
    account_name="work",
    email_ids=["123"],
)
original = result.emails[0]
```

Then send the reply using both threading headers:

```python
await send_email(
    account_name="work",
    recipients=[original.sender],
    subject=f"Re: {original.subject}",
    body="Thank you for your email.",
    in_reply_to=original.message_id,
    references=original.message_id,
)
```

For an existing thread, `references` should contain the known ancestor message
IDs followed by the immediate parent's ID, separated by spaces.

## Read a long message in chunks

`get_emails_content` returns at most `max_body_length` characters for each
message. If the body ends with `...[TRUNCATED]`, request the next window:

```python
first = await get_emails_content(
    account_name="work",
    email_ids=["123"],
    body_offset=0,
    max_body_length=20000,
)

second = await get_emails_content(
    account_name="work",
    email_ids=["123"],
    body_offset=20000,
    max_body_length=20000,
)
```

Keep the mailbox argument consistent with the mailbox used to obtain the
`email_id`.

## Import legacy accounts into a managed catalog

Create the destination without selecting it, preview the effective legacy
source, and apply only after reviewing every action:

```bash
mcp-email-server config init \
  --database ~/.config/mcp-email-server/catalog.sqlite3
mcp-email-server config import-legacy
mcp-email-server config import-legacy --apply
# Review the displayed plan, then type IMPORT at the prompt.
mcp-email-server account list
mcp-email-server account test work incoming
mcp-email-server config activate
mcp-email-server config select managed
```

The source uses the TOML file selected by `MCP_EMAIL_SERVER_CONFIG_PATH` plus
the same complete environment-account replacement/addition and policy override
precedence as legacy runtime. Preview displays endpoint, TLS, user,
save-to-sent, policy, credential source class, and exact target revision details
without accessing credential values or the keyring, so it can still show actions
while the legacy keyring is locked. `--apply` prints that complete plan before
accepting the interactive `IMPORT` confirmation; a no-op plan does not prompt.
Apply then fails safely if a required current TOML, environment, or keyring
credential cannot be read.

A `conflict` means the destination already has a different account with that
normalized name or retains a soft-removal tombstone. Import never overwrites
that row.
Resolve it deliberately by choosing a fresh staging catalog or reconciling the
account manually. An exact repeat is `unchanged`; an interrupted matching import
can report `resume_credentials` and install only missing bindings. The source is
left untouched in every case.

## Optional Codex and Claude Code guidance plugin

The repository publishes one optional `safe-email-operations` guidance plugin
through the Codex and Claude Code marketplace manifests. The plugin does not
install or run the Python application, expose a management API, or receive
credentials. It permits only the bounded version check plus `config status
--json` and `config doctor --json`, validates the JSON schema/command fields, and
hands all account creation or credential entry back to the user-operated terminal
or local browser. JSON availability on another command does not grant the agent
permission to run it. The plugin must never launch the UI, copy its bootstrap URL,
edit the catalog directly, or accept a secret in chat.

Before installation, pin and inspect the official repository release, both
marketplace manifests, the plugin manifest, and the canonical skill. Plugin
version `0.0.1` is matched to application version `0.0.1`; stop using
release-specific commands if they differ. Installation, update, and removal are
explicit user actions. The complete pinned commands and source-verification
checklist live in the plugin's `references/installation.md`; never curl and
execute an installer or put repository credentials in a marketplace URL.

If a user asks an agent to add an account or rotate a password, the safe handoff
is: run `mcp-email-server ui` locally and complete secret entry in the browser,
or use the documented masked interactive CLI in the user's terminal. Do not
paste credentials or the one-time UI URL into chat. If no user-controlled
terminal or browser is available, setup cannot be completed safely through the
plugin.

## Containers and CI

For non-interactive environments:

- Supply account settings through environment variables or mount a protected
  TOML file and set `MCP_EMAIL_SERVER_CONFIG_PATH`.
- Use `credential_storage = "plaintext"` only when the mounted secret file is
  appropriately protected, or provide a functional keyring backend.
- Expect `auto` to fall back to plaintext when no D-Bus keyring session exists.
- Bind HTTP transports to the required interface and configure explicit allowed
  hosts and origins.
- Mount only the directories needed for attachment upload or download.

See [Security](security.md) and [Transports](transports.md) before exposing the
service outside a local development environment.
