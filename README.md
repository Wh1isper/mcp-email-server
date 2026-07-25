# mcp-email-server

[![Release](https://img.shields.io/github/v/release/wh1isper/mcp-email-server)](https://github.com/wh1isper/mcp-email-server/releases)
[![Build status](https://img.shields.io/github/actions/workflow/status/wh1isper/mcp-email-server/main.yml?branch=main)](https://github.com/wh1isper/mcp-email-server/actions/workflows/main.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/Wh1isper/mcp-email-server/graph/badge.svg?token=0mToRybKx8)](https://codecov.io/gh/Wh1isper/mcp-email-server)
[![License](https://img.shields.io/github/license/wh1isper/mcp-email-server)](https://github.com/wh1isper/mcp-email-server/blob/main/LICENSE)

An MCP server for reading, searching, organizing, and sending email through
IMAP and SMTP.

> [!IMPORTANT]
> This branch documents the Local Email App V2 contract. PyPI 0.16.0 and earlier
> use the legacy Gradio/TOML UI and still expose `add_email_account`. Review
> [version availability and upgrade guidance](docs/getting-started.md#version-availability)
> before using an `@latest` command.

## Quick start

### 1. Configure an email account

From this source checkout, run the configuration UI with
[`uv`](https://docs.astral.sh/uv/):

```bash
uv sync
uv run mcp-email-server ui
```

For a published release whose notes state that it includes Local Email App V2,
`uvx mcp-email-server@latest ui` is the equivalent temporary invocation.

Keep the foreground command running. On a truly empty installation, the
authenticated browser session prepares private account storage at the safe local
default; existing TOML or environment configuration instead offers an explicit
import review while the previous settings keep running. The account-first UI has
only **Email accounts** and **Settings & help** as primary destinations. Start
with the email address and password; the UI fills common connection settings from
the email domain and keeps them editable, while outgoing mail remains optional.
A saved complete account is ready without a separate activation step. Use
**Password & test** on the saved account if desired, then restart the MCP client
to apply the selected settings.

### 2. Configure the MCP client

Use the same V2-capable distribution for stdio as for the UI. For a published
V2 release, add the following server definition to the MCP client:

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

Restart the MCP client after updating its configuration. When testing this
source checkout before publication, invoke `uv run --directory
/absolute/path/to/mcp-email-server mcp-email-server stdio` instead of pairing a
managed catalog with PyPI `@latest`.

### 3. Verify the connection

Ask the client to list the configured email accounts or recent messages.

## Other configuration methods

For the SQLite-backed managed CLI workflow, headless environments, containers,
multiple accounts, custom TLS settings, and environment-variable configuration,
see the [documentation](https://mcp-email-server.wh1isper.top/).

## Documentation

- [Getting Started](https://mcp-email-server.wh1isper.top/getting-started/)
- [Configuration](https://mcp-email-server.wh1isper.top/configuration/)
- [MCP Tools](https://mcp-email-server.wh1isper.top/tools/)
- [Transports](https://mcp-email-server.wh1isper.top/transports/)
- [Security](https://mcp-email-server.wh1isper.top/security/)
- [Troubleshooting](https://mcp-email-server.wh1isper.top/troubleshooting/)

## Development

See [CONTRIBUTING.md](https://github.com/wh1isper/mcp-email-server/blob/main/CONTRIBUTING.md).

## License

This project is licensed under the terms of the [LICENSE](https://github.com/wh1isper/mcp-email-server/blob/main/LICENSE).
