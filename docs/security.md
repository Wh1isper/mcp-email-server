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
assets and explicit management use cases. There is no mail, arbitrary file,
generic RPC, OpenAPI, metrics, or unauthenticated health route. Before binding,
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

Every response uses `Cache-Control: no-store`, a same-origin CSP with framing,
objects, forms, base changes, workers, and remote runtime assets disabled,
`nosniff`, `no-referrer`, frame denial, and a restrictive permissions policy.
The application ships no CDN code, remote font, analytics, telemetry, service
worker, or runtime asset download. Secret values occur only in protected
credential mutation bodies, are cleared by the frontend after every outcome,
and never enter URLs, browser storage, responses, logs, or conflict summaries.

The route and cookie names are random defense in depth, not substitutes for the
session checks. This is explicitly a local single-user trust boundary. A
malicious process running as the same operating-system user can generally inspect
or replace user-owned state; same-UID hostile path replacement is not claimed to
be contained. Do not treat loopback, owner-only modes, or path preflight as a
multi-user or hostile-same-UID sandbox.

## Managed credential storage

Managed mode requires an operating-system keyring backend and never falls back
to TOML plaintext. SQLite stores only random opaque binding references and
bounded lifecycle states. A create or rotation writes a new immutable keyring
candidate, commits it as active only if the account revision still matches, and
records the old binding as cleanup-required in that same transaction. It then
best-effort removes the old candidate and marks cleanup complete. A crash before
deletion therefore remains visible to `config doctor`. The active credential is
never overwritten in place.

Credential removal is available only for a disabled account. It atomically
detaches the binding and increments the account revision before attempting the
external keyring deletion, so an enabled provider operation cannot continue to
use a credential that is being removed. A successful replacement atomically
claims any older pending candidates before deleting them, so concurrent
activation cannot turn a cleanup target into an active missing secret. Failed
deletion remains
`CLEANUP_REQUIRED`. `config cleanup-credentials` processes at most 100 rows per
invocation and first atomically claims stale `PENDING` candidates older than five
minutes; it never deletes an active binding. This claim prevents concurrent
activation from racing a cleanup delete.

Enter managed credentials through the masked prompt or `--password-stdin`.
Never place them in argv. `config doctor`, account summaries, errors, logs, and
MCP results do not expose secret values or candidate locators. The CLI `--json`
mode uses reviewed presentation fields rather than recursively serializing
application objects; it likewise omits secret values, candidate locators,
database paths, and import preview tokens. Secret-writing JSON commands require
`--password-stdin` only to preserve a single result document and remain
user-operated; JSON does not make an agent a safe credential channel. A missing
or unreadable active secret fails closed rather than selecting a legacy account
or plaintext fallback.

Managed bootstrap/catalog support requires POSIX ownership, no-follow,
directory-descriptor, and advisory-lock primitives. Bootstrap files, their
immediate parent directory, the SQLite database, sidecars, and lock must be
owned by the current user and must not grant group or world access. Symlinked,
hard-linked where forbidden, or non-regular paths are rejected. New directories
and files are created with `0700` and `0600` permissions respectively. Selection
changes compare the expected monotonic bootstrap revision while holding the
private sibling lock, preventing concurrent last-writer-wins authority changes.
A platform without these guarantees fails before creating managed targets rather
than using a weaker fallback. Correct permissions before retrying; do not bypass
these checks by moving secrets into the catalog.

The managed secret service is separate from legacy account-name-based entries.
Its internal candidate names are intentionally not a diagnostic or user-facing
contract.

Soft account removal is a tombstone plus bounded credential-cleanup operation.
It retains stable operational identity, endpoint rows, and binding metadata, but
claims every referenced candidate as `CLEANUP_REQUIRED` before committing the
tombstone and then attempts to delete up to 100 referenced keyring values.
Successful deletions are finalized as superseded; unavailable keyring or
post-commit bookkeeping leaves conservative cleanup state for `config doctor`
and `config cleanup-credentials`. A tombstoned account has no provider authority
or public per-role credential-removal command.

### Legacy import security

`config import-legacy` previews stored TOML only. Preview ignores environment
overlays, does not resolve plaintext or keyring credentials, and performs no
managed write. The plan exposes only non-secret endpoint/policy settings,
credential source classes, and exact target revisions. CLI apply displays that
plan before reading its interactive confirmation; UI confirmation is bound to a
one-time preview token. Confirmed apply is limited to `STAGING`, checks all
destination conflicts before resolving any secret, revalidates reviewed source
identity and target revisions before each credential resolution/write, and
installs credentials through the same immutable candidate protocol as manual
setup. It never deletes or rewrites the legacy source or its keyring entries, and
does not activate or select managed mode. Provider-style legacy accounts are
reported unsupported rather than partially imported.

## Temporary oversized results

A large but otherwise valid `get_emails_content` result may exceed the inline
MCP serialization ceiling. The private spill directory is allocated lazily only
when that result variant is needed, so management commands and bounded inline
reads neither create nor depend on temporary spill storage. The local server
stores the canonical JSON in a randomly named process-private temporary
directory and returns its
exact local path, byte count, SHA-256 digest, media type, and lifetime notice.
The directory and files are owner-only; creation is exclusive and no-follow,
and file type, identity, link count, mode, size, and digest inputs are checked
around the write. This spill variant is available only when the required POSIX
primitives exist. Otherwise bounded inline results still work, while a result
that would require spill fails with a bounded error.

These artifacts contain private message content. They are not credentials, are
never placed in SQLite or configuration, and are removed on graceful server
shutdown. A crash can leave an OS temporary artifact, so normal host temporary
storage protections and cleanup still apply. No HTTP route, generic MCP file
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

Neither MCP nor the local management UI accepts legacy environment secrets for
account-management writes. Treat environment-composited accounts as runtime
compatibility inputs and migrate them through an explicit preview/apply flow.

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

`list_allowed_recipients` is always visible in the static MCP tool catalog and
returns an empty list when unrestricted. An empty list permits all recipients.

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

This is more explicit but reveals that a blocked message exists.
`list_allowed_senders` is always visible in the static MCP tool catalog and
returns an empty list when unrestricted.

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
client's pending deletions. Metadata projection invalidation is rebuildable: if
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
on the exact requested target. On POSIX it creates and traverses parent
components through pinned no-follow directory descriptors, rejects unsafe
ownership or writable non-sticky parents, and validates an existing target as a
single-link, owner-only regular file. It writes a random exclusive sibling with
`0600` mode, fsyncs and verifies identity/type/owner/link-count/mode/size, then
atomically replaces the exact destination through the pinned parent descriptor
and verifies the final identity again. A failed pre-replace write preserves an
existing file and removes only a temporary name whose identity is proven. A
platform without the required POSIX no-follow and directory-descriptor primitives
rejects attachment materialization before creating a parent or file; no weaker
path-based fallback is used. An existing private regular file at the exact
requested path can be replaced.

The POSIX descriptor traversal prevents provider-controlled filenames and common
symlink/FIFO/device races from redirecting the write; these checks are not a
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
