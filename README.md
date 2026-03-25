# mcp-email-server

[![Release](https://img.shields.io/github/v/release/ai-zerolab/mcp-email-server)](https://img.shields.io/github/v/release/ai-zerolab/mcp-email-server)
[![Build status](https://img.shields.io/github/actions/workflow/status/ai-zerolab/mcp-email-server/main.yml?branch=main)](https://github.com/ai-zerolab/mcp-email-server/actions/workflows/main.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/ai-zerolab/mcp-email-server/branch/main/graph/badge.svg)](https://codecov.io/gh/ai-zerolab/mcp-email-server)
[![Commit activity](https://img.shields.io/github/commit-activity/m/ai-zerolab/mcp-email-server)](https://img.shields.io/github/commit-activity/m/ai-zerolab/mcp-email-server)
[![License](https://img.shields.io/github/license/ai-zerolab/mcp-email-server)](https://img.shields.io/github/license/ai-zerolab/mcp-email-server)
[![smithery badge](https://smithery.ai/badge/@ai-zerolab/mcp-email-server)](https://smithery.ai/server/@ai-zerolab/mcp-email-server)

IMAP and SMTP via MCP Server

- **Github repository**: <https://github.com/ai-zerolab/mcp-email-server/>
- **Documentation** <https://ai-zerolab.github.io/mcp-email-server/>

## Installation

### Manual Installation

We recommend using [uv](https://github.com/astral-sh/uv) to manage your environment.

Try `uvx mcp-email-server@latest ui` to config, and use following configuration for mcp client:

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

This package is available on PyPI, so you can install it using `pip install mcp-email-server`

After that, configure your email server using the ui: `mcp-email-server ui`

### Environment Variable Configuration

You can also configure the email server using environment variables, which is particularly useful for CI/CD environments like Jenkins. zerolib-email supports both UI configuration (via TOML file) and environment variables, with environment variables taking precedence.

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

#### Available Environment Variables

| Variable                                      | Description                                                                                                                                | Default       | Required |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------- | -------- |
| `MCP_EMAIL_SERVER_ACCOUNT_NAME`               | Account identifier                                                                                                                         | `"default"`   | No       |
| `MCP_EMAIL_SERVER_FULL_NAME`                  | Display name                                                                                                                               | Email prefix  | No       |
| `MCP_EMAIL_SERVER_EMAIL_ADDRESS`              | Email address                                                                                                                              | -             | Yes      |
| `MCP_EMAIL_SERVER_USER_NAME`                  | Login username                                                                                                                             | Same as email | No       |
| `MCP_EMAIL_SERVER_PASSWORD`                   | Email password                                                                                                                             | -             | Yes      |
| `MCP_EMAIL_SERVER_IMAP_HOST`                  | IMAP server host                                                                                                                           | -             | Yes      |
| `MCP_EMAIL_SERVER_IMAP_PORT`                  | IMAP server port                                                                                                                           | `993`         | No       |
| `MCP_EMAIL_SERVER_IMAP_SSL`                   | Enable IMAP SSL                                                                                                                            | `true`        | No       |
| `MCP_EMAIL_SERVER_IMAP_VERIFY_SSL`            | Verify IMAP SSL certificates (disable for self-signed)                                                                                     | `true`        | No       |
| `MCP_EMAIL_SERVER_SMTP_HOST`                  | SMTP server host                                                                                                                           | -             | Yes      |
| `MCP_EMAIL_SERVER_SMTP_PORT`                  | SMTP server port                                                                                                                           | `465`         | No       |
| `MCP_EMAIL_SERVER_SMTP_SSL`                   | Enable SMTP SSL                                                                                                                            | `true`        | No       |
| `MCP_EMAIL_SERVER_SMTP_START_SSL`             | Enable STARTTLS                                                                                                                            | `false`       | No       |
| `MCP_EMAIL_SERVER_SMTP_VERIFY_SSL`            | Verify SSL certificates (disable for self-signed)                                                                                          | `true`        | No       |
| `MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD` | Enable attachment download                                                                                                                 | `false`       | No       |
| `MCP_EMAIL_SERVER_SAVE_TO_SENT`               | Save sent emails to IMAP Sent folder                                                                                                       | `true`        | No       |
| `MCP_EMAIL_SERVER_SENT_FOLDER_NAME`           | Custom Sent folder name (auto-detect if not set)                                                                                           | -             | No       |
| `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`         | Comma-separated list of permitted recipient addresses. Empty = allow all (default). Example: `alice@example.com,bob@example.com`           | -             | No       |
| `MCP_EMAIL_SERVER_ALLOWED_SENDERS`            | Comma-separated list of permitted sender address patterns. Supports fnmatch wildcards (e.g. `*@example.com`). Empty = allow all (default). | -             | No       |

> **Note:** Unknown keys in the TOML config file are silently ignored. This means typos,
> keys from older versions, or settings from a different feature branch will not cause
> the server to crash.

### Enabling Attachment Downloads

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

Add `enable_attachment_download = true` to your TOML configuration file (`~/.config/zerolib/mcp_email_server/config.toml`):

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

### Restricting Recipients (Allowlist)

By default, the MCP client can send email to any address. You can restrict sends to a trusted set of addresses using the `allowed_recipients` option.

**Option 1: Environment Variable**

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS": "alice@example.com,bob@example.com"
      }
    }
  }
}
```

**Option 2: TOML Configuration**

```toml
allowed_recipients = ["alice@example.com", "bob@example.com"]
```

When non-empty, `send_email` rejects any recipient (To, CC, BCC) not in the list with a clear error. Addresses are matched case-insensitively. Use the `list_allowed_recipients` tool to let the MCP client discover permitted addresses before sending.

### Filtering Incoming Email (Sender Allowlist)

By default, the MCP client can read emails from any sender. You can restrict this to a
trusted set of senders using the `allowed_senders` option, protecting the AI from spam
and prompt-injection attempts via email.

**Option 1: Environment Variable**

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_ALLOWED_SENDERS": "*@trusted-company.com,alice@example.com"
      }
    }
  }
}
```

**Option 2: TOML Configuration**

```toml
allowed_senders = ["*@trusted-company.com", "alice@example.com"]
```

> **Note:** This setting must be at the **top level** of the config file, not inside an
> `[[emails]]` block. If placed inside `[[emails]]`, it is silently ignored with no error.

If `allowed_senders` is not set or left empty, no filtering is applied and the MCP client
can read email from any sender (backwards-compatible default). When non-empty,
`list_emails_metadata` and `get_emails_content` silently exclude emails from unlisted
senders. Patterns support Unix shell-style wildcards via `fnmatch` (`*@example.com`
matches any address at that domain; `alice*@example.com` matches any address starting
with `alice` at that domain). Matching is case-insensitive. Call `list_allowed_senders`
at the start of a session to check which senders the MCP client has access to.

> **Known limitation:** The `total` field returned by `list_emails_metadata` reflects the
> IMAP server-side message count and is not adjusted after sender filtering. The number of
> emails in the response may therefore be lower than `total`. Correcting `total` would
> require fetching all pages from the server, which is impractical.

Emails from filtered senders remain visible in your normal mail client — only the MCP
client's view is restricted.

### Self-Signed Certificates (e.g., ProtonMail Bridge)

If you're using a local mail server with self-signed certificates (like ProtonMail Bridge), you'll need to disable SSL certificate verification:

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
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
    "zerolib-email": {
      "command": "{{ ENTRYPOINT }}",
      "args": ["stdio"]
    }
  }
}
```

If `docker` is avaliable, you can try use docker image, but you may need to config it in your client using `tools` via `MCP`. The default config path is `~/.config/zerolib/mcp_email_server/config.toml`

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

### Installing via Smithery

To install Email Server for Claude Desktop automatically via [Smithery](https://smithery.ai/server/@ai-zerolab/mcp-email-server):

```bash
npx -y @smithery/cli install @ai-zerolab/mcp-email-server --client claude
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

This project is managed using [uv](https://github.com/ai-zerolab/uv).

Try `make install` to install the virtual environment and install the pre-commit hooks.

Use `uv run mcp-email-server` for local development.

## Releasing a new version

- Create an API Token on [PyPI](https://pypi.org/).
- Add the API Token to your projects secrets with the name `PYPI_TOKEN` by visiting [this page](https://github.com/ai-zerolab/mcp-email-server/settings/secrets/actions/new).
- Create a [new release](https://github.com/ai-zerolab/mcp-email-server/releases/new) on Github.
- Create a new tag in the form `*.*.*`.

For more details, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/cicd/#how-to-trigger-a-release).
