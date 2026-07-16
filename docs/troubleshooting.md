# Troubleshooting

Start by running the relevant command with a visible terminal so server logs and
keyring prompts are not hidden by the MCP client.

Set a more detailed log level when needed:

```bash
MCP_EMAIL_SERVER_LOG_LEVEL=DEBUG mcp-email-server stdio
```

Restart the server after changing configuration paths or environment variables.

## The server reports `Missing command`

The CLI requires a subcommand. Use one of:

```bash
mcp-email-server stdio
mcp-email-server sse
mcp-email-server streamable-http
mcp-email-server ui
```

For local development, use `uv run mcp-email-server stdio` rather than
`uv run mcp-email-server`.

## An environment account does not appear

An environment-provided account requires all three variables:

```text
MCP_EMAIL_SERVER_EMAIL_ADDRESS
MCP_EMAIL_SERVER_PASSWORD
MCP_EMAIL_SERVER_IMAP_HOST
```

The generic password remains required even when
`MCP_EMAIL_SERVER_IMAP_PASSWORD` is set. Invalid integer ports or invalid
account fields cause the environment account to be skipped and an error to be
logged.

If the environment account has the same `MCP_EMAIL_SERVER_ACCOUNT_NAME` as a
TOML account, it replaces that entire account for the current process rather
than merging individual fields.

## A different configuration file is loaded

The default file is:

```text
~/.config/mcp-email-server/config.toml
```

`MCP_EMAIL_SERVER_CONFIG_PATH` selects another path. The path is resolved when
the configuration module is imported, so restart the server after changing it.

On first use, the server can copy a legacy file from:

```text
~/.config/zerolib/mcp_email_server/config.toml
```

Check server logs for the resolved path.

## The UI cannot load accounts

If the UI displays a keyring error, the TOML file probably contains
`__KEYRING__` markers whose secrets cannot currently be read. Unlock or restore
the operating system keyring, approve any pending Keychain prompt, or migrate
the credentials to plaintext after keyring access is restored:

```bash
mcp-email-server migrate-credentials --to plaintext
```

If the stored configuration is no longer recoverable, reset it and re-add the
accounts:

```bash
mcp-email-server reset
```

`reset` removes all persistently configured accounts and performs best-effort
keyring cleanup.

## Keychain repeatedly asks for permission

On macOS, Keychain access can be associated with the application path. `uvx`
may resolve a new executable path after an update, causing another prompt.
Grant the appropriate persistent permission when prompted or install the
package at a stable path and point the MCP client to that executable.

## A keyring-stored secret cannot be resolved

The error identifies the service and entry, for example:

```text
service: mcp-email-server
entry: work:incoming
```

Check that:

- The keyring is unlocked and available in the server's session.
- The entry was not removed by another application or cleanup operation.
- The server process has access to the same keyring as the configuration UI.
- A macOS Keychain access prompt is not waiting behind another window.

Re-add the account if the referenced secret no longer exists.

## `credential_storage` is `plaintext` but the file contains `__KEYRING__`

The file references keyring entries while the active mode refuses to resolve
them. Use one of these approaches:

- Remove the `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE=plaintext` override.
- Change the stored mode back to `auto` or `keyring` long enough to load it.
- Run `mcp-email-server migrate-credentials --to plaintext` while the keyring
  is accessible.

Do not replace `__KEYRING__` with an unknown value; it is only a marker.

## Credential migration appears to have no effect

Check `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE`. If it remains set, every later run
uses that value even when a migration wrote a different mode to the TOML file.
The migration command prints a warning when the values conflict.

Migration changes only persistent TOML accounts. It does not migrate an
account supplied solely through environment variables.

## `send_email` is missing

`send_email` is advertised only when at least one configured account contains
an SMTP `outgoing` section or `MCP_EMAIL_SERVER_SMTP_HOST` is set for the
environment account.

If the tool is visible but sending fails for one account, confirm that the
selected `account_name` itself has SMTP settings. Visibility is based on all
accounts, not the selected one.

## SMTP delivery succeeds but saving to Sent fails

SMTP delivery and the IMAP append are separate operations. List the provider's
folders with `list_mailboxes`, then configure the exact folder:

```toml
[[emails]]
account_name = "work"
save_to_sent = true
sent_folder_name = "INBOX.Sent"
```

Set `save_to_sent = false` if the provider already stores sent messages and an
additional append is unnecessary.

## IMAP or SMTP TLS fails

Verify that the port and TLS mode match the provider:

| Connection        | Common settings                                 |
| ----------------- | ----------------------------------------------- |
| IMAP implicit TLS | Port 993, `use_ssl = true`, `start_ssl = false` |
| IMAP STARTTLS     | Port 143, `use_ssl = false`, `start_ssl = true` |
| SMTP implicit TLS | Port 465, `use_ssl = true`, `start_ssl = false` |
| SMTP STARTTLS     | Port 587, `use_ssl = false`, `start_ssl = true` |

Do not enable both implicit TLS and STARTTLS. Disable certificate verification
only for a trusted local endpoint with a known self-signed certificate.

For ProtonMail Bridge, copy the host, ports, username, and password shown by the
bridge rather than using the normal account password.

## Attachment download is denied

The tool is visible even when permission is disabled. Enable it explicitly:

```toml
enable_attachment_download = true
```

Or:

```bash
MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD=true
```

Use an absolute `save_path` when possible and ensure the server process can
write to its parent directory. A relative path is resolved against the server
process's working directory.

## A message mutation reports success but nothing changed

With `allowed_senders` configured, blocked message IDs are reported as
successful no-ops by default. This prevents callers from using mutation results
to discover hidden messages.

To report blocked IDs as failures instead:

```toml
report_blocked_mutations = true
```

Also confirm that the `email_id` belongs to the mailbox supplied to the
mutation tool.

## Archive folder cannot be found

`archive_emails` first looks for an RFC 6154 `\Archive` flag and then checks
`Archive`, `Archives`, and `[Gmail]/All Mail`.

Call `list_mailboxes` to discover the actual folder and use `move_emails` with
an explicit destination when the provider uses another name.

## HTTP requests are rejected by `Host` or `Origin` validation

For a container, proxy, or non-loopback hostname, configure the names seen by
the server:

```bash
MCP_ALLOWED_HOSTS='mail-mcp.example.com,mcp-email-server'
MCP_ALLOWED_ORIGINS='https://mail-mcp.example.com'
```

A wildcard bind such as `0.0.0.0` does not tell the server which public
hostname a request will use. Do not disable DNS rebinding protection merely to
avoid configuring an explicit allowlist.

See [DNS rebinding protection](transports.md#dns-rebinding-protection).

## Duplicate account name

Account names must be unique across all stored account types. Choose a new
`account_name`, or remove the existing account before adding its replacement.

An environment account with the same name as a TOML email account is the one
exception: it intentionally replaces that account in the runtime view.

## Collect information for a bug report

Include:

- Operating system and version.
- Python and `mcp-email-server` versions.
- Installation method, such as `uvx` or `pip`.
- Transport and MCP client.
- IMAP/SMTP provider and TLS mode, without credentials.
- Relevant logs with email addresses, message contents, tokens, and passwords
  removed.
- Minimal steps to reproduce the problem.

Report issues at <https://github.com/wh1isper/mcp-email-server/issues>.
