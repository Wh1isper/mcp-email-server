# Installation & Configuration

Complete guide for installing mcp-email-server and configuring accounts, permissions, and security options.

## Quick start

We recommend [uv](https://github.com/astral-sh/uv) to manage your environment.

Run the configuration UI once to set up your account:

```bash
uvx mcp-email-server@latest ui
```

Then point your MCP client at the server:

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"]
    }
  }
}
```

> **Note:** the server starts **read-only** by default. To let it organize, delete, or send mail, grant [permission scopes](#permission-scopes).

## Installation methods

### uv (recommended)

Install as a persistent tool so `mcp-email-server` is on your `PATH`:

```bash
uv tool install mcp-email-server
mcp-email-server ui   # configure your account
```

Alternatively, the Quick start above uses `uvx mcp-email-server@latest stdio`, which runs the server without installing it — uv fetches and caches it on first use. Use `uv tool install` when you want a stable command (e.g. for the Claude Code CLI example below); use `uvx` for a zero-install setup.

### Claude Code (CLI)

If you use the [Claude Code](https://claude.com/claude-code) CLI, register the server with `claude mcp add` instead of hand-editing JSON. With uv tool install (above) putting `mcp-email-server` on your `PATH`:

```bash
claude mcp add zerolib-email --scope user -- mcp-email-server stdio
```

`--scope user` makes it available across all your projects (drop it for the current project only). To run without installing first, use `uvx` as the command:

```bash
claude mcp add zerolib-email --scope user -- uvx mcp-email-server@latest stdio
```

Pass configuration with `-e KEY=value` before the `--`:

```bash
claude mcp add zerolib-email --scope user \
  -e MCP_EMAIL_SERVER_EMAIL_ADDRESS=you@example.com \
  -e MCP_EMAIL_SERVER_PASSWORD=your_password \
  -e MCP_EMAIL_SERVER_IMAP_HOST=imap.gmail.com \
  -- mcp-email-server stdio
```

Verify with `claude mcp list` (or `/mcp` inside Claude Code); remove with `claude mcp remove zerolib-email`.

### pip

If you prefer pip:

```bash
pip install mcp-email-server
mcp-email-server ui   # configure your account
```

For clients that need an absolute path, find the entrypoint with `which mcp-email-server` and configure:

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "{{ ENTRYPOINT }}",
      "args": ["stdio"]
    }
  }
}
```

### Docker

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "docker",
      "args": ["run", "-it", "ghcr.io/ai-zerolab/mcp-email-server:latest"]
    }
  }
}
```

The default config path inside the container is `~/.config/zerolib/mcp_email_server/config.toml`.

### Smithery

To install for Claude Desktop automatically via [Smithery](https://smithery.ai/server/@ai-zerolab/mcp-email-server):

```bash
npx -y @smithery/cli install @ai-zerolab/mcp-email-server --client claude
```

## Updating

How you update depends on how you installed. Restart your MCP client (Claude Desktop, Claude Code, etc.) afterward so it picks up the new version.

| Installed with    | Update command                                                                              |
| ----------------- | ------------------------------------------------------------------------------------------- |
| `uv tool install` | `uv tool upgrade mcp-email-server`                                                          |
| `uvx ...@latest`  | Re-resolves on each run; force a refresh with `uvx --refresh mcp-email-server@latest stdio` |
| `pip`             | `pip install --upgrade mcp-email-server`                                                    |
| Docker            | `docker pull ghcr.io/ai-zerolab/mcp-email-server:latest`                                    |

For the Claude Code CLI: if you registered the `uvx ...@latest` form it updates automatically on the next launch; if you registered the installed `mcp-email-server` command, run `uv tool upgrade mcp-email-server` first.

## Configuration overview

Configuration lives in a TOML file at `~/.config/zerolib/mcp_email_server/config.toml` (override the path with `MCP_EMAIL_SERVER_CONFIG_PATH`). Two ways to manage it:

- **UI**: `mcp-email-server ui` walks you through account setup.
- **Environment variables**: useful for CI/CD and containerized setups; they take precedence over the TOML file.

Example environment-variable account configuration:

```json
{
  "mcpServers": {
    "zerolib-email": {
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

### Available environment variables

| Variable                                      | Description                                                                                          | Default       | Required |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------------- | -------- |
| `MCP_EMAIL_SERVER_ACCOUNT_NAME`               | Account identifier                                                                                   | `"default"`   | No       |
| `MCP_EMAIL_SERVER_FULL_NAME`                  | Display name                                                                                         | Email prefix  | No       |
| `MCP_EMAIL_SERVER_EMAIL_ADDRESS`              | Email address                                                                                        | -             | Yes      |
| `MCP_EMAIL_SERVER_USER_NAME`                  | Login username                                                                                       | Same as email | No       |
| `MCP_EMAIL_SERVER_PASSWORD`                   | Email password                                                                                       | -             | Yes      |
| `MCP_EMAIL_SERVER_IMAP_HOST`                  | IMAP server host                                                                                     | -             | Yes      |
| `MCP_EMAIL_SERVER_IMAP_PORT`                  | IMAP server port                                                                                     | `993`         | No       |
| `MCP_EMAIL_SERVER_IMAP_SSL`                   | Enable IMAP SSL                                                                                      | `true`        | No       |
| `MCP_EMAIL_SERVER_IMAP_START_SSL`             | Enable IMAP STARTTLS                                                                                 | `false`       | No       |
| `MCP_EMAIL_SERVER_IMAP_VERIFY_SSL`            | Verify IMAP SSL certificates (disable for self-signed)                                               | `true`        | No       |
| `MCP_EMAIL_SERVER_SMTP_HOST`                  | SMTP server host; omit for read-only mode                                                            | -             | No       |
| `MCP_EMAIL_SERVER_SMTP_PORT`                  | SMTP server port                                                                                     | `465`         | No       |
| `MCP_EMAIL_SERVER_SMTP_SSL`                   | Enable SMTP SSL                                                                                      | `true`        | No       |
| `MCP_EMAIL_SERVER_SMTP_START_SSL`             | Enable STARTTLS                                                                                      | `false`       | No       |
| `MCP_EMAIL_SERVER_SMTP_VERIFY_SSL`            | Verify SSL certificates (disable for self-signed)                                                    | `true`        | No       |
| `MCP_EMAIL_SERVER_PERMISSIONS`                | Permission scopes (comma-separated): `read`, `draft`, `organize`, `delete`, `send`, `manage`, `full` | `read`        | No       |
| `MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD` | Enable attachment download                                                                           | `false`       | No       |
| `MCP_EMAIL_SERVER_SAVE_TO_SENT`               | Save sent emails to IMAP Sent folder                                                                 | `true`        | No       |
| `MCP_EMAIL_SERVER_SENT_FOLDER_NAME`           | Custom Sent folder name (auto-detect if not set)                                                     | -             | No       |
| `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`         | Recipient allowlist (comma-separated); empty = all                                                   | -             | No       |
| `MCP_EMAIL_SERVER_ALLOWED_SENDERS`            | Sender allowlist (comma-separated globs); empty = all                                                | -             | No       |
| `MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS`   | Report blocked mutations as failures (default: silent no-op)                                         | `false`       | No       |
| `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE`         | Credential storage mode: `auto`, `keyring`, or `plaintext`                                           | `auto`        | No       |

For separate IMAP/SMTP credentials, you can also use:

- `MCP_EMAIL_SERVER_IMAP_USER_NAME` / `MCP_EMAIL_SERVER_IMAP_PASSWORD`
- `MCP_EMAIL_SERVER_SMTP_USER_NAME` / `MCP_EMAIL_SERVER_SMTP_PASSWORD`

## Permission scopes

Every MCP tool is gated behind a capability scope. **The default is `read`: a fresh install is read-only** — the server can list and read mail but cannot modify, delete, or send anything until you grant more scopes. Tools outside the granted scopes are hidden from the client's tool list _and_ rejected at call time.

| Scope      | Grants                                                                                                                                                                              |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read`     | Always granted. `list_available_accounts`, `list_emails_metadata`, `get_emails_content`, `list_mailboxes`, `download_attachment` (which also requires `enable_attachment_download`) |
| `draft`    | `save_to_mailbox`, restricted to drafts-type folders (`Drafts`, `INBOX.Drafts`, `[Gmail]/Drafts`, …). Combine with `organize` to save to arbitrary folders.                         |
| `organize` | `move_emails`, `archive_emails`, `mark_emails_as_read` — and the `mark_as_read` parameter of `get_emails_content`                                                                   |
| `delete`   | `delete_emails`                                                                                                                                                                     |
| `send`     | `send_email` (the tool also requires at least one SMTP-configured account)                                                                                                          |
| `manage`   | `add_email_account` — writes credentials to disk/keyring, so treat it as server administration                                                                                      |
| `full`     | Everything above, including `manage`                                                                                                                                                |

Scopes are independent — grant any combination. For example, an assistant that reads mail, drafts replies, and sends them, but can never delete or move anything, is `["read", "draft", "send"]`.

### Setting and changing scopes

Scopes come from one of two places. If both are set, the **environment variable wins**.

**1. Config file — persistent (recommended).** Edit the `permissions` line in your config TOML (`~/.config/zerolib/mcp_email_server/config.toml`, or wherever `MCP_EMAIL_SERVER_CONFIG_PATH` points):

```toml
permissions = ["read", "draft", "send"]
```

Save the file, then **restart your MCP client** — scopes are read when the server starts. To change them later, edit this same line and restart again. (If you configured the account entirely through environment variables and have no TOML file, use method 2 instead.)

**2. `MCP_EMAIL_SERVER_PERMISSIONS` environment variable — overrides the file.** Comma-separated (e.g. `read,draft,send`). It overrides whatever the TOML says and is **never written back** to the file, so it's the right choice for per-client or throwaway setups. Set it wherever your client defines the server's environment:

- **Claude Code CLI** — pass `-e` when adding the server:

  ```bash
  claude mcp add zerolib-email --scope user \
    -e MCP_EMAIL_SERVER_PERMISSIONS=read,draft,send \
    -- mcp-email-server stdio
  ```

  To change it later, remove and re-add with the new value (or edit the entry in your Claude config), then reconnect:

  ```bash
  claude mcp remove zerolib-email
  claude mcp add zerolib-email --scope user \
    -e MCP_EMAIL_SERVER_PERMISSIONS=read,organize \
    -- mcp-email-server stdio
  ```

- **JSON `mcpServers` config** (Claude Desktop and similar) — add or edit it in the server's `env` block, then restart the app:

  ```json
  {
    "mcpServers": {
      "zerolib-email": {
        "command": "uvx",
        "args": ["mcp-email-server@latest", "stdio"],
        "env": { "MCP_EMAIL_SERVER_PERMISSIONS": "read,draft,send" }
      }
    }
  }
  ```

- **Docker** — add it under `environment:` (Compose) or `-e MCP_EMAIL_SERVER_PERMISSIONS=...` (`docker run`).

Setting the variable to an empty string resets to the read-only default. Unknown scope names are rejected at startup.

### After you change scopes

- **Restart / reconnect the MCP client.** The server reads scopes once at startup, so a change only takes effect on the next launch.
- **Verify what's active by the tools your client shows.** A scope you didn't grant hides its tools — e.g. without `delete`, `delete_emails` won't appear in the tool list — and calling a hidden tool anyway is rejected server-side, so visibility and enforcement always agree.

> **Upgrade note:** versions without permission scopes exposed every tool unconditionally. To restore that behavior, set `permissions = ["full"]` (or `MCP_EMAIL_SERVER_PERMISSIONS=full`).

## Credential storage

Accounts added via the UI or the `add_email_account` tool are persisted to the TOML config file. Where the actual passwords/API keys live depends on `credential_storage` (also settable via `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE`), one of:

- **`auto`** (default): store credentials in the OS keyring — macOS Keychain, Linux Secret Service (GNOME Keyring / KWallet) — when a usable backend is detected; otherwise fall back to the plaintext TOML file (`0o600` permissions, owner-only). Falls back automatically on headless Linux, Docker, or any environment without a D-Bus session.
- **`keyring`**: require the OS keyring; fail loudly instead of silently falling back if no backend is usable.
- **`plaintext`**: never touch the keyring. Useful for containers, CI, or if you simply prefer a portable config file.

When credentials are keyring-backed, the TOML file stores only a placeholder (`__KEYRING__`) and non-secret metadata — the real secret lives in the OS keyring under service `mcp-email-server`, one entry per `<account_name>:<incoming|outgoing|api_key>` (viewable in Keychain Access on macOS, or Seahorse on Linux).

**Migrating an existing config** between storage modes:

```sh
mcp-email-server migrate-credentials --to keyring    # move plaintext secrets into the OS keyring
mcp-email-server migrate-credentials --to plaintext  # move keyring secrets back into the TOML file
```

Migration also happens implicitly: any time you add/edit an account while `credential_storage` is `auto` or `keyring` with a usable backend, that account's secrets move into the keyring on the next save. If `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE` is active during a save, its effective mode is persisted too, keeping the mode marker consistent with the credential representation written to the same file.

### Failure modes & troubleshooting

- **Server won't start / UI won't load accounts, keychain-related error**: the OS keyring is locked or unreachable. This is expected if credentials are keyring-backed — the secret simply isn't in the config file. Unlock your keychain, or run `mcp-email-server migrate-credentials --to plaintext` if you'd rather not depend on it.
- **`credential_storage` is 'plaintext' but the config references keyring-stored credentials**: run `migrate-credentials --to plaintext`, or unset `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE` / the `credential_storage` setting so the config can resolve them from the keyring instead.
- **macOS Keychain access prompt, or the server can't read a secret it wrote earlier**: Keychain ACLs are per-application. If the server is spawned via `uvx`, a fresh `uvx` resolution can present a different binary path than the one that stored the secret, triggering a "Keychain wants to use a password" prompt. Choose "Always Allow" the first time this happens.
- **A migration seems to have had no effect**: if `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE` is set in your environment, it takes precedence over whatever `migrate-credentials --to ...` just wrote to the file on every subsequent run. Unset it, or keep it in sync with your intended mode.

### Known limitations

- **Non-POSIX (Windows) file permissions**: the `0o600` owner-only guarantee on the plaintext TOML is enforced only on POSIX systems. On Windows the file is written without an owner-restricted ACL, so prefer `keyring` mode (Windows Credential Locker) there when secrets must not be readable by other accounts.
- **`auto`/`keyring` trusts whatever `keyring` backend is active**: usability is decided by a live set/get round-trip, not by the backend's storage guarantees. A third-party `keyring` plugin that persists secrets in plaintext would pass that probe. If you install custom `keyring` backends, verify the active one (`keyring --list-backends`) stores secrets securely.
- **Keyring and TOML writes are not transactional**: a save pushes secrets to the keyring and then rewrites the TOML. The TOML rewrite is atomic on its own (temp file + `os.replace`), but a crash _between_ the two steps can leave a keyring entry with no matching config reference (an orphaned secret), or a config reference whose keyring write partly failed. A plaintext migration reports keyring entries it could not remove so you can clean them up manually.

## Read-only IMAP mode

SMTP configuration is optional. When `MCP_EMAIL_SERVER_SMTP_HOST` is omitted, no account can send: `send_email` is hidden when every configured email account lacks SMTP — independently of the [permission scopes](#permission-scopes) above (both gates must pass). This does not disable IMAP-backed write tools: `save_to_mailbox` (a pure IMAP APPEND) stays available whenever the `draft` scope is granted, as do `delete_emails`, `move_emails`, and `archive_emails` under their `delete`/`organize` scopes.

```json
{
  "mcpServers": {
    "zerolib-email": {
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

## HTTP transport security

HTTP transports (`sse` and `streamable-http`) validate request `Host` and `Origin` headers to protect against DNS rebinding attacks. Localhost is allowed by default. For Docker networks or reverse proxies, configure the expected service names explicitly.

| Variable                              | Description                                                      | Default           |
| ------------------------------------- | ---------------------------------------------------------------- | ----------------- |
| `MCP_HOST`                            | HTTP bind host for `streamable-http`                             | `localhost`       |
| `MCP_PORT`                            | HTTP bind port for `streamable-http`                             | `9557`            |
| `MCP_ALLOWED_HOSTS`                   | Comma-separated allowed `Host` values. Supports `host:*` ports   | Localhost hosts   |
| `MCP_ALLOWED_ORIGINS`                 | Comma-separated allowed `Origin` values. Supports `host:*` ports | Localhost origins |
| `MCP_ENABLE_DNS_REBINDING_PROTECTION` | Enable DNS rebinding protection                                  | `true`            |

Docker Compose example:

```yaml
services:
  mcp-email-server:
    image: ghcr.io/ai-zerolab/mcp-email-server:latest
    command: ["streamable-http"]
    environment:
      MCP_HOST: 0.0.0.0
      MCP_PORT: 9557
      MCP_ALLOWED_HOSTS: mcp-email-server:*,localhost:*,127.0.0.1:*
      MCP_ALLOWED_ORIGINS: http://mcp-email-server:*,http://localhost:*,http://127.0.0.1:*
```

Bare host entries such as `MCP_ALLOWED_HOSTS=mcp-email-server` also allow any port on that host. `MCP_ENABLE_DNS_REBINDING_PROTECTION=false`, `MCP_ALLOWED_HOSTS=*`, or `MCP_ALLOWED_ORIGINS=*` disables Host and Origin validation entirely. Use those options only in isolated local development environments.

IPv6 literals in allowlists should use bracketed notation, such as `[::1]:*` and `http://[::1]:*`.

## Enabling attachment downloads

By default, downloading email attachments is disabled for security reasons. To enable this feature, you can either:

**Option 1: Environment Variable**

```json
{
  "mcpServers": {
    "zerolib-email": {
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

Add `enable_attachment_download = true` to your TOML configuration file:

```toml
enable_attachment_download = true

[[emails]]
# ... your email configuration
```

Once enabled, you can use the `download_attachment` tool to save email attachments to a specified path.

## Saving sent emails to the IMAP Sent folder

By default, sent emails are automatically saved to your IMAP Sent folder. This ensures that emails sent via the MCP server appear in your email client (Thunderbird, webmail, etc.).

The server auto-detects common Sent folder names: `Sent`, `INBOX.Sent`, `Sent Items`, `Sent Mail`, `[Gmail]/Sent Mail`.

**To specify a custom Sent folder name** (useful for providers with non-standard folder names):

**Option 1: Environment Variable**

```json
{
  "mcpServers": {
    "zerolib-email": {
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

## Restricting recipients (allowlist)

By default the server can send to any address. Set `allowed_recipients` to restrict **both** `send_email` and `save_to_mailbox` to a trusted set. Leave it empty (the default) to allow all.

```toml
allowed_recipients = ["alice@example.com", "bob@example.com"]
```

Or via environment variable (comma-separated):

```
MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS="alice@example.com,bob@example.com"
```

When configured, any To/CC/BCC address not on the list is rejected with a clear error. Matching is case-insensitive and understands the `Name <addr@example.com>` form. The `list_allowed_recipients` tool appears only when an allowlist is configured, so default installs keep a minimal tool surface.

## Filtering incoming mail (sender allowlist)

By default all senders are visible. Set `allowed_senders` to show mail only from trusted senders. Patterns support globs (e.g. `*@company.com`) and exact addresses, matched case-insensitively. Leave it empty (the default) to show everything.

```toml
allowed_senders = ["*@company.com", "alice@example.com"]
```

Or via environment variable (comma-separated):

```
MCP_EMAIL_SERVER_ALLOWED_SENDERS="*@company.com,alice@example.com"
```

When configured, filtering is applied to inbound read and mutation paths: `list_emails_metadata` excludes non-allowed senders **before** pagination, so `total` and page sizes reflect only allowed mail; `get_emails_content` and `download_attachment` check the sender before reading a message, so a non-allowed message's body and attachments are never fetched or marked read, and it is reported as inaccessible — indistinguishable from a missing message. Mutation tools first check the sender and never delete, flag, or move blocked mail. The `list_allowed_senders` tool appears only when an allowlist is configured.

**Scope:** the allowlist protects every inbound path — read (`list_emails_metadata`, `get_emails_content`, `download_attachment`) and mutation (`delete_emails`, `mark_emails_as_read`, `move_emails`, `archive_emails`). A blocked sender's mail is never read, deleted, flagged, or moved.

**Blocked mutations (`report_blocked_mutations`, default `false`):** when a mutation targets a blocked sender's message, it is never performed. By default the result is reported as a successful no-op — indistinguishable from acting on a non-existent message, so the allowlist does not reveal that a hidden message exists. Set `report_blocked_mutations = true` (or `MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS=true`) to instead report blocked UIDs as failures (explicit, but reveals a blocked-but-real message differs from a missing one).

**Note:** matching is against the message's `From` header — local filtering only, not sender authentication. A spoofed `From` will pass the allowlist, so this is not a substitute for provider-side SPF / DKIM / DMARC enforcement.

## Self-signed certificates and IMAP STARTTLS (e.g., ProtonMail Bridge)

Local mail bridges such as ProtonMail Bridge commonly use STARTTLS with self-signed certificates. Configure IMAP with plaintext connect plus STARTTLS upgrade, and disable certificate verification for the local bridge certificate:

```json
{
  "mcpServers": {
    "zerolib-email": {
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
