# mcp-email-server

[![Release](https://img.shields.io/github/v/release/wh1isper/mcp-email-server)](https://img.shields.io/github/v/release/wh1isper/mcp-email-server)
[![Build status](https://img.shields.io/github/actions/workflow/status/wh1isper/mcp-email-server/main.yml?branch=main)](https://github.com/wh1isper/mcp-email-server/actions/workflows/main.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/Wh1isper/mcp-email-server/graph/badge.svg?token=0mToRybKx8)](https://codecov.io/gh/Wh1isper/mcp-email-server)
[![Commit activity](https://img.shields.io/github/commit-activity/m/wh1isper/mcp-email-server)](https://img.shields.io/github/commit-activity/m/wh1isper/mcp-email-server)
[![License](https://img.shields.io/github/license/wh1isper/mcp-email-server)](https://img.shields.io/github/license/wh1isper/mcp-email-server)

IMAP and SMTP via MCP Server

- **Github repository**: <https://github.com/wh1isper/mcp-email-server/>
- **Documentation** <https://mcp-email-server.wh1isper.top/>

## Installation

### Manual Installation

We recommend using [uv](https://github.com/astral-sh/uv) to manage your environment.

Try `uvx mcp-email-server@latest ui` to config, and use following configuration for mcp client:

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

This package is available on PyPI, so you can install it using `pip install mcp-email-server`

After that, configure your email server using the ui: `mcp-email-server ui`

### Credential Storage

Accounts added via the UI or the `add_email_account` tool are persisted to a TOML
file at `~/.config/mcp-email-server/config.toml`. On first use, an existing
config from the previous `~/.config/zerolib/mcp_email_server/config.toml`
location is copied automatically when the new file does not exist. Where the
actual passwords/API keys live depends on `credential_storage` (also settable via
`MCP_EMAIL_SERVER_CREDENTIAL_STORAGE`), one of:

- **`auto`** (default): store credentials in the OS keyring — macOS Keychain,
  Linux Secret Service (GNOME Keyring / KWallet) — when a usable backend is
  detected; otherwise fall back to the plaintext TOML file (`0o600`
  permissions, owner-only). Falls back automatically on headless Linux,
  containers, or any environment without a D-Bus session.
- **`keyring`**: require the OS keyring; fail loudly instead of silently
  falling back if no backend is usable.
- **`plaintext`**: never touch the keyring. Useful for containers, CI, or if
  you simply prefer a portable config file.

When credentials are keyring-backed, the TOML file stores only a placeholder
(`__KEYRING__`) and non-secret metadata — the real secret lives in the OS
keyring under service `mcp-email-server`, one entry per
`<account_name>:<incoming|outgoing|api_key>` (viewable in Keychain Access on
macOS, or Seahorse on Linux).

**Migrating an existing config** between storage modes:

```sh
mcp-email-server migrate-credentials --to keyring    # move plaintext secrets into the OS keyring
mcp-email-server migrate-credentials --to plaintext  # move keyring secrets back into the TOML file
```

Migration also happens implicitly: any time you add/edit an account while
`credential_storage` is `auto` or `keyring` with a usable backend, that
account's secrets move into the keyring on the next save. If
`MCP_EMAIL_SERVER_CREDENTIAL_STORAGE` is active during a save, its effective
mode is persisted too, keeping the mode marker consistent with the credential
representation written to the same file.

#### Failure modes & troubleshooting

- **Server won't start / UI won't load accounts, keychain-related error**: the
  OS keyring is locked or unreachable. This is expected if credentials are
  keyring-backed — the secret simply isn't in the config file. Unlock your
  keychain, or run `mcp-email-server migrate-credentials --to plaintext` if
  you'd rather not depend on it.
- **`credential_storage` is 'plaintext' but the config references
  keyring-stored credentials**: run `migrate-credentials --to plaintext`, or
  unset `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE` / the `credential_storage`
  setting so the config can resolve them from the keyring instead.
- **macOS Keychain access prompt, or the server can't read a secret it wrote
  earlier**: Keychain ACLs are per-application. If the server is spawned via
  `uvx` (as in the Claude Desktop config above), a fresh `uvx` resolution can
  present a different binary path than the one that stored the secret,
  triggering a "Keychain wants to use a password" prompt. Choose "Always
  Allow" the first time this happens.
- **A migration seems to have had no effect**: if
  `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE` is set in your environment, it takes
  precedence over whatever `migrate-credentials --to ...` just wrote to the
  file on every subsequent run. Unset it, or keep it in sync with your
  intended mode.

#### Known limitations

- **Non-POSIX (Windows) file permissions**: the `0o600` owner-only guarantee on
  the plaintext TOML is enforced only on POSIX systems. On Windows the file is
  written without an owner-restricted ACL, so prefer `keyring` mode (Windows
  Credential Locker) there when secrets must not be readable by other accounts.
- **`auto`/`keyring` trusts whatever `keyring` backend is active**: usability is
  decided by a live set/get round-trip, not by the backend's storage guarantees.
  A third-party `keyring` plugin that persists secrets in plaintext would pass
  that probe. If you install custom `keyring` backends, verify the active one
  (`keyring --list-backends`) stores secrets securely.
- **Keyring and TOML writes are not transactional**: a save pushes secrets to
  the keyring and then rewrites the TOML. The TOML rewrite is atomic on its own
  (temp file + `os.replace`), but a crash _between_ the two steps can leave a
  keyring entry with no matching config reference (an orphaned secret), or a
  config reference whose keyring write partly failed. A plaintext migration
  reports keyring entries it could not remove so you can clean them up manually.

### Environment Variable Configuration

You can also configure the email server using environment variables, which is particularly useful for CI/CD environments like Jenkins. mcp-email-server supports both UI configuration (via TOML file) and environment variables, with environment variables taking precedence.

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_ACCOUNT_NAME": "work",
        "MCP_EMAIL_SERVER_FULL_NAME": "John Doe",
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "john@example.com",
        "MCP_EMAIL_SERVER_USER_NAME": "john@example.com",
        "MCP_EMAIL_SERVER_PASSWORD": "your_password",
        "MCP_EMAIL_SERVER_IMAP_HOST": "imap.gmail.com",
        "MCP_EMAIL_SERVER_IMAP_PORT": "993",
        "MCP_EMAIL_SERVER_SMTP_HOST": "smtp.gmail.com",
        "MCP_EMAIL_SERVER_SMTP_PORT": "465"
      }
    }
  }
}
```

#### Available Environment Variables

| Variable                                      | Description                                                  | Default       | Required |
| --------------------------------------------- | ------------------------------------------------------------ | ------------- | -------- |
| `MCP_EMAIL_SERVER_ACCOUNT_NAME`               | Account identifier                                           | `"default"`   | No       |
| `MCP_EMAIL_SERVER_FULL_NAME`                  | Display name                                                 | Email prefix  | No       |
| `MCP_EMAIL_SERVER_EMAIL_ADDRESS`              | Email address                                                | -             | Yes      |
| `MCP_EMAIL_SERVER_USER_NAME`                  | Login username                                               | Same as email | No       |
| `MCP_EMAIL_SERVER_PASSWORD`                   | Email password                                               | -             | Yes      |
| `MCP_EMAIL_SERVER_IMAP_HOST`                  | IMAP server host                                             | -             | Yes      |
| `MCP_EMAIL_SERVER_IMAP_PORT`                  | IMAP server port                                             | `993`         | No       |
| `MCP_EMAIL_SERVER_IMAP_SSL`                   | Enable IMAP SSL                                              | `true`        | No       |
| `MCP_EMAIL_SERVER_IMAP_START_SSL`             | Enable IMAP STARTTLS                                         | `false`       | No       |
| `MCP_EMAIL_SERVER_IMAP_VERIFY_SSL`            | Verify IMAP SSL certificates (disable for self-signed)       | `true`        | No       |
| `MCP_EMAIL_SERVER_SMTP_HOST`                  | SMTP server host; omit for IMAP-only mode (no sending)       | -             | No       |
| `MCP_EMAIL_SERVER_SMTP_PORT`                  | SMTP server port                                             | `465`         | No       |
| `MCP_EMAIL_SERVER_SMTP_SSL`                   | Enable SMTP SSL                                              | `true`        | No       |
| `MCP_EMAIL_SERVER_SMTP_START_SSL`             | Enable STARTTLS                                              | `false`       | No       |
| `MCP_EMAIL_SERVER_SMTP_VERIFY_SSL`            | Verify SSL certificates (disable for self-signed)            | `true`        | No       |
| `MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD` | Enable attachment download                                   | `false`       | No       |
| `MCP_EMAIL_SERVER_SAVE_TO_SENT`               | Save sent emails to IMAP Sent folder                         | `true`        | No       |
| `MCP_EMAIL_SERVER_SENT_FOLDER_NAME`           | Custom Sent folder name (auto-detect if not set)             | -             | No       |
| `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`         | Recipient allowlist (comma-separated); empty = all           | -             | No       |
| `MCP_EMAIL_SERVER_ALLOWED_SENDERS`            | Sender allowlist (comma-separated globs); empty = all        | -             | No       |
| `MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS`   | Report blocked mutations as failures (default: silent no-op) | `false`       | No       |
| `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE`         | Credential storage mode: `auto`, `keyring`, or `plaintext`   | `auto`        | No       |

### IMAP-only mode (no SMTP)

SMTP configuration is optional. When `MCP_EMAIL_SERVER_SMTP_HOST` is omitted, the account runs in IMAP-only mode: `send_email` is hidden (when every configured email account lacks SMTP) and no mail can leave the server. Note that IMAP-only is not strictly read-only — IMAP-backed write tools such as `save_to_mailbox` (which composes a message and stores it in a folder via IMAP APPEND), `delete_emails`, `move_emails`, and `archive_emails` remain available.

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "john@example.com",
        "MCP_EMAIL_SERVER_PASSWORD": "your_password",
        "MCP_EMAIL_SERVER_IMAP_HOST": "imap.gmail.com"
      }
    }
  }
}
```

### HTTP Transport Security

HTTP transports (`sse` and `streamable-http`) validate request `Host` and `Origin` headers to protect against DNS rebinding attacks. Localhost is allowed by default. For container networks or reverse proxies, configure the expected service names explicitly.

| Variable                              | Description                                                      | Default           |
| ------------------------------------- | ---------------------------------------------------------------- | ----------------- |
| `MCP_HOST`                            | HTTP bind host for `streamable-http`                             | `localhost`       |
| `MCP_PORT`                            | HTTP bind port for `streamable-http`                             | `9557`            |
| `MCP_ALLOWED_HOSTS`                   | Comma-separated allowed `Host` values. Supports `host:*` ports   | Localhost hosts   |
| `MCP_ALLOWED_ORIGINS`                 | Comma-separated allowed `Origin` values. Supports `host:*` ports | Localhost origins |
| `MCP_ENABLE_DNS_REBINDING_PROTECTION` | Enable DNS rebinding protection                                  | `true`            |

Bare host entries such as `MCP_ALLOWED_HOSTS=mcp-email-server` also allow any port on that host. `MCP_ENABLE_DNS_REBINDING_PROTECTION=false`, `MCP_ALLOWED_HOSTS=*`, or `MCP_ALLOWED_ORIGINS=*` disables Host and Origin validation entirely. Use those options only in isolated local development environments.

IPv6 literals in allowlists should use bracketed notation, such as `[::1]:*` and `http://[::1]:*`.

### Enabling Attachment Downloads

By default, downloading email attachments is disabled for security reasons. To enable this feature, you can either:

**Option 1: Environment Variable**

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD": "true"
      }
    }
  }
}
```

**Option 2: TOML Configuration**

Add `enable_attachment_download = true` to your TOML configuration file (`~/.config/mcp-email-server/config.toml`):

```toml
enable_attachment_download = true

[[emails]]
# ... your email configuration
```

Once enabled, you can use the `download_attachment` tool to save email attachments to a specified path.

### Saving Sent Emails to IMAP Sent Folder

By default, sent emails are automatically saved to your IMAP Sent folder. This ensures that emails sent via the MCP server appear in your email client (Thunderbird, webmail, etc.).

The server auto-detects common Sent folder names: `Sent`, `INBOX.Sent`, `Sent Items`, `Sent Mail`, `[Gmail]/Sent Mail`.

**To specify a custom Sent folder name** (useful for providers with non-standard folder names):

**Option 1: Environment Variable**

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_SENT_FOLDER_NAME": "INBOX.Sent"
      }
    }
  }
}
```

**Option 2: TOML Configuration**

```toml
[[emails]]
account_name = "work"
save_to_sent = true
sent_folder_name = "INBOX.Sent"
# ... rest of your email configuration
```

**To disable saving to Sent folder**, set `MCP_EMAIL_SERVER_SAVE_TO_SENT=false` or `save_to_sent = false` in your TOML config.

### Restricting Recipients (Allowlist)

By default the server can send to any address. Set `allowed_recipients` to restrict **both**
`send_email` and `save_to_mailbox` to a trusted set. Leave it empty (the default) to allow all.

```toml
allowed_recipients = ["alice@example.com", "bob@example.com"]
```

Or via environment variable (comma-separated):

```
MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS="alice@example.com,bob@example.com"
```

When configured, any To/CC/BCC address not on the list is rejected with a clear error. Matching is
case-insensitive and understands the `Name <addr@example.com>` form. The `list_allowed_recipients`
tool appears only when an allowlist is configured, so default installs keep a minimal tool surface.

### Filtering Incoming Mail (Sender Allowlist)

By default all senders are visible. Set `allowed_senders` to show mail only from trusted senders.
Patterns support globs (e.g. `*@company.com`) and exact addresses, matched case-insensitively. Leave
it empty (the default) to show everything.

```toml
allowed_senders = ["*@company.com", "alice@example.com"]
```

Or via environment variable (comma-separated):

```
MCP_EMAIL_SERVER_ALLOWED_SENDERS="*@company.com,alice@example.com"
```

When configured, filtering is applied to inbound read and mutation paths: `list_emails_metadata` excludes
non-allowed senders **before** pagination, so `total` and page sizes reflect only allowed mail;
`get_emails_content` and `download_attachment` check the sender before reading a message, so a non-allowed
message's body and attachments are never fetched or marked read, and it is reported as inaccessible —
indistinguishable from a missing message. Mutation tools first check the sender and never delete, flag, or
move blocked mail. The `list_allowed_senders` tool appears only when an allowlist is configured.

**Scope:** the allowlist protects every inbound path — read (`list_emails_metadata`, `get_emails_content`,
`download_attachment`) and mutation (`delete_emails`, `mark_emails_as_read`, `move_emails`,
`archive_emails`). A blocked sender's mail is never read, deleted, flagged, or moved.

**Blocked mutations (`report_blocked_mutations`, default `false`):** when a mutation targets a blocked
sender's message, it is never performed. By default the result is reported as a successful no-op —
indistinguishable from acting on a non-existent message, so the allowlist does not reveal that a hidden
message exists. Set `report_blocked_mutations = true` (or `MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS=true`)
to instead report blocked UIDs as failures (explicit, but reveals a blocked-but-real message differs from
a missing one).

**Note:** matching is against the message's `From` header — local filtering only, not sender
authentication. A spoofed `From` will pass the allowlist, so this is not a substitute for provider-side
SPF / DKIM / DMARC enforcement.

### Self-Signed Certificates and IMAP STARTTLS (e.g., ProtonMail Bridge)

Local mail bridges such as ProtonMail Bridge commonly use STARTTLS with self-signed certificates. Configure IMAP with plaintext connect plus STARTTLS upgrade, and disable certificate verification for the local bridge certificate:

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_IMAP_HOST": "127.0.0.1",
        "MCP_EMAIL_SERVER_IMAP_PORT": "1143",
        "MCP_EMAIL_SERVER_IMAP_SSL": "false",
        "MCP_EMAIL_SERVER_IMAP_START_SSL": "true",
        "MCP_EMAIL_SERVER_IMAP_VERIFY_SSL": "false",
        "MCP_EMAIL_SERVER_SMTP_VERIFY_SSL": "false"
      }
    }
  }
}
```

Or in TOML configuration:

```toml
[[emails]]
account_name = "protonmail"
# ... other settings ...

[emails.incoming]
host = "127.0.0.1"
port = 1143
use_ssl = false
start_ssl = true
verify_ssl = false

[emails.outgoing]
verify_ssl = false
```

For separate IMAP/SMTP credentials, you can also use:

- `MCP_EMAIL_SERVER_IMAP_USER_NAME` / `MCP_EMAIL_SERVER_IMAP_PASSWORD`
- `MCP_EMAIL_SERVER_SMTP_USER_NAME` / `MCP_EMAIL_SERVER_SMTP_PASSWORD`

Then you can try it in [Claude Desktop](https://claude.ai/download). If you want to intergrate it with other mcp client, run `$which mcp-email-server` for the path and configure it in your client like:

```json
{
  "mcpServers": {
    "mcp-email-server": {
      "command": "{{ ENTRYPOINT }}",
      "args": ["stdio"]
    }
  }
}
```

## Usage

### Replying to Emails

To reply to an email with proper threading (so it appears in the same conversation in email clients):

1. First, fetch the original email to get its `message_id`:

```python
emails = await get_emails_content(account_name="work", email_ids=["123"])
original = emails.emails[0]
```

2. Send your reply using `in_reply_to` and `references`:

```python
await send_email(
    account_name="work",
    recipients=[original.sender],
    subject=f"Re: {original.subject}",
    body="Thank you for your email...",
    in_reply_to=original.message_id,
    references=original.message_id,
)
```

The `in_reply_to` parameter sets the `In-Reply-To` header, and `references` sets the `References` header. Both are used by email clients to thread conversations properly.

## Development

This project is managed using [uv](https://github.com/astral-sh/uv).

Try `make install` to install the virtual environment and install the pre-commit hooks.

Use `uv run mcp-email-server` for local development.

## Releasing a new version

- Create an API Token on [PyPI](https://pypi.org/).
- Add the API Token to your projects secrets with the name `PYPI_TOKEN` by visiting [this page](https://github.com/wh1isper/mcp-email-server/settings/secrets/actions/new).
- Create a [new release](https://github.com/wh1isper/mcp-email-server/releases/new) on Github.
- Create a new tag in the form `*.*.*`.

For more details, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/cicd/#how-to-trigger-a-release).
