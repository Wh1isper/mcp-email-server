# mcp-email-server

mcp-email-server connects MCP clients to email accounts through IMAP and SMTP.
It provides tools for reading, searching, organizing, composing, and sending
email without tying the client to a specific email provider.

## Start here

- [Getting Started](getting-started.md) — configure your first account and MCP client.
- [Configuration](configuration.md) — configure accounts with TOML or environment variables.
- [MCP Tools](tools.md) — understand the tools exposed to MCP clients.
- [Transports](transports.md) — run the server over stdio, SSE, or Streamable HTTP.
- [Security](security.md) — manage credentials, allowlists, TLS, and attachment access.
- [Guides](guides.md) — configure IMAP-only accounts, ProtonMail Bridge, drafts, and replies.
- [Troubleshooting](troubleshooting.md) — resolve common account, keyring, TLS, and transport problems.

## Core capabilities

- Connect to standard IMAP and SMTP servers.
- Configure multiple email accounts in one server.
- Run without SMTP for IMAP-only workflows.
- Search messages by mailbox, date, address, status, subject, body, or text.
- Read long messages in bounded chunks.
- Send plain-text or HTML messages with attachments.
- Preserve reply threading with `In-Reply-To` and `References` headers.
- Save drafts and other composed messages through IMAP.
- Move, archive, mark, and delete messages.
- Store credentials in the operating system keyring when available.
- Restrict outgoing recipients and visible incoming senders.
- Run locally over stdio or remotely over an HTTP transport.

Continue with [Getting Started](getting-started.md) to configure an account and
connect an MCP client.
