# mcp-email-server

[![Release](https://img.shields.io/github/v/release/wh1isper/mcp-email-server)](https://github.com/wh1isper/mcp-email-server/releases)
[![Build status](https://img.shields.io/github/actions/workflow/status/wh1isper/mcp-email-server/main.yml?branch=main)](https://github.com/wh1isper/mcp-email-server/actions/workflows/main.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/Wh1isper/mcp-email-server/graph/badge.svg?token=0mToRybKx8)](https://codecov.io/gh/Wh1isper/mcp-email-server)
[![License](https://img.shields.io/github/license/wh1isper/mcp-email-server)](https://github.com/wh1isper/mcp-email-server/blob/main/LICENSE)

An MCP server for reading, searching, organizing, and sending email through
IMAP and SMTP.

## Quick start

### 1. Configure an email account

Run the configuration UI with [`uv`](https://docs.astral.sh/uv/):

```bash
uvx mcp-email-server@latest ui
```

Add your IMAP account in the browser window. SMTP is optional if the account
does not need to send email.

### 2. Configure the MCP client

Add the following server definition to the MCP client:

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

Restart the MCP client after updating its configuration.

### 3. Verify the connection

Ask the client to list the configured email accounts or recent messages.

## Other configuration methods

For headless environments, containers, multiple accounts, custom TLS settings,
and environment-variable configuration, see the
[documentation](https://mcp-email-server.wh1isper.top/).

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
