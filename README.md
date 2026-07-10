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

We recommend [uv](https://github.com/astral-sh/uv) to manage your environment.

Run the configuration UI once to set up your account, then point your MCP client at the server:

```bash
uvx mcp-email-server@latest ui
```

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

The package is also on PyPI (`pip install mcp-email-server`), as a Docker image (`ghcr.io/ai-zerolab/mcp-email-server`), and on [Smithery](https://smithery.ai/server/@ai-zerolab/mcp-email-server).

**See the full [Installation & Configuration guide](https://ai-zerolab.github.io/mcp-email-server/installation/)** ([docs/installation.md](docs/installation.md)) for environment-variable configuration, credential storage (OS keyring), HTTP transport security, attachment downloads, allowlists, and self-signed certificate setups.

## Permissions

The server is **read-only by default**. Every tool is gated behind a capability scope, granted via `permissions` in the config file or `MCP_EMAIL_SERVER_PERMISSIONS` (comma-separated):

| Scope      | Grants                                                                          |
| ---------- | ------------------------------------------------------------------------------- |
| `read`     | Always granted: list/read mail, mailboxes, attachments                          |
| `draft`    | `save_to_mailbox` (drafts-type folders only, unless `organize` is also granted) |
| `organize` | `move_emails`, `archive_emails`, `mark_emails_as_read`                          |
| `delete`   | `delete_emails`                                                                  |
| `send`     | `send_email`                                                                     |
| `manage`   | `add_email_account`                                                              |
| `full`     | Everything above                                                                 |

```toml
permissions = ["read", "draft", "send"]  # reads, drafts, and sends — can never delete or move mail
```

Tools outside the granted scopes are hidden from the tool list and rejected if called anyway. Upgrading from a version without scopes? Set `permissions = ["full"]` to restore the old behavior. Details in the [Installation & Configuration guide](docs/installation.md#permission-scopes).

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
