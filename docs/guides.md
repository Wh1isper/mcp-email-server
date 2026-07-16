# Guides

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

If no configured account has SMTP, `send_email` is omitted from the MCP tool
list. IMAP mutation tools remain available, so this is not a strict read-only
mode. To limit mutations, also constrain which MCP tools the client may call or
run the server with an account whose provider permissions are read-only.

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
