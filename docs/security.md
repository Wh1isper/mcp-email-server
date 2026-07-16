# Security

An email MCP server can read private messages, modify mailboxes, send messages,
and access local files. Review the controls on this page before exposing it to
an MCP client or network.

## Credential storage

Persistent configuration is stored in
`~/.config/mcp-email-server/config.toml` by default. The `credential_storage`
setting controls where passwords are written.

### `auto`

`auto` is the default. The server performs a live usability check against the
active operating system keyring backend:

- macOS commonly uses Keychain.
- Linux desktop environments commonly use Secret Service through GNOME Keyring
  or KWallet.
- Other platforms use the backend selected by the Python `keyring` package.

If the keyring works, secrets are stored there. If no usable backend is
detected, such as in many headless Linux sessions or containers, the server
falls back to the TOML file and logs a warning. The usability result is cached
for the life of the process, so restart after unlocking or repairing a backend
that failed its first probe.

### `keyring`

`keyring` requires a usable keyring. A failed keyring write is reported instead
of falling back to plaintext.

Use this mode when storing credentials outside the operating system keyring is
not acceptable:

```toml
credential_storage = "keyring"
```

### `plaintext`

`plaintext` writes credentials directly into the TOML file and never uses the
keyring for normal loads or saves:

```toml
credential_storage = "plaintext"
```

On POSIX systems, the file is created atomically with owner-only `0600`
permissions. On non-POSIX systems, the application does not install an
equivalent owner-restricted ACL. Protect the file using operating system or
container controls.

### Keyring representation

When keyring storage is active, the TOML file contains `__KEYRING__` instead of
the secret. The actual value is stored under:

```text
service: mcp-email-server
entry: <account_name>:<incoming|outgoing|api_key>
```

`__KEYRING__` is reserved and cannot be used as a real password.

### Environment-provided secrets

`credential_storage` controls only credentials persisted by mcp-email-server.
It does not move or protect a password supplied through an MCP client JSON
file, process environment, CI configuration, or container metadata.

Prefer the secret injection facility provided by the MCP client, CI system, or
container platform. If a literal secret must be stored in a client
configuration, restrict that file to the account running the client and keep it
out of version control and diagnostic output.

Do not use the UI or call `add_email_account` in a process with secret-bearing
environment overrides unless persisting the effective runtime settings is
intended. A normal settings save serializes that runtime view.

## Credential migration

Move all credentials represented by the stored configuration into the keyring:

```bash
mcp-email-server migrate-credentials --to keyring
```

Move referenced keyring credentials back into the TOML file:

```bash
mcp-email-server migrate-credentials --to plaintext
```

Migration operates on the stored TOML file. It intentionally ignores
environment-provided accounts, allowlists, boolean overrides, and the
credential storage environment override while loading the source data.

If `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE` is set to a different mode, the command
warns because future server runs will continue to obey the environment value.
Unset it or keep it synchronized with the intended storage mode.

A plaintext migration attempts to delete the keyring entries referenced by the
original file. It reports entries that remain or whose removal cannot be
verified.

## Keyring limitations

### Application-specific Keychain access

On macOS, Keychain access control can be associated with an executable. A fresh
`uvx` resolution may run the server from a different path than the process that
stored the secret. Keychain can then display a permission prompt or deny access.
Choose the appropriate persistent permission when prompted, or use a stable
installation path.

### Backend trust

The `auto` usability check verifies that the active backend can store and read a
probe value. It does not audit how a third-party keyring backend protects data.
If custom backends are installed, verify that the selected backend meets the
required security properties.

### Non-transactional backends

Writing secrets to the keyring and replacing the TOML file are separate
operations. A crash between them can leave an orphaned keyring entry or a
configuration marker whose corresponding write did not complete. Migration
reports cleanup failures, but backup and recovery remain the operator's
responsibility.

## Recipient allowlist

By default, the server can address any recipient. Restrict both `send_email`
and `save_to_mailbox` with exact addresses:

```toml
allowed_recipients = [
  "alice@example.com",
  "bob@example.com",
]
```

Or use a comma-separated environment variable:

```bash
MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS='alice@example.com,bob@example.com'
```

Every To, CC, and BCC address must be allowed. Matching is case-insensitive and
understands display-name forms such as `Alice <alice@example.com>`.

When the list is configured, `list_allowed_recipients` becomes visible to MCP
clients. An empty list permits all recipients.

## Sender allowlist

Restrict incoming messages by exact address or glob pattern:

```toml
allowed_senders = [
  "alice@example.com",
  "*@company.example",
]
```

Or:

```bash
MCP_EMAIL_SERVER_ALLOWED_SENDERS='alice@example.com,*@company.example'
```

Matching is case-insensitive and applies to the single address parsed from the
message's `From` header. Malformed, empty, or multi-address `From` headers fail
closed when the allowlist is active.

The allowlist protects:

- Metadata listing and pagination.
- Body retrieval and optional read marking.
- Attachment download.
- Deletion and read-state mutations.
- Move and archive operations.

A blocked message's body and attachments are not fetched or marked as read. By
default, blocked mutation IDs are returned as successful no-ops so the caller
cannot distinguish a hidden message from a nonexistent one.

Set this option to report blocked IDs as failures instead:

```toml
report_blocked_mutations = true
```

This is more explicit but reveals that a blocked message exists. When the
sender list is configured, `list_allowed_senders` becomes visible to MCP
clients.

The sender allowlist is local filtering, not sender authentication. A spoofed
`From` header can match. Continue to rely on provider-side SPF, DKIM, DMARC,
and spam controls.

## Attachment access

Attachment downloads are disabled by default because the tool writes data from
email to the server's filesystem.

Enable the operation with:

```toml
enable_attachment_download = true
```

Or:

```bash
MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD=true
```

Use an absolute destination path with `download_attachment` so the target is
unambiguous. The implementation also accepts relative paths and resolves them
against the server process's working directory. Run the server with filesystem
permissions that limit where it can write, and do not assume attachments are
safe to open or execute.

The separate `attachments` parameter on `send_email` and `save_to_mailbox`
reads local file paths. Relative paths are likewise resolved against the server
process's working directory. Only connect clients that should be trusted to
request access to files visible to that process.

## TLS certificate verification

Keep `verify_ssl = true` for remote IMAP and SMTP services. Disabling
verification permits interception and credential exposure if the network or
endpoint is not fully trusted.

If both `use_ssl` and `start_ssl` are false, there is no TLS layer and
`verify_ssl` has no effect. Credentials and message contents may cross the
network in plaintext. Use that mode only for a trusted local bridge, an
encrypted tunnel, or an isolated network; remote services should use implicit
TLS or STARTTLS.

A trusted local bridge with a self-signed certificate can require:

```toml
[emails.incoming]
use_ssl = false
start_ssl = true
verify_ssl = false
```

Limit this exception to the specific local connection. See
[ProtonMail Bridge and self-signed TLS](guides.md#protonmail-bridge-and-self-signed-tls).

## HTTP transport security

SSE and Streamable HTTP validate `Host` and `Origin` headers by default to
reduce DNS rebinding risk. Network exposure still requires appropriate
authentication, authorization, TLS termination, and firewall policy around the
server.

See [Transports](transports.md#dns-rebinding-protection) for allowed host and
origin settings.
