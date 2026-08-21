# Troubleshooting

> **Version scope:** Managed mode and the embedded React UI on this page are
> Local Email App V2 behavior. See [Version availability](getting-started.md#version-availability)
> before using these commands with a PyPI installation.

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

The generic password must be non-empty and remains required even when
`MCP_EMAIL_SERVER_IMAP_PASSWORD` is set. An absent or empty IMAP/SMTP-specific
password falls back to the generic password. Invalid integer ports or invalid
account fields cause the environment account to be skipped and an error to be
logged.

If the environment account has the same `MCP_EMAIL_SERVER_ACCOUNT_NAME` as a
TOML account, it replaces that entire account for the current process rather
than merging individual fields.

## A different configuration file is loaded

The default legacy source and derived bootstrap authority are:

```text
~/.config/mcp-email-server/config.toml
~/.config/mcp-email-server/config.bootstrap.toml
```

`MCP_EMAIL_SERVER_CONFIG_PATH` selects another legacy source path; the bootstrap
sidecar is its sibling with `.bootstrap` inserted before the TOML suffix. The
path is resolved when the configuration module is imported, so restart the server
after changing it. Selection and reviewed import do not rewrite the source file.

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
# For agent/user automation:
mcp-email-server config status --json
mcp-email-server config doctor --json
```

The low-level agent management API uses `schema_version: 1`. JSON callers should
branch on `schema_version`, `ok`, `command`, typed `error.code`, and stable data
rather than matching fixed safe message prose. JSON output grants no authority to
run another command. Managed startup requires all of the following:

- a parseable private bootstrap sidecar with `bootstrap_version = 1`,
  `managed_selection = true`, `mode = "managed"`, and
  `managed_db_location`;
- a present, regular, non-link SQLite file in a private immediate parent under
  the active POSIX or Windows profile;
- the exact supported managed schema.

The server deliberately does not fall back to TOML accounts when a bootstrap or
catalog check fails. An incomplete enabled account is omitted individually and
reported by `config doctor`; it does not block other complete accounts. Active
credentials are resolved only immediately before constructing that account's
provider, from the private managed SQLite secret store on Linux and Windows or
the same macOS system-keyring session. An unreadable secret
therefore fails that account operation and is reported by `config doctor`, rather
than blocking unrelated complete accounts at startup. `config status` still
returns bounded bootstrap state and
`catalog_status=unavailable` when the selected database is missing, corrupt,
incompatible, or insecure. A fresh installation reports
`catalog_status=not_configured`; an agent must hand setup back to the user rather
than collecting credentials. In an unavailable state, deliberately run `mcp-email-server
config select legacy` and restart; this recovery transition uses a revisioned
bootstrap compare-and-swap and does not open the failed catalog. If the bootstrap
sidecar itself is unparseable, repair or restore that sidecar manually; `reset`
cannot safely infer its mode and therefore does not unlink the independent legacy
source.

There are no released managed-catalog users for this pre-release redesign, so
older development catalog schemas are rejected rather than migrated. While
legacy mode is selected, preserve the old file for rollback and initialize a
fresh owner-only path with `mcp-email-server config init --database NEW_PATH`.
Then re-enter accounts or use the reviewed legacy import flow. Fresh setup
selects managed immediately; an existing v1 source remains selected until a
complete import succeeds. Remove the obsolete development catalog only after verifying the
replacement; on Linux and Windows, treat every old catalog copy as
secret-bearing.

If a managed password save fails, correct the reported storage or revision
problem and submit a new value with:

```bash
mcp-email-server account set-secret ACCOUNT incoming
```

Use `outgoing` for an SMTP credential. A failed save leaves no intermediate
binding and does not change the current binding authority. On Linux and Windows,
secret insertion and active binding/revision commit in one managed SQLite
transaction. Check private catalog access and, on Windows, verify local fixed
NTFS and DACL security; on macOS, restore system-keyring access before retrying.
Provider connectivity is diagnosed with `mcp-email-server account test ACCOUNT
incoming|outgoing [--json]`; this agent-facing low-level CLI diagnostic has no
Web UI route or Test connection action. Connectivity checks report only bounded
categories: `timeout`, `endpoint_unavailable`, `credential_unavailable`,
`authentication_or_provider_rejected`, or `tls_or_connection_failed`. Follow the
safe remediation message; raw provider exceptions are intentionally hidden.

`CLEANUP_REQUIRED` means a replacement or detachment committed but an old value
could not be deleted. Restore access to the selected managed secret store and
run:

```bash
mcp-email-server config cleanup-credentials --limit 100
```

Cleanup handles only superseded cleanup-required rows and never removes an active
credential. The account remains usable after a rotation cleanup failure; a
credential detachment instead leaves the disabled account incomplete until a new
secret is installed.

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

The operation is a soft removal. Its normalized-name tombstone permanently
reserves that name in this delivery; it cannot be reused.

## Metadata index warnings or `query_too_broad`

In legacy mode, an owner, permission, symlink, busy, corrupt, or unsupported
schema problem at `db_location` disables only the rebuildable metadata index.
The application logs a bounded warning and runs the same request through IMAP;
the MCP handler does not bypass the application query service. Correct the
parent directory and database to owner-only access, or remove a disposable
operational database while the server is stopped so it can be rebuilt.

Managed mode is different because the selected database also owns account
authority and, on Linux and Windows, contains plaintext managed values in
`managed_secret`. A copied or backed-up catalog on either platform must retain
private protection equivalent to the original and must not be shared as a
non-secret diagnostic artifact. An
open, security, corruption, schema, or projection-write failure
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

Non-ASCII subject, body, text, sender, or recipient filters are sent as
synchronizing UTF-8 IMAP literals with `CHARSET UTF-8`. If a provider rejects the
charset, the search fails without rewriting or dropping the filter. Use the
provider's supported search syntax or an ASCII/narrower criterion; do not assume
that changing the process locale will help, because IMAP date months are always
protocol-defined English tokens.

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
After authentication, ordinary account work is under **Email accounts**;
importing earlier settings, sending/attachment safety, and troubleshooting are
folded under **Settings & help**. Account creation starts with an email address
and password. The suggested server settings remain editable under the account
form's connection disclosures, together with login name, port, security,
certificate, sending, and Sent-folder details. There is no redundant connection
preview; advanced settings and optional outgoing mail stay folded until needed.
Provider connectivity testing is CLI-only. Empty-workspace and settled-ready
banners with no next action are hidden; actionable import, selection, restart,
or conflict states remain visible. There is no catalog activation step, and a
saved account is not a provider-connectivity certification.
There is no supported remote, wildcard, CORS, or shared-link mode. Managed catalog/bootstrap operations, attachment writes, and oversized spill
require the documented POSIX profile or a local fixed NTFS Windows path. An
unsupported-platform/filesystem error is a fail-closed boundary, not a
permissions setting that can be bypassed.

If **Import existing settings** reports that the managed catalog parent must be
owner-only, an existing legacy configuration directory grants group or world
access. Stop every server process, verify that the directory is owned by the
current user and is dedicated to this application, then restrict that directory
before retrying. For the default location on POSIX systems:

```bash
chmod 700 ~/.config/mcp-email-server
```

Do not apply this command to a shared directory or change ownership/permissions
without first inspecting the path. The application deliberately does not chmod
an existing legacy directory on the user's behalf.

If status loads but managed operations fail, run the equivalent bounded CLI
checks in the same operating-system login session:

```bash
mcp-email-server config status
mcp-email-server config doctor
```

A storage failure prevents credential installation but leaves the current
binding authority unchanged. On Linux and Windows, check managed database
access; on Windows also verify that the catalog is on local fixed NTFS with its
private DACL intact. On macOS, restore system-keyring access. Return to **Email
accounts**, open **Password** for the affected account,
and submit a new value.
`CLEANUP_REQUIRED` means the active result is known but an old superseded value
remains. **Email accounts** shows a bounded password-data cleanup action whenever
doctor reports such leftovers, including after the last active account was
removed; you can also use the CLI. Revision conflicts are not
retried automatically: inspect
the displayed current summary before resubmitting. If a write succeeds but the
following account-list refresh fails, the UI hides the older account actions
instead of reusing stale revisions. Choose **Refresh accounts** before making
another change.

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

## `send_email` or `forward_email` reports that SMTP is unavailable

`send_email` and `forward_email` are always advertised in the static MCP
catalog, and both fail their SMTP capability check for an account without an
outgoing endpoint — a forward is refused before its source message is even
read. If sending fails for one account, confirm that the selected account is enabled and has a complete
SMTP endpoint and active outgoing credential. In managed mode, inspect it with
`account show`; disable it before changing or removing credentials, then
re-enable it with the latest revision. Run `account test ACCOUNT outgoing` to
authenticate and verify that the provider accepts the configured account email
address in `MAIL FROM`; the command then issues `RSET` without a recipient or
message body. A successful check does not prove later `RCPT TO` or `DATA`
acceptance.

The configured full name is a display name, not an envelope address. Values that
contain `@`, commas, quotes, or non-ASCII text are quoted or encoded in the
message `From` header, while SMTP uses only the separate account email address.
A partial or failed `send_email` result includes reviewed fixed tags such as
`smtp-mail-rejected`, `smtp-recipient-rejected`, or `smtp-data-rejected` when
available. `smtp-utf8-unsupported` means an envelope addr-spec or an address or
thread header requires internationalized syntax but the SMTP server did not
advertise SMTPUTF8; the server rejects before issuing `MAIL FROM`, `RCPT TO`, or
`DATA`. Use an ASCII addr-spec/header value or a provider with SMTPUTF8 support.
A non-ASCII display name attached to an ASCII address does not trigger this
requirement.

`smtp-8bitmime-required` means either a correctly labeled `8bit` MIME body needs
raw high-bit transport or an SMTPUTF8 message is subject to RFC 6531's mandatory
8BITMIME pairing, but the server did not advertise `8BITMIME`. Use a provider
with that extension or, when SMTPUTF8 is not otherwise required, compose a
7-bit-safe message whose parts use base64 or quoted-printable.
`smtp-mime-transport-invalid` means raw high-bit payload bytes
do not match their declared transfer encoding, so enabling `8BITMIME` would not
make the MIME entity valid; correct or re-encode that source part.
`smtp-binarymime-unsupported` means the message requires a binary transport path,
for example because it declares a binary transfer encoding or contains NUL or
DATA framing that ordinary line-oriented SMTP cannot carry. This client does not
implement `BINARYMIME` with `CHUNKING`/`BDAT`; re-encode the affected leaf part as
base64 before sending. Both failures happen before `MAIL FROM`, so they are known
failures and must not be treated as ambiguous delivery. Results never include the
provider's free-form response text.

For delivery diagnostics, enable `DEBUG` and inspect the bounded SMTP records.
`phase=connect` and `phase=authenticate` cover session setup; `phase=mail`,
`phase=rcpt`, and `phase=data` identify explicit SMTP commands, while
`phase=transaction` covers other transaction preparation; `phase=send` is the
legacy aggregate path; and `phase=cleanup` occurs after a known delivery outcome. A numeric `code` is the SMTP response status. A `category` is a fixed
transport class rather than raw exception or provider text. Logs intentionally
omit usernames, addresses, subjects, bodies, raw MIME, attachments, and provider
response strings at every level. Use the provider's own delivery logs when its
free-form rejection explanation is required.

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

For a message with an internationalized addr-spec or thread-header identifier,
`utf8-append-unsupported` means the IMAP server did not advertise and positively
enable the RFC 6855 UTF8 mode before mailbox selection. SMTP delivery may still
have succeeded. Do not resend; inspect the provider-managed Sent folder, disable
the extra copy when the provider already saves one, or use an IMAP endpoint that
supports `ENABLE` with `UTF8=ACCEPT`/`UTF8=ONLY`.

## IMAP reports a malformed `ID` command

`mcp-email-server` sends at most one compact RFC 2971 `ID` command after login,
and only when the IMAP server advertises the `ID` capability. This form supports
strict parsers such as NetEase while avoiding an optional command on providers
that do not implement the extension.

If logs still show `BAD malformed command`, `IMAP ID command failed`, or a
subsequent `provider_failure`, first upgrade to a release containing the latest
IMAP compatibility fixes. Then run `mcp-email-server account test ACCOUNT
incoming` from a visible terminal. If it still fails, report the provider, server
hostname, application version, and sanitized IMAP command/response sequence.
Never include the username, password, message data, or authentication payload.

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

Normally omit `save_path`. The server then creates a safe randomized file under the
current user's `Downloads/mcp-email-server` directory and returns its absolute
path. It securely creates missing components and gives the application child
private permissions; it does not change permissions on the general Downloads
directory. Windows accepts a redirected Downloads Known Folder when it remains
on safe local fixed NTFS storage and falls back to the profile's `~/Downloads`
when the registry value is unavailable, malformed, or non-absolute. If the resolved location
fails the platform security profile, use an explicit safe local destination
instead.

For an explicit destination, use an absolute `save_path` when possible and
ensure the server process can write to its parent directory. A relative path is
resolved against the server process's working directory. Filesystem support is
checked before provider fetch. The destination fails closed if any parent is a
symlink/reparse point or not a directory, or if the target is linked,
permissive, a FIFO/device, or another non-regular object.

On Windows, use an ordinary local fixed NTFS drive-letter path with a parent
directory below the volume root; direct `C:\\file`-style storage is unsupported.
For an explicit path, the immediate parent must not grant another SID permission
to create, modify, delete, or change security on entries. Read-only ACEs do not
need to be removed. The default application child avoids requiring the Downloads
folder itself to satisfy this sensitive-parent rule. Junctions and all reparse
tags are rejected along with UNC/mapped network paths, `\\?\\`/`\\.\\` device
paths, alternate streams such as `file:stream`, and FAT/exFAT. Move the
destination to local NTFS rather than bypassing the check. Old application temp
files are deleted only after bounded owner/DACL/type/link/identity validation;
a substituted or unverified entry is deliberately left untouched.

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
A conflict means the managed destination differs from the effective legacy
account, collides after managed name normalization, or retains that name from a
soft removal. Planning also rejects normalized collisions within the source and
account-limit overflow. Import checks all such conditions before resolving
secrets or writing, and it will not overwrite the destination. Use a fresh
database or reconcile the destination manually.

Preview includes complete environment-only accounts and environment policy
overrides with legacy runtime precedence, but never reads their secret values or
the keyring. Run `config import-legacy --apply`, review the full non-secret plan,
and type `IMPORT` only when a changed plan prompts. Apply reads required current
TOML, environment, or keyring credentials. If it reports a missing credential,
unlock or repair the legacy keyring entry or restore the environment value and
repeat the reviewed apply. A stale-preview error means the effective source,
selected catalog path/bootstrap revision, or an exact catalog, policy, or
account target revision changed; create and review a new preview rather than
retrying an old confirmation. Matching account rows are reused and only missing
bindings are filled. Once every source account type is supported, even when every
row is already present, run `--apply` without a confirmation prompt or choose
**Finish setup**: finalization privately verifies that each active managed
password equals the current legacy password before cutover.
`import_credential_conflict` means they differ; update or remove the
managed credential and preview again. Import does not guess which password should
win and overwrites neither side. A failed credential save leaves destination
authority unchanged and legacy runtime selected; cleanup-required results need
the reported cleanup. A fully successful
import selects managed automatically only when all source account types are
supported. Unsupported providers keep legacy selected until the user explicitly
chooses otherwise. TOML, environment, and legacy keyring entries are never
deleted.

## Duplicate account name

Account names must be unique across all stored account types. Choose a new
`account_name`. Soft removal does not release a name: its normalized tombstone
remains reserved permanently in this delivery.

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

Report issues at <https://github.com/Wh1isper/mcp-email-server/issues>.
