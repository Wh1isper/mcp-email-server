# Transports

mcp-email-server supports stdio, SSE, and Streamable HTTP transports. Use stdio
for a local MCP client unless a network transport is specifically required.

## CLI commands

```text
mcp-email-server stdio
mcp-email-server sse [--host HOST] [--port PORT]
mcp-email-server streamable-http [--host HOST] [--port PORT]
mcp-email-server ui
mcp-email-server reset
mcp-email-server migrate-credentials [--to keyring|plaintext]
mcp-email-server config {init|status|doctor|cleanup-credentials|import-legacy|activate|select}
mcp-email-server account {add|set-secret|list|show|update|disable|enable|remove|remove-secret|test}
```

Run `mcp-email-server COMMAND --help` or
`mcp-email-server config|account COMMAND --help` for current options. Managed
setup and mode selection are documented in
[Configuration](configuration.md#managed-cli-setup).

## stdio

stdio is the recommended transport for Claude Desktop and other local MCP
clients. The client starts the server and communicates through standard input
and output.

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

Do not write unrelated output to stdout when wrapping a stdio server process,
because stdout carries the MCP protocol.

The process resolves the bootstrap mode at startup. An explicitly selected
managed mode loads only an `ACTIVE` managed catalog and its keyring bindings. A
missing, staging, corrupt, incompatible, or insecure selected catalog fails
closed and never falls back to preserved legacy TOML accounts. Restart stdio
after every `config select` command.

The tool catalog remains static during the process. Account disablement,
re-enablement, credential changes, endpoint changes, and policy changes are
revalidated on each operation and do not rewrite `tools/list`. Normal shutdown,
EOF, cancellation, or transport lifespan exit closes application runtime
resources and discards the process cache.

## SSE

Start the legacy SSE transport with:

```bash
mcp-email-server sse --host localhost --port 9557
```

With the default host and port, the FastMCP endpoints are:

```text
SSE stream:      http://localhost:9557/sse
SSE messages:    http://localhost:9557/messages/
```

MCP clients normally configure the `/sse` URL; the stream tells the client
where to send messages.

The default host is `localhost` and the default port is `9557`. Configure the
SSE bind address with command-line options; `MCP_HOST` and `MCP_PORT` are not
used as defaults by this command.

Prefer Streamable HTTP for new network integrations when the MCP client
supports it.

## Streamable HTTP

Start the server with:

```bash
mcp-email-server streamable-http --host localhost --port 9557
```

Connect the MCP client to:

```text
http://localhost:9557/mcp
```

The host and port can also be supplied as defaults through environment
variables:

```bash
MCP_HOST=0.0.0.0 \
MCP_PORT=9557 \
mcp-email-server streamable-http
```

Explicit `--host` and `--port` options override those defaults.

| Variable                              | Default             | Description                                   |
| ------------------------------------- | ------------------- | --------------------------------------------- |
| `MCP_HOST`                            | `localhost`         | Bind host for Streamable HTTP.                |
| `MCP_PORT`                            | `9557`              | Bind port for Streamable HTTP.                |
| `MCP_ALLOWED_HOSTS`                   | Derived safe values | Comma-separated allowed HTTP `Host` values.   |
| `MCP_ALLOWED_ORIGINS`                 | Derived safe values | Comma-separated allowed HTTP `Origin` values. |
| `MCP_ENABLE_DNS_REBINDING_PROTECTION` | `true`              | Enable `Host` and `Origin` validation.        |

## DNS rebinding protection

Both HTTP transports validate `Host` and `Origin` headers by default. Loopback
hosts and origins are allowed for local use.

When binding to a named non-loopback host, that host is included in the derived
allowlist. When binding to a wildcard address such as `0.0.0.0` or `::`, the
server cannot infer the public hostname. Configure the expected service names
explicitly:

```bash
MCP_HOST=0.0.0.0 \
MCP_ALLOWED_HOSTS='mail-mcp.example.com,mcp-email-server' \
MCP_ALLOWED_ORIGINS='https://mail-mcp.example.com' \
mcp-email-server streamable-http
```

A bare host entry also permits any port on that host. For example,
`mcp-email-server` expands to include `mcp-email-server:*`.

Specify IPv6 literals with brackets:

```bash
MCP_ALLOWED_HOSTS='[::1]:*,[2001:db8::10]:*'
MCP_ALLOWED_ORIGINS='http://[::1]:*,https://[2001:db8::10]:*'
```

Any of the following disables `Host` and `Origin` validation entirely:

```bash
MCP_ENABLE_DNS_REBINDING_PROTECTION=false
MCP_ALLOWED_HOSTS='*'
MCP_ALLOWED_ORIGINS='*'
```

Use these escape hatches only in an isolated development environment. Prefer
explicit allowlists behind containers and reverse proxies.

## Reverse proxies

Preserve the FastMCP endpoint paths through the proxy: `/sse` and `/messages/`
for SSE, or `/mcp` for Streamable HTTP. These are the current SDK defaults
because this project does not override the path settings.

When a reverse proxy terminates TLS:

- Bind the server to a private interface whenever possible.
- Add the externally visible host to `MCP_ALLOWED_HOSTS`.
- Add the browser or client origin, including its scheme, to
  `MCP_ALLOWED_ORIGINS`.
- Preserve the request headers expected by the MCP transport.
- Apply authentication and network access controls at the proxy or surrounding
  platform; transport exposure does not by itself authenticate arbitrary users.

## Other CLI operations

Open the legacy account configuration interface with:

```bash
mcp-email-server ui
```

Remove persistent configuration with:

```bash
mcp-email-server reset
```

Move credentials between the TOML file and operating system keyring with:

```bash
mcp-email-server migrate-credentials --to keyring
mcp-email-server migrate-credentials --to plaintext
```

The UI, reset, and credential migration commands are legacy configuration
surfaces and are rejected while managed mode is selected. Managed accounts use
the nested `config` and `account` commands. Credential behavior and migration
caveats are covered in [Security](security.md#credential-migration).
