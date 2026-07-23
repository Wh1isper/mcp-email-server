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

## Managed mode does not start

Run bounded diagnostics from a terminal:

```bash
mcp-email-server config status
mcp-email-server config doctor
```

Managed startup requires all of the following:

- a parseable owner-only bootstrap with `bootstrap_version = 1`,
  `mode = "managed"`, and `db_location`;
- a present, regular, non-symlink SQLite file in an owner-only immediate parent;
- a supported schema and an `ACTIVE` catalog;
- complete endpoint/binding pairs for enabled accounts;
- readable active credentials in the same operating-system keyring session.

The server deliberately does not fall back to TOML accounts when any of these
checks fails. `config status` still returns bounded bootstrap state and
`catalog_status=unavailable` when the selected database is missing, corrupt,
incompatible, or insecure. In that state, deliberately run `mcp-email-server
config select legacy` and restart; this recovery transition uses a revisioned
bootstrap compare-and-swap and does not open the failed catalog. If the bootstrap
itself is unparseable, repair or restore it manually; `reset` cannot safely infer
its mode and therefore does not unlink it.

A `PENDING` binding usually means a prior keyring write failed. Restore keyring
access and retry with:

```bash
mcp-email-server account set-secret ACCOUNT incoming
```

Use `outgoing` for an SMTP credential. A successful retry retires stale pending
candidates. `CLEANUP_REQUIRED` means a replacement or detachment committed but
an old candidate could not be deleted. Restore keyring access and run:

```bash
mcp-email-server config cleanup-credentials --limit 100
```

Cleanup claims only stale pending or cleanup-required rows and never removes an
active credential. The account remains usable after a rotation cleanup failure;
a credential detachment instead leaves the disabled account incomplete until a
new secret is installed.

`PENDING_REPAIR_REQUIRED` means the keyring write may have completed but could
not be confirmed. Inspect the account revision, then explicitly choose one path:

```bash
mcp-email-server account repair-secret ACCOUNT incoming resume --expected-revision REV
mcp-email-server account repair-secret ACCOUNT incoming rollback --expected-revision REV
```

Resume first verifies the staged value exists; rollback claims it for bounded
cleanup. Neither command accepts a replacement secret or guesses which outcome
occurred. Once either repair transaction commits, a later keyring or SQLite
finalization failure returns `active_cleanup_required` or
`rolled_back_cleanup_required` with the new revision instead of reporting an
uncommitted error. Reconcile the cleanup state; do not replay the old revision.

## A managed write reports a revision conflict

Run `mcp-email-server account show ACCOUNT` and use its current `revision` with
`--expected-revision`. Update, disable, enable, credential removal, and soft
removal use optimistic revisions so a stale operator command cannot overwrite a
concurrent lifecycle or endpoint change. Do not blindly retry: inspect the new
state first, then issue the intended command against that revision.

To remove an account, the confirmation must also exactly match the current name:

```bash
mcp-email-server account remove work \
  --expected-revision 7 \
  --confirm work
```

The operation is a soft removal and does not make the name immediately reusable.

## Metadata index warnings or `query_too_broad`

In legacy mode, an owner, permission, symlink, busy, corrupt, or unsupported
schema problem at `db_location` disables only the rebuildable metadata index.
The application logs a bounded warning and runs the same request through IMAP;
the MCP handler does not bypass the application query service. Correct the
parent directory and database to owner-only access, or remove a disposable
operational database while the server is stopped so it can be rebuilt.

Managed mode is different because the selected database also owns account
authority. An open, security, corruption, schema, or projection-write failure
therefore fails closed rather than returning a result or falling back to TOML.
In legacy mode, a projection write failure after a validated bounded provider
read may return that provider result with a warning; the next request refreshes
again.

`query_too_broad` means an IMAP search returned more than 10,000 candidate UIDs,
so the application could not prove the requested page and exact filtered total
within its work budget. Narrow the mailbox or add a date, sender, recipient,
subject, body, text, flag, or attachment filter. Increasing `page_size` cannot
bypass the limit; `page_size` is restricted to 1 through 100. An `invalid UID
search results` or incomplete provider-metadata error means the server returned
a malformed UID set or did not return exact sender/INTERNALDATE evidence for
every requested UID. The request is rejected rather than expanding a UID range
or returning an incorrect page; retry after the mailbox is stable or report the
provider issue.

## The UI cannot load or authenticate

Run `mcp-email-server ui` in a visible terminal and keep that foreground process
running. Open only the fresh browser link launched by that process. If browser
launch fails, the command prints the one-time URL to that attached terminal. To
suppress browser launch deliberately, use `--no-open` in a real TTY; redirected
or noninteractive stdout/stderr is rejected and never receives the token. A bootstrap
link is single-use and expires after five minutes; replay, a stale tab after
restart, `localhost` substitution, a foreign Origin, or a copied URL whose
fragment was stripped produces the same bounded recovery message. Close the tab
and launch the command again rather than editing the process route or cookie.

The server accepts only exact `127.0.0.1:<actual-port>` requests. A proxy,
browser extension, security product, or custom hosts rewrite that changes Host,
Origin, Fetch Metadata, JSON content type, cookie, or CSRF headers is rejected.
There is no supported remote, wildcard, CORS, or shared-link mode. Managed
catalog/bootstrap operations, attachment writes, and oversized spill require
the documented POSIX filesystem primitives. An unsupported-platform error is a
fail-closed boundary, not a permissions setting that can be bypassed.

If status loads but managed operations fail, run the equivalent bounded CLI
checks in the same operating-system login session:

```bash
mcp-email-server config status
mcp-email-server config doctor
```

A locked or unavailable keyring prevents credential installation and may leave
`PENDING_REPAIR_REQUIRED`. Restore keyring access, refresh the account, then use
the UI's explicit Resume or Roll back action. `CLEANUP_REQUIRED` instead means
the active result is known but an old candidate remains; run bounded cleanup.
Revision conflicts are not retried automatically: inspect the displayed current
summary before resubmitting.

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

## `send_email` reports that SMTP is unavailable

`send_email` is always advertised in the static MCP catalog. If sending fails
for one account, confirm that the selected account is enabled and has a complete
SMTP endpoint and active outgoing credential. In managed mode, inspect it with
`account show`; disable it before changing or removing credentials, then
re-enable it with the latest revision.

## SMTP delivery succeeds but saving to Sent fails

SMTP delivery and the IMAP append are separate operations. A tagged result can
therefore show accepted recipients together with `sent-copy: failed` or
`sent-copy: unknown`. Do not resend the message to repair the copy. List the
provider's folders with `list_mailboxes`, then configure the exact folder:

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
process's working directory. The destination fails closed if any existing parent
is a symlink or not a directory, or if the final target is a symlink, FIFO,
device, or other non-regular file. Choose a real directory and an exact regular
file path; do not work around this check with links.

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

## Delete or move reports failures on an older IMAP server

Message-scoped delete requires IMAP `UIDPLUS` and uses target-scoped
`UID EXPUNGE`. It deliberately never falls back to mailbox-wide `EXPUNGE`,
because that could remove unrelated messages already marked `\Deleted` by
another client.

When a server lacks both native `MOVE` and `UIDPLUS`, `move_emails` also rejects
the COPY-and-delete fallback before copying. Use the provider's native client or
upgrade/configure the server to support `MOVE` or `UIDPLUS`.

## A mutation result contains `unknown` or `reconciliation needed`

`unknown` means the remote effect may have started but the connection did not
return authoritative completion evidence. The server deliberately does not
retry. Inspect the target mailbox, flags, Message-ID, or provider delivery
records before deciding whether a narrow manual retry is safe.

`reconciliation needed` means the remote outcome is known, but invalidating the
local metadata projection failed. The projection is disposable; correct the
operational database problem and refresh metadata. Do not undo or repeat the
provider effect merely to repair local index state.

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

## Legacy import reports a conflict or missing credential

Run `mcp-email-server config import-legacy` without `--apply` to preview again.
A conflict means the staging destination differs from the stored TOML account or
retains that name from a soft removal. Import checks all such conflicts before
resolving secrets or writing, and it will not overwrite the destination. Use a
fresh staging database or reconcile the destination manually.

Preview intentionally ignores environment-only accounts and never accesses the
keyring. Run `config import-legacy --apply`, review the full non-secret plan, and
type `IMPORT` only when prompted. Apply reads required stored credentials. If it
reports a missing credential, unlock or repair the legacy keyring entry and
repeat the reviewed apply. A stale-preview error means the source or an exact
catalog, policy, or account target revision changed; create and review a new
preview rather than retrying an old confirmation. Matching account rows are
reused and only missing bindings are resumed; the stored TOML and legacy keyring
entries are never deleted.

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
