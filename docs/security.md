# Security

> **Version scope:** The managed catalog and React UI security model on this
> page is Local Email App V2 behavior. See
> [Version availability](getting-started.md#version-availability) before using
> this guidance with a PyPI installation.

An email MCP server can read private messages, modify mailboxes, send messages,
and access local files. Review the controls on this page before exposing it to
an MCP client or network.

## Local management UI security

`mcp-email-server ui` is a foreground, single-user local adapter. It binds
exactly to IPv4 `127.0.0.1`; port `0` is the default and selects an ephemeral
port. The command exposes no host, wildcard, share, daemon, debug, reload, CORS,
or remote mode. Its process-unique route serves only packaged same-origin React
assets and explicit management use cases. There is no provider-connectivity
control or route, mail, arbitrary file, generic RPC, OpenAPI, metrics, or
unauthenticated health route. Provider diagnostics remain available only through
the low-level `account test` CLI. Before binding,
the process freezes its bootstrap mode and selected catalog. It does not open
the catalog during that freeze, so an unavailable selected catalog can still be
recovered by selecting legacy; status continues comparing against the frozen
startup authority and requires a restart after the change.

At startup, a high-entropy bootstrap value is placed only in a URL fragment.
The default command hands that URL directly to the browser. With `--no-open`, or
when browser launch reports failure, it prints the URL only to an attached
stdout/stderr TTY; without a TTY it fails before serving, so the value is not
written to a pipe or noninteractive log. Fragments are not sent with HTTP
requests. The frontend removes it immediately
with `history.replaceState`, sends it once in an `Authorization` header, and
keeps the returned CSRF value only in memory. The server compares a fixed-size
hash in constant time and atomically consumes the bootstrap. It expires after
five minutes, is attempt-rate-limited, and cannot be replayed.

A successful exchange creates a random process-local session and a
process-unique `HttpOnly`, `SameSite=Strict` cookie scoped to the process route.
Every mutation requires the exact `127.0.0.1:<port>` Host, exact startup Origin,
accepted same-origin Fetch Metadata when present, JSON content type, session
cookie, and separate CSRF header. Request and response bodies are bounded.
Logout, normal shutdown, startup failure, SIGINT/SIGTERM, or process restart
invalidates all tokens and sessions. Authentication failures do not redirect.
GET routes never create preview capabilities or initialize storage. Import
preview and default initialization are CSRF-protected POSTs. Only after
authentication and a status read may the frontend call default initialization,
and only a backend-proven empty installation triggers that call automatically.
The backend chooses the `managed.sqlite3` sibling path, rechecks the effective
legacy source, and atomically compares the initial zero revision plus the absent
bootstrap-file proof against concurrent legacy settings writes. Detected legacy content requires an explicit **Import existing settings**
preparation click and preview review. Fresh initialization selects managed mode
without importing or contacting providers. Legacy preparation keeps the prior
runtime selected; only a fully successful reviewed import with no unsupported
provider types automatically selects managed. Failure leaves legacy selected.

Every response uses `Cache-Control: no-store`, a same-origin CSP with framing,
objects, forms, base changes, workers, and remote runtime assets disabled,
`nosniff`, `no-referrer`, frame denial, and a restrictive permissions policy.
The application ships no CDN code, remote font, analytics, telemetry, service
worker, or runtime asset download. Secret values occur only in protected
credential mutation bodies. The account editor may derive non-secret names and
server suggestions from the typed email domain, but performs no remote discovery.
Password fields live only in the active account editor or that account's
**Password** component; the frontend clears them after every outcome, when
an optional SMTP section is disabled, when the selected account or credential
role changes, and when the component unmounts. Secrets
never enter URLs, browser storage, responses, logs, or conflict summaries.

Web/application errors expose only bounded categories and fixed safe messages,
never exception text. Management access logs contain only a fixed operation id,
bounded allowlisted method, status, and duration. They never contain route/path
parameters or templates, raw paths, URLs or queries, request/response bodies,
account/email/filesystem values, session/CSRF/bootstrap tokens, secrets, or
exception text.

The route and cookie names are random defense in depth, not substitutes for the
session checks. This is explicitly a local single-user trust boundary. A
malicious process running as the same operating-system user can generally inspect
or replace user-owned state; same-UID hostile path replacement is not claimed to
be contained. Do not treat loopback, owner-only modes, or path preflight as a
multi-user or hostile-same-UID sandbox.

## Mail provider log redaction

SMTP diagnostics use bounded phase records such as `phase=mail`, `phase=rcpt`,
`phase=data`, and `phase=cleanup`. Warnings may include a numeric SMTP response
code or one of the fixed transport categories `timeout`, `connection`, `tls`,
`io`, or `unexpected`.

These records remain redacted at every log level. They never include account
usernames, sender or recipient addresses (including BCC), endpoint hostnames,
provider response text, exception text, subjects, bodies, raw MIME, attachment
content, or secret values. Enabling `DEBUG` adds safe phase transitions only; it
does not enable a message-content dump.

## Managed credential storage

Managed mode never falls back to TOML plaintext. Its default managed secret
backend is platform-specific:

- Linux and Windows store values in the dedicated `managed_secret` table inside
  the same private managed SQLite database as the catalog;
- macOS uses the operating-system keyring. Platforms without the complete
  filesystem-security profile are not supported for managed catalogs.

Only the `SecretStore` adapter reads or writes these values. Catalog queries,
projections, CLI/UI responses, diagnostics, and logs never select or expose the
`managed_secret.secret_value` column or a keyring value. On Linux and Windows,
the managed catalog is a secret-bearing database: file copies, snapshots, and
backups include plaintext `managed_secret.secret_value` values. Keep every copy
under protection equivalent to the private original; do not upload, share, or
treat it as a non-secret account database.

A create or rotation stores a new immutable value and commits it as active only
if the reviewed account revision still matches. On Linux and Windows, inserting
`managed_secret`, activating its binding, incrementing the binding/account
revision, and marking any old active value `CLEANUP_REQUIRED` are one SQLite
transaction. On macOS, the new keyring value is written before one
compare-and-swap activation transaction. A conflict or failure before activation
returns an error, best-effort deletes any unreferenced keyring value, persists no
provisional binding, and leaves the current binding authority unchanged.

The active credential is never overwritten in place. Rotation first makes the
new value authoritative and marks the old active value `CLEANUP_REQUIRED`; it
then deletes the old value and clears cleanup state after confirmed success.
Only an external or follow-up deletion failure retains `CLEANUP_REQUIRED`, so a
crash before deletion remains visible to `config doctor` and `config
cleanup-credentials`. Failed saves expose no follow-up password action beyond
retrying a new save after correcting the reported problem.

Credential removal is available only for a disabled account. It atomically
detaches the binding and increments the account revision before attempting value
deletion, so an enabled provider operation cannot continue to use a credential
that is being removed. Failed deletion remains `CLEANUP_REQUIRED`. `config
cleanup-credentials` processes at most 100 cleanup rows per invocation,
revalidates that each value is superseded, and never deletes an active binding.

Enter managed credentials only through user-controlled terminal stdin (the
masked prompt or explicit `--password-stdin`). Never place them in argv. `config
doctor`, account summaries, errors, logs, and MCP results do not expose secret
values or internal locators. The CLI `--json` mode uses reviewed presentation
fields rather than recursively serializing application objects; it likewise
omits secret values, locators, database paths, and import preview tokens.
Secret-writing JSON commands require `--password-stdin` only to preserve a
single result document and remain user-operated; JSON does not make an agent a
safe credential channel. A missing or unreadable active secret fails closed
rather than selecting a legacy account or plaintext fallback.

Managed bootstrap/catalog support requires the complete platform profile.
Selection authority lives in a private sibling sidecar
(`config.bootstrap.toml` for `config.toml`), not in the legacy source. Bootstrap
sidecars, their immediate parent, SQLite database and WAL/SHM sidecars, and locks
must remain private regular non-link objects with stable identity.

On POSIX, the profile uses current-owner checks, `0700` directories, `0600`
files, component no-follow/directory-descriptor traversal, single-link checks,
and bounded `fcntl` locking. On Linux, these controls protect both catalog state
and the `managed_secret` table.

### Windows filesystem boundary

On Windows, managed/bootstrap authority, attachment materialization, and
oversized-result spill support only an ordinary drive-letter path on a local
fixed NTFS volume with at least one validated directory below the volume root.
Direct `C:\\file`-style volume-root storage is unsupported. UNC and mapped network paths, remote drives, `\\?\\`/`\\.\\`
device namespaces, alternate data streams, FAT/exFAT, and unknown filesystem
types fail before parent creation, SQLite/provider work, or credential effects.

Every existing component is opened with reparse traversal disabled and held
without delete sharing while sensitive checks run. File and directory symlinks,
junctions, mount points, and provider-defined reparse tags are rejected. Identity
is the volume serial plus file index read from the held handle; exact files must
be regular and single-link. Private objects are current-user-owned and use a
protected DACL granting only the current user, LocalSystem, and built-in
Administrators. A Windows OWNER RIGHTS/CREATOR OWNER ACE is treated as that
already validated owner, not as an independent principal. Unknown/NULL DACLs,
unsupported allow ACEs, foreign owners, or
any allow access granted to another SID fail closed, including raw generic ACE
masks. Existing safe private parents may be normalized to the protected DACL
only during an explicit managed creation operation; validation-only reads never
rewrite a DACL, and an unsafe parent is never silently made acceptable.

Windows cross-process locks use the maintained `filelock` `NtCreateFile` and
`LockFileEx` path after parent-chain validation. Lock handles reject final
reparse points, deny delete sharing, use a fixed byte range, time out in a bounded
period, and are released by process termination. SQLite DB/WAL/SHM/lock objects
are validated before open and again after WAL setup. On Windows, sidecar
validation runs under the setup lock because SQLite creates an inherited-DACL
sidecar before the adapter can harden it; concurrent local opens wait rather than
rejecting that bounded intermediate state. Every current SQLite sidecar is
rehardened and revalidated because another connection's final close
may delete and recreate WAL/SHM under a new identity. Post-commit reconciliation
contention is logged and deferred rather than misreporting a committed mutation
as failed; the next open still performs strict prevalidation before SQLite.
These NTFS identity and DACL controls protect both catalog state and the
`managed_secret` table, so the Windows catalog and every backup are sensitive.

Private Windows replacement for bootstrap authority, legacy TOML, attachments,
and spill output creates a random same-directory file with `CREATE_NEW` and the
private DACL, writes through its held handle, and calls `FlushFileBuffers`. An
overwrite then uses
`MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)`; no-clobber
legacy migration omits `MOVEFILE_REPLACE_EXISTING`, so a concurrent destination
wins without being overwritten. Neither path sets `MOVEFILE_COPY_ALLOWED`, so a
cross-volume copy/delete fallback is impossible.
The final target is reopened without following reparse points and must match the
temporary object's identity and expected size. A pre-replace failure preserves
the old file; after replacement, an observer sees complete old or complete new
bytes, never a partial file.

Selection changes compare the expected monotonic bootstrap revision while
holding the private sibling lock, preventing concurrent last-writer-wins
authority changes. Historical `db_location` remains the legacy metadata index;
managed selection is stored separately as `managed_db_location`. Selection
writes atomically replace only the sidecar; initialization and import cutover
never reserialize the legacy source. A newly created POSIX legacy parent is
`0700`; a newly created Windows legacy parent and every new or replaced Windows
legacy TOML file receive the protected private DACL before any credential bytes
are written. Existing legacy-only parent directories retain their compatibility
boundary, and a fresh reset with neither source nor sidecar creates no artifact.

A platform without its complete profile fails before managed target, provider,
or bootstrap effects instead of using a weaker fallback. An unavailable macOS
system keyring fails the credential operation without changing binding
authority. Legacy-only store, reset, and credential migration retain their
historical compatibility behavior.

The managed secret service is separate from legacy account-name-based entries.
Its opaque handles are intentionally not a diagnostic or user-facing contract.
Every UI mutation over a selected catalog submits the exact catalog identity and
bootstrap revision bound to the workspace snapshot that produced the displayed
data, in addition to catalog/account revisions. A later status response cannot
retarget that snapshot; a changed selection remounts and reloads the workspace.
The service binds one catalog instance and rejects selection drift before its
next access. Import additionally rechecks selection before each secret resolution
and immediately before every account, credential, and policy write. Preview
capability state is one-time, ten-minute, expired on access, and capped at 32
entries. Cleanup-required state is never treated as a missing binding by import.

Soft account removal is a tombstone plus bounded credential-cleanup operation.
It retains stable operational identity, endpoint rows, and binding metadata, but
marks every referenced value `CLEANUP_REQUIRED` before committing the tombstone
and then attempts to delete up to 100 values. Successful deletions are finalized
as superseded; unavailable storage or post-commit bookkeeping leaves conservative
cleanup state for `config doctor` and `config cleanup-credentials`. A tombstoned
account has no provider authority or public per-role credential-removal command,
and its normalized name remains reserved permanently in this delivery.

### Legacy import security

`config import-legacy` previews the effective legacy TOML/environment view.
Preview parses environment account and policy precedence but does not resolve or
expose plaintext, environment, or keyring credential values and performs no
managed write. Environment password presence is detected by enumerating variable
names without retrieving its value; role/base values are read only during
confirmed apply. The plan exposes only non-secret endpoint/policy settings,
credential source classes, and exact target revisions. CLI apply displays that
plan before reading interactive confirmation; a no-change plan needs no
confirmation but still executes credential proof and guarded finalization. For a
changed UI plan, the user checks an explicit review box and
the adapter submits the fixed `IMPORT` confirmation value; it cannot apply while
the box is clear. UI confirmation is bound to a one-time preview token. The
private preview is additionally bound to the selected catalog path and bootstrap
revision. Confirmed apply preflights normalized-name collisions and account
capacity, checks destination conflicts before resolving any secret, revalidates
reviewed source identity and target revisions before each credential
resolution/write, and installs credentials through the same save protocol as
manual setup. Before an otherwise eligible cutover, an `unchanged` active role is
privately resolved on both sides and must compare equal; mismatch overwrites
neither side and blocks cutover. Its
legacy value and managed account revision remain bound into finalization. Final
automatic cutover holds the shared source/selection lock, rechecks the complete
source snapshot and each proven or imported private credential value,
then holds a SQLite writer fence while checking the final catalog revision and
each imported account revision/enabled state. The bootstrap sidecar CAS completes
before either fence is released. A failed save, any drift, unsupported provider,
or cleanup attention leaves legacy runtime selected. Import never deletes,
rewrites, or reformats TOML, environment, or legacy keyring state. Provider-style
legacy accounts are reported unsupported and prevent automatic cutover rather
than being silently lost.

## Temporary oversized results

A large but otherwise valid `get_emails_content` result may exceed the inline
MCP serialization ceiling. The private spill directory is allocated lazily only
when that result variant is needed, so management commands and bounded inline
reads neither create nor depend on temporary spill storage. The local server
stores the canonical JSON in a randomly named process-private temporary
directory and returns its
exact local path, byte count, SHA-256 digest, media type, and lifetime notice.
The directory and files are private; creation is exclusive and no-follow, and
file type, identity, link count, mode or DACL, size, and digest inputs are checked
around the write. POSIX uses owner-only modes. Windows uses a dedicated
protected-DACL temporary container with a random per-process root on local fixed
NTFS and handle-bound identity. Without the complete
active platform profile, bounded inline results still work while a result that
requires spill fails with a bounded error.

These artifacts contain private message content. They are not credentials, are
never placed in SQLite or configuration, and are removed by identity on graceful
shutdown. A killed process may leave a remnant. Each Windows root holds an
owner-marker `LockFileEx` range for its lifetime; cleanup must first acquire that
range non-blocking, so age never makes a live long-running process eligible. The
next writer then performs bounded prefix/age/type/owner/DACL/link/identity
validation and removes only a fully verified stale root; reparse or substituted
entries are left untouched.
No HTTP route, generic MCP file
reader, directory listing, remote URL, or arbitrary path lookup is exposed by
this feature. Only connect a local MCP client whose own filesystem tools may
legitimately inspect paths returned by the server.

## Indexed metadata privacy

The operational SQLite projection contains no message bodies, raw MIME,
attachment bytes, passwords, tokens, or secret locators. It can contain account
source fingerprints, mailbox names, UIDs, UIDVALIDITY, provider flags, and the
message ID, subject, sender, recipients, and dates required by
`list_emails_metadata`. Treat it as private email metadata even though it does
not contain credentials.

Legacy source fingerprints are one-way hashes of non-secret account identity and
incoming endpoint attributes. Secret values are excluded, and legacy endpoints
are not copied into managed account rows. The database and SQLite sidecars use
the same owner-only, anti-symlink checks as managed catalog storage. Existing
files are checked for exact application schema ownership before WAL is enabled;
an unrelated or unmarked database is rejected without changing its journal mode
or creating WAL sidecars. Deleting the projection does not delete provider mail,
but an untrusted copy can still reveal communication metadata.

## Credential storage

Persistent legacy configuration is stored in
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

On POSIX systems, a new immediate configuration parent uses `0700` and the file
is created atomically with owner-only `0600` permissions; existing legacy parent
permissions are not silently changed. On non-POSIX systems, the application does
not install an equivalent owner-restricted ACL. Protect the file using operating
system or container controls.

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

Neither MCP nor ordinary local-UI account forms read legacy environment secrets.
Treat environment-composited accounts as runtime compatibility inputs and copy
them only through the explicit, reviewed import flow; the environment value is
read during confirmed apply, never during preview.

## Credential migration

These commands migrate legacy TOML credentials only. They are rejected while
managed mode is selected; managed credentials use `account set-secret` instead.

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

Sending is disabled when the allowed-recipient collection is empty. Enable and
restrict both `send_email` and `save_to_mailbox` by adding exact addresses:

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

`list_allowed_recipients` is always visible in the static MCP tool catalog. An
empty result means sending is disabled; it never means unrestricted sending.
The Web UI edits recipients as individual add/edit/remove items and states this
empty behavior explicitly.

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
- Deletion and approved flag/read-state mutations.
- Move and archive operations.

A blocked message's body and attachments are not fetched or marked as read. By
default, blocked mutation IDs are returned as successful no-ops so the caller
cannot distinguish a hidden message from a nonexistent one.

Set this option to report blocked IDs as failures instead:

```toml
report_blocked_mutations = true
```

This is more explicit but reveals that a blocked message exists.
`list_allowed_senders` is always visible in the static MCP tool catalog and
returns an empty list when unrestricted. Unlike recipients, an empty
allowed-sender collection does not restrict reading. The Web UI edits sender
patterns as individual add/edit/remove items.

The sender allowlist is local filtering, not sender authentication. A spoofed
`From` header can match. Continue to rely on provider-side SPF, DKIM, DMARC,
and spam controls.

## Mutation replay safety

IMAP and SMTP connections can fail after a remote server has accepted an
effect but before this process receives the result. Such targets are reported
as `unknown`, not silently retried or rewritten as known failures. Repeating an
unknown send, APPEND, MOVE, delete, or flag update can duplicate delivery or
apply an effect twice; inspect the provider mailbox or delivery evidence first.

Scoped delete and the COPY/delete move fallback require `UIDPLUS` and use only
`UID EXPUNGE`. They never use mailbox-wide `EXPUNGE`, which could commit another
client's pending deletions. The generic `set_email_flags` tool rejects
`\Deleted`, so it cannot bypass this scoped deletion boundary; it also rejects
the server-controlled `\Recent` flag and provider-specific keywords.

Metadata projection invalidation is rebuildable: if
it fails after a provider effect, the result keeps the provider evidence and
adds a reconciliation warning instead of claiming rollback.

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
against the server process's working directory. The application fetches at most
a 50 MiB raw message, accepts at most 25 MiB of decoded attachment bytes, and
passes bytes rather than a path to the artifact writer. The writer operates only
on the exact requested target. Filesystem capability preflight occurs before
credential resolution, provider construction, download, or MIME decoding.

On POSIX, the writer traverses parent components through pinned no-follow
directory descriptors, rejects unsafe ownership or writable non-sticky parents,
and validates an existing target as a single-link owner-only regular file. It
writes a random `0600` sibling, fsyncs, atomically replaces through the pinned
parent descriptor, and verifies final identity and size.

On Windows, the writer applies the [local fixed NTFS boundary](#windows-filesystem-boundary),
rejects every reparse point and hard-linked/permissive target, creates a private
same-directory sibling, flushes it, performs same-volume write-through replace,
and verifies final volume/file identity and size. Pre-replace failure preserves
an existing target. The next operation removes only old prefix-matching temporary
files that still pass owner, DACL, type, link, age, and identity checks. No
platform uses a weaker path-based fallback. An existing private regular file at
the exact requested path can be replaced.

These traversals prevent provider-controlled filenames and common
symlink/junction/FIFO/device races from redirecting the write; they are not a
sandbox around arbitrary paths a trusted MCP caller can request. Run the server
with filesystem permissions
that limit where it can write, and do not assume attachments are safe to open or
execute.

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
