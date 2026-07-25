# Transports

> **Version scope:** Managed startup and the embedded React UI on this page are
> Local Email App V2 behavior. See [Version availability](getting-started.md#version-availability)
> before using these commands with a PyPI installation.

mcp-email-server supports stdio, SSE, and Streamable HTTP transports. Use stdio
for a local MCP client unless a network transport is specifically required.

## CLI commands

```text
mcp-email-server --version
mcp-email-server stdio
mcp-email-server sse [--host HOST] [--port PORT]
mcp-email-server streamable-http [--host HOST] [--port PORT]
mcp-email-server ui [--no-open] [--port PORT]
mcp-email-server reset --confirm RESET [--json]
mcp-email-server migrate-credentials [--to keyring|plaintext] [--json]
mcp-email-server config {init|status|doctor|index-health|policy|update-policy|cleanup-credentials|import-legacy|select}
mcp-email-server account {add|set-secret|list|show|update|disable|enable|remove|remove-secret|test}
mcp-email-server-plugin
```

`mcp-email-server-plugin` is the dedicated plugin entry point. It accepts no
transport or management command and starts the same bounded stdio server directly.
Because this entry point is introduced with the mail-only Local Email App V2
catalog, resolving `mcp-email-server@latest` fails closed on earlier packages
instead of starting their legacy MCP surface.

Run `mcp-email-server COMMAND --help` or
`mcp-email-server config|account COMMAND --help` for current options. Managed
setup and mode selection are documented in
[Configuration](configuration.md#managed-cli-setup). Every finite nested
management command has a leaf `--json` option, for example
`mcp-email-server config status --json`; see
[Machine-readable CLI output](configuration.md#machine-readable-cli-output).
`--help`, `--version`, transports, and the foreground UI remain text/protocol
interfaces rather than CLI result documents. The managed CLI is the low-level
agent management API and retains catalog, revision, binding-state, and
restart-state terms. Its JSON documents use `schema_version: 1`, typed error
codes with fixed safe messages, and post-operation revision/restart data; JSON
never grants authority to run a command, and secret-writing commands accept
secrets only from user-controlled stdin. `account test` remains the agent-facing
provider-connectivity diagnostic. The Web UI intentionally has no corresponding
connectivity control or route.

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
because stdout carries newline-delimited UTF-8 MCP JSON-RPC frames. Local Email
App V2 rejects malformed UTF-8/JSON and frames larger than 2 MiB with bounded,
redacted diagnostics, remains usable after a rejected frame, propagates MCP
cancellation, and cancels in-flight work before cleanup on EOF.

The process resolves the bootstrap mode at startup. An explicitly selected
managed mode loads only the exact supported catalog schema and its active secret
bindings. Linux resolves those bindings from the owner-only managed SQLite
secret store; non-Linux platforms that satisfy the managed catalog's required
POSIX filesystem guarantees use the system keyring. A missing, corrupt,
incompatible, or insecure selected catalog fails closed and never falls back to
preserved legacy TOML accounts. Restart stdio after every `config select`
command.

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

Open the foreground managed management interface with:

```bash
mcp-email-server ui [--no-open] [--port PORT]
```

The UI always binds exactly to `127.0.0.1`; port `0` is the default. It does
not read `MCP_HOST`, `MCP_PORT`, HTTP transport allowlists, or framework sharing
and debug settings. `--no-open` suppresses browser launch and prints the
one-time fragment URL only to an attached stdout/stderr TTY. The same TTY-only
fallback is used when automatic browser launch reports failure. Without an
attached TTY, startup fails before serving instead of sending the token through
a pipe or log. After authentication, empty-install account-storage preparation
is a CSRF-protected browser POST to a backend-selected local path, not a startup
or GET side effect; detected legacy content requires an explicit preparation
action. Keep the process in the foreground and use SIGINT or SIGTERM for
graceful session invalidation and shutdown.

Remove persistent legacy configuration with:

```bash
mcp-email-server reset --confirm RESET
```

Move credentials between the TOML file and operating system keyring with:

```bash
mcp-email-server migrate-credentials --to keyring
mcp-email-server migrate-credentials --to plaintext
```

Reset deletes every persistent legacy account and performs best-effort cleanup
of its referenced keyring entries, so exact uppercase confirmation is mandatory.
Reset and credential migration are legacy compatibility operations and are
rejected while managed mode is selected. Both support one-document `--json`
results for user-owned automation. The UI and nested `config`/`account`
commands are equivalent managed management adapters; the UI can also guide an
explicit import while legacy mode is selected. Credential behavior and migration
caveats are covered in [Security](security.md#credential-migration).
