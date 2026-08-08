# Configuration

> **Version scope:** Managed mode and the embedded React UI on this page are
> Local Email App V2 behavior. See [Version availability](getting-started.md#version-availability)
> before using these commands with a PyPI installation.

mcp-email-server supports two explicitly selectable configuration modes:

- `legacy` keeps account configuration in TOML and composes documented
  environment overlays;
- `managed` keeps account authority in private SQLite. On Linux and Windows,
  credentials default to its dedicated `managed_secret` table. Windows requires
  a local fixed NTFS catalog protected by handle-bound DACL, reparse, identity,
  and locking checks. macOS uses the operating-system keyring. Managed catalogs
  are not
  supported without the complete platform filesystem-security profile.

A missing configuration file or a v1 TOML source without separate bootstrap
authority remains `legacy` for backward compatibility. Historical `db_location`
continues to name the rebuildable legacy metadata index; it is not a managed
catalog selection. For the default source `config.toml`, selection authority is
stored separately in the owner-only sibling `config.bootstrap.toml`. Every new
sidecar write records `bootstrap_version = 1`, a monotonic `bootstrap_revision`,
an explicit mode, a `managed_selection` marker, and any selected catalog as
`managed_db_location`. Selection uses the bootstrap revision as a
compare-and-swap token under the private `config.bootstrap.toml.lock`; it is
separate from the managed catalog revision. Selection and import never reserialize
or rewrite the v1 source, so comments, ordering, whitespace, and rollback bytes
remain unchanged. A pre-release combined file is accepted read-only; a later
selection write, or the authority step preceding an explicit legacy source write,
materializes its selection into the sidecar without that step changing source
bytes.

## Configuration file

The default path is:

```text
~/.config/mcp-email-server/config.toml
```

Set `MCP_EMAIL_SERVER_CONFIG_PATH` to use a different legacy TOML source path.
Relative paths are resolved against the server process's working directory. The
bootstrap sidecar is derived in the same directory by adding `.bootstrap` before
the TOML suffix: `/path/work.toml` uses `/path/work.bootstrap.toml`.

On first use, if the current file does not exist and a legacy file exists at
`~/.config/zerolib/mcp_email_server/config.toml`, the legacy file is copied to
the current location automatically. The destination is created owner-only on
POSIX and with the protected private DACL on Windows; concurrent destination
creation is never overwritten. An explicitly managed file at the old path is
not copied as an import-time side effect.

## Local management UI

`mcp-email-server ui [--no-open] [--port PORT]` provides the user-facing managed
management plane through an embedded React application. The low-level CLI is the
complete headless superset, including provider-connectivity diagnostics. The UI
always binds to `127.0.0.1`; the default port `0` asks the operating system for
an ephemeral port. There is no host, share, debug, reload, daemon, or remote-UI
option. The
default opens the one-time URL directly in the browser; `--no-open` requires an
attached terminal and prints the URL only there. A noninteractive invocation
fails before serving rather than exposing its bootstrap token in logs.

After authentication and status inspection, a truly empty installation issues
one CSRF-protected POST that prepares `managed.sqlite3` in the same private
directory as the active source and bootstrap sidecar. The POST rechecks the
effective legacy source, then binds
both the zero revision and absent-file proof under the same secure cross-process
platform lock used by legacy settings writes. No GET mutates state.
If effective TOML/environment legacy content exists, the UI does not initialize
automatically; **Import existing settings** is an explicit preparation and
review action. A fresh installation creates the catalog and selects managed mode
in one step. With existing v1 content, preparation records the migration
destination while legacy remains selected; it does not import or contact a
provider. A complete reviewed import automatically selects managed mode when all
source account types are supported. Failure or unsupported provider types leave
legacy selected, and no path restarts the process automatically.

The UI is account-first and has two primary destinations. **Email accounts**
lists saved accounts and keeps edit, pause/enable, soft removal, and password
rotation in each account's context. The add form is email/password-first.
It derives an editable sender name and account nickname, uses finite presets for
common email services, and otherwise suggests `imap.<email-domain>` and
`smtp.<email-domain>`. Suggestions continue changing only while their fields are
untouched. There is no redundant connection preview. Advanced server, login,
port, security, certificate, and Sent-folder fields are folded behind progressive
disclosure, as is optional outgoing mail; changing an untouched security default
also moves between the corresponding standard ports. **Settings & help** folds
importing earlier settings, sending/attachment safety, account checks, and email
search checks into optional sections and does not load a section until it is
opened. Per-role password changes remain in the affected account's **Password**
context. A failed save displays an error and leaves the current binding authority
unchanged. Provider connectivity testing and its route are absent from
the Web UI; use the CLI `account test` diagnostic instead. When the
catalog reports inactive password data left by a failed external deletion,
**Email accounts** shows one bounded cleanup action even if no active account
remains; cleanup is catalog-wide and never removes an active password.

A contextual status area distinguishes settings chosen for the next restart
from those currently running and exposes import, explicit recovery selection,
and restart steps when actionable. Banners with no next action are hidden for an
empty workspace and for settled ready state. There is no catalog activation or
second save step. Account completeness is evaluated per account: an incomplete
account appears in diagnostics and is omitted from MCP discovery without
blocking complete accounts. Ordinary status, conflict,
credential, import, and troubleshooting messages map implementation states to
task language. Catalog-
dependent content remains unavailable until the catalog is usable. Mutable
catalog operations carry the exact reviewed catalog path and bootstrap revision
as well as the relevant catalog or account revision. A selection change
therefore conflicts instead of redirecting a write to a different catalog with
a coincidentally equal numeric revision. A conflict displays a current bounded
non-secret summary and requires explicit review rather than automatic replay.
Legacy mode remains inspectable and importable, but the UI is not a legacy TOML
editor.

The browser process is only an adapter over shared management application
services. It has no provider-connectivity route, mail, generic RPC, filesystem
browser, OpenAPI, or MCP App surface. Managed catalog and attachment effects,
plus oversized-result spill, require the documented POSIX or Windows
filesystem-security profile and fail closed when that complete profile is
unavailable. See [Security](security.md#local-management-ui-security)
for its one-time bootstrap, platform, and session boundaries.

## Managed CLI setup

A fresh managed setup creates and selects its catalog in one step:

```bash
mcp-email-server config init --database ~/.config/mcp-email-server/catalog.sqlite3

mcp-email-server account add work \
  --email john@example.com \
  --full-name "John Doe" \
  --imap-host imap.example.com \
  --imap-user john@example.com

mcp-email-server account test work incoming
```

Restart MCP clients when `config status` reports `restart_required=true`.

`account add` prompts for the IMAP password with masked input. Add SMTP with
`--smtp-host`, `--smtp-port`, and `--smtp-user`; the command then prompts for a
separate SMTP password. For non-interactive setup, pass `--password-stdin` and
provide one line for IMAP, followed by one SMTP line when SMTP is configured.
Passwords are never accepted as ordinary command-line values.

`config init` creates a usable SQLite catalog with no staging lifecycle. On a
fresh installation it selects managed mode immediately. If it detects effective
v1 TOML accounts, environment configuration, providers, or customized policy,
it records the migration destination but leaves legacy mode in use until the
reviewed import succeeds. Retrying the same owner-only, structurally valid path
adopts it without resetting data or revisions; a corrupt, foreign, insecure, or
incompatible target is rejected. This makes a retry possible if catalog creation
committed but the bootstrap compare-and-swap did not. Initialization cannot
replace a different catalog while managed mode is selected.

Accounts may be added or updated at any time. A complete account becomes
available without catalog activation or another save. An incomplete enabled
account remains visible to `config doctor` but is omitted from runtime discovery;
it does not block complete accounts. `config select managed` validates catalog
schema/security and the reviewed revision, not global account completeness.
Restart every MCP server process after a selection change.

Useful inspection and management commands are:

```bash
mcp-email-server config status
mcp-email-server config doctor
mcp-email-server config index-health
mcp-email-server config policy
mcp-email-server config update-policy --expected-revision 1 \
  --allowed-recipients 'alice@example.test,bob@example.test'
mcp-email-server account list
mcp-email-server account show work
mcp-email-server account set-secret work incoming
mcp-email-server account test work incoming
mcp-email-server account disable work --expected-revision 3
mcp-email-server account enable work --expected-revision 4
mcp-email-server config cleanup-credentials
mcp-email-server config select legacy
```

### Machine-readable CLI output

The managed CLI is the low-level agent management API. Its exact catalog,
revision, binding-state, and restart-state terms are intentional and are more
technical than Web UI task language. Every finite `config` and `account` command
supports a leaf `--json` option, as do `reset` and `migrate-credentials`. Put it
after the command:

```bash
mcp-email-server config status --json
mcp-email-server config doctor --json
mcp-email-server account list --json
mcp-email-server account show work --json
```

A dispatched success writes one JSON document to stdout with
`schema_version: 1`, `ok: true`, a stable `command` identifier, command-specific
`data`, and an always-present `warnings` array. Mutations include the relevant
resulting account, binding, catalog, bootstrap-revision, or restart-state fields.
A dispatched application failure
keeps its nonzero exit status and writes one schema-version-1 document with
`ok: false`, a typed stable `error.code`, that code's fixed safe `message`, and
reviewed non-secret `details`. Click usage errors such as a
missing required option happen before command dispatch and remain normal Click
errors in this release.

Schema-version-1 management failures use the following public codes. Callers must
still treat an unknown code as unsupported rather than guessing from its message.

| `error.code`                   | Meaning and recovery                                                     |
| ------------------------------ | ------------------------------------------------------------------------ |
| `revision_conflict`            | Reviewed state changed; inspect the safe `details` revisions and reread. |
| `account_limit_reached`        | The managed account cap was reached; remove an unused account.           |
| `account_name_exists`          | The normalized nickname is reserved, including by a removed account.     |
| `bootstrap_unavailable`        | Bootstrap authority is invalid, unavailable, or busy; inspect status.    |
| `catalog_not_configured`       | Initialize or select a managed catalog before the catalog command.       |
| `catalog_unavailable`          | The selected catalog cannot be opened or validated; run status/doctor.   |
| `credential_store_unavailable` | Restore access to the selected managed secret store and retry.           |
| `import_credential_conflict`   | Legacy and active managed passwords differ; reconcile before cutover.    |
| `import_preview_stale`         | Source or destination changed, or preview expired; preview again.        |
| `import_target_changed`        | The selected import destination changed; preview the new target.         |
| `invalid_input`                | Correct the command values; framework usage errors remain non-JSON.      |
| `storage_unavailable`          | Required local management storage is inaccessible; repair access.        |
| `management_error`             | A bounded management failure has no narrower recovery category.          |
| `runtime_error`                | An unexpected bounded command failure occurred; inspect status.          |

JSON presentation fields are explicit rather than generic dataclass dumps. They
do not include local configuration or database paths, import preview tokens,
secret values, or secret locators. Credential migration JSON reports cleanup
counts, completion state, and warning codes; exact keyring entry names remain
available only in the human-facing text result. Bootstrap parse/write failures
use bounded remediation rather than embedding the private path. `config status
--json` reports `catalog_status` as `not_configured`, `available`, or
`unavailable`, includes `restart_required`, and nests the bounded
doctor report when available. Empty `account list --json` results use
`{"accounts": []}` rather than silent output. `account show --json` includes all
mutable non-secret identity, endpoint/TLS, Sent-copy, revision, and binding
fields so automation can review before a revisioned write.

JSON output is a presentation contract and grants no command authority.
`account add --json` and `account set-secret --json` require
`--password-stdin`; secrets enter the low-level API only through user-controlled
stdin. This prevents an interactive prompt from corrupting the single document
and is intended for user-owned automation, not for sending a credential through
an agent. Interactive `config import-legacy --apply`
rejects `--json` because review and same-process confirmation cannot be reduced
to one result document; JSON preview without `--apply` is supported.

Managed account and binding summaries never print secret locators or values.
Account-create commands carry non-secret endpoint summaries separately from
`SecretStr` credential fields; command representation and equality exclude those
fields. `config doctor` also verifies active secret availability without printing
the secret or locator. Disabled accounts are revalidated and excluded before
every provider access, including calls in an already-running stdio session.
Selecting managed mode never deletes preserved legacy TOML rows, and selecting
legacy mode never deletes the managed catalog.

Managed policy updates use the same canonicalization as legacy configuration:
recipient addresses are extracted and lowercased, sender glob patterns are
trimmed and lowercased, and empty or duplicate entries are removed while
preserving first occurrence order. `config update-policy` preserves omitted
fields; pass an empty value to `--allowed-recipients` or `--allowed-senders` to
clear that list. These empty values differ deliberately: empty allowed recipients
disables sending, while empty allowed senders does not restrict reading. The Web
UI represents each recipient or sender pattern as an individual add/edit/remove
item rather than a comma-separated field. Every update requires the revision
shown by `config policy`.

Endpoint ports must be between 1 and 65535, and implicit TLS cannot be combined
with STARTTLS. `account add` checks the selected catalog and these non-secret
endpoint rules before prompting or reading a credential; application services
revalidate them for every caller.

A failed `account set-secret` returns a typed error and does not persist an
intermediate binding or change the current binding authority. `config
index-health` prints bounded rebuildable-projection status and problems. `account
test ACCOUNT [incoming|outgoing]` is the retained low-level, agent-facing
connectivity diagnostic (not a Web UI capability) and exits nonzero for a typed
connection failure and never prints a success sentence in that case. JSON
failures use one of `timeout`, `endpoint_unavailable`, `credential_unavailable`,
`authentication_or_provider_rejected`, or `tls_or_connection_failed`; messages
contain bounded remediation rather than provider exception text. An outgoing test
checks for a configured SMTP endpoint before resolving its credential, authenticates,
submits `MAIL FROM` with the configured account email address, and resets the
transaction without issuing `RCPT TO` or `DATA`. Success therefore proves that
the server accepted the envelope sender, but not that it would accept a recipient
or message body. Typed IMAP/SMTP authentication, sender rejection, and timeout
failures retain the correct category.

### Managed account lifecycle

On Linux and Windows, `account add` and `account set-secret` insert the new value
into the private `managed_secret` table and activate its binding/revision in one
SQLite transaction. On macOS, the system keyring write precedes one
compare-and-swap activation transaction. Any failure before
activation returns an error without persisting an intermediate binding or
changing current binding authority. During rotation, activation first marks the
old active value `CLEANUP_REQUIRED`; confirmed deletion clears that state, while
an external or follow-up deletion failure retains it.

`account show` prints the current account revision. Pass that value with
`--expected-revision` to `account update`, `account disable`, `account enable`,
`account remove-secret`, and `account remove`. A stale value is rejected without
applying the write; inspect the account again and decide whether to retry against
the new state.

`account update` can rename an account, change identity fields and endpoint
settings, add or remove SMTP, and change Sent-copy behavior. A new SMTP endpoint
requires all host, port, username, and TLS fields. An enabled account cannot be
left with an incomplete endpoint/binding pair. Remove its SMTP credential before
using `--remove-outgoing`.

Disable an account before removing one of its credentials. Credential removal
first detaches the active binding in SQLite, then attempts deletion from the
selected managed secret store. If deletion cannot be confirmed, `config doctor`
reports `CLEANUP_REQUIRED`; run the bounded `config cleanup-credentials --limit
100` command after storage access is restored. Re-enabling validates all required
active credentials outside a SQLite transaction and rejects concurrent revision
changes. A failed new save returns an error, leaves no intermediate binding, and
preserves the prior binding authority.

`account remove NAME --expected-revision REV --confirm NAME` is an intentional
soft removal. The exact name confirmation is required. The row no longer appears
as an available account, while its stable operational identity, endpoints, and
binding metadata remain for bounded cleanup; hard purge is not part of this
release. Before committing the tombstone, the service marks every referenced
credential value `CLEANUP_REQUIRED`, then attempts bounded deletion from the
selected store.
Successfully deleted values are finalized as superseded; failures remain visible
to `config doctor` and `config cleanup-credentials`. The normalized-name
tombstone is permanent in this delivery, so the removed account name cannot be
reused.

Legacy and managed authority each allow at most 1,000 configured live accounts,
1,000 recipient allowlist entries, and 1,000 sender allowlist entries. Managed
writes reject larger snapshots before commit. Discovery also applies the shared
8 MiB canonical serialized-result ceiling and fails with `limit_exceeded` rather
than truncating authority data.

### Import stored legacy configuration

Import is explicit and preview-first. Initialize a migration destination, then run:

```bash
mcp-email-server config import-legacy
mcp-email-server config import-legacy --apply
# Review the displayed plan, then type IMPORT at the prompt.
```

The preview reads the effective legacy view without resolving secrets: stored
TOML email accounts, unsupported provider names, exact-name environment account
replacement or environment-account insertion, and environment boolean/allowlist
overrides. Credential source is reported as `plaintext`, `keyring`, or
`environment`; preview uses redacted endpoint models, does not read environment
password values or the keyring, and does not place TOML password material in the
snapshot or plan. Password-variable presence is detected by enumerating names,
not retrieving values. Apply resolves required plaintext, keyring, or current
environment credentials through the normal managed save workflow and leaves the
source TOML, environment, and legacy keyring entries unchanged. Preparation keeps
legacy selected. A complete successful import automatically selects managed mode only when every
source account type is supported and no cleanup attention remains. Final cutover
holds the shared source/selection lock and a managed-catalog writer fence while
rechecking the exact source snapshot, imported private credential values, final
catalog/account revisions, and bootstrap compare-and-swap. Any failure or drift
keeps legacy selected; unsupported provider-style accounts are reported and
prevent automatic cutover until the user explicitly decides whether to select
the supported managed subset.

Each preview has a random one-time token, a SHA-256 fingerprint of the bounded
non-secret source snapshot, creation time, a ten-minute lifetime, and exact
catalog, policy, and per-account target revisions. Account rows show their full
non-secret endpoint/TLS/user/save-to-sent settings and whether each credential
comes from plaintext TOML, the legacy keyring, or environment; no secret value
or reusable locator is shown. CLI `--apply` prints this plan before it prompts
for the exact word `IMPORT`, so confirmation cannot be supplied before review.
A no-change plan needs no prompt, but when all source account types are supported,
**Finish setup**/`--apply` still verifies that each legacy password equals its
active managed counterpart and attempts the guarded final cutover; it never
returns early merely because no rows need writing.
Apply consumes the token, verifies that the selected catalog path and bootstrap revision still
match the private preview, and rejects expiry or source/target drift before the
next credential resolution or write. Selection is checked again immediately
before every account, credential, and policy write; apply advances only revisions
caused by its own completed steps. Preview capability state is expired on access and capped at
32 entries per process. In the UI, preview creation is a CSRF-protected POST,
not a state-changing GET.

A repeated import is deterministic: exact matching accounts and policy are
reported unchanged only after their active managed passwords are privately proven
equal to the current legacy values, genuinely `MISSING` bindings can be filled,
and a changed, renamed, normalized-name-colliding, or previously soft-removed
destination is reported as a conflict before any destination write. A password
mismatch is a credential conflict: neither side is overwritten and legacy remains
selected until the user updates or removes the managed credential and retries.
Planning also rejects normalized collisions within the source and account-limit
overflow. If the effective source account changes between planning and
credential resolution, apply fails and requires a new preview rather than
mixing an old endpoint with a new secret. A committed credential outcome that needs cleanup is reported explicitly rather
than claimed as clean success. A cleanup-required binding becomes an explicit
conflict until cleanup completes. Failed credential saves preserve prior binding
authority. Resolve conflicts or cleanup attention, preview again, and test
imported endpoints. A complete supported import has already selected managed
mode; otherwise make any later selection explicit.

## Operational metadata database

`list_emails_metadata` uses `db_location` for a bounded, rebuildable metadata
projection. Managed mode stores that projection beside the authoritative catalog
in the selected managed database. Legacy and environment-composited accounts use
the configured `db_location`, whose default is `db.sqlite3` next to the active
configuration file. The server creates this operational database only when the
metadata workflow first needs it.

Legacy source mapping stores a stable non-secret fingerprint and never copies an
account password, keyring entry, endpoint row, or secret locator into managed
configuration tables. The projection may contain mailbox names and message
headers needed by the public metadata result. Removing the operational database
only discards rebuildable observations; it does not remove accounts or mail.

Managed storage uses one exact current schema for account authority, the
platform-selected secret binding, and the operational projection. On Linux and
Windows, any copy, snapshot, or backup of this database includes plaintext values
from `managed_secret`; protect every copy with private access equivalent to the
original and never treat the catalog as a non-secret database. There are no
released managed-catalog users, so pre-release development schemas receive no
compatibility or automatic-migration promise. Legacy TOML, environment, and
keyring sources remain supported through explicit import. Unsupported, corrupt,
or insecure managed storage fails closed. In legacy mode, an unavailable or unsafe operational
database produces a bounded warning and the metadata query uses its bounded IMAP
fallback instead.

## Legacy configuration precedence

The following composition applies only in `legacy` mode. The TOML file provides
the base settings, then environment variables are applied as follows:

- Global boolean and allowlist environment variables override the matching TOML
  values.
- `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE` overrides the TOML storage mode.
- A complete environment-provided email account replaces a TOML account with
  the same `account_name`.
- If no TOML account has that name, the environment-provided account is added
  before the TOML accounts.

An environment account is created only when `MCP_EMAIL_SERVER_EMAIL_ADDRESS`,
a non-empty `MCP_EMAIL_SERVER_PASSWORD`, and `MCP_EMAIL_SERVER_IMAP_HOST` are all
present. The generic password remains required even when separate IMAP or SMTP
password variables are provided. An absent or explicitly empty role-specific
password falls back to that generic password in both legacy runtime and managed
import.

`migrate-credentials` is intentionally different: it migrates only the stored
TOML configuration and ignores environment-provided accounts and overrides. Its
stored load, keyring/TOML commit, and checked plaintext cleanup run under the same
legacy write transaction as store/reset, so concurrent operations cannot leave a
sentinel referring to a deleted credential.

Legacy runtime continues to compose this view without persisting it. Managed
import explicitly copies the reviewed effective account/policy view and required
credentials into managed SQLite/SecretStore state; it never rewrites the legacy
TOML or environment. Legacy TOML/environment/keyring behavior remains a
compatibility input and explicit import source; account management is available
only through the managed CLI and authenticated local UI.

`credential_storage` controls only how persistent settings are written. It does
not protect passwords stored in an MCP client configuration, process
environment, CI definition, or container metadata. Prefer the platform's secret
injection mechanism and restrict access to any file containing literal values.

## TOML example

The following example contains all commonly used account fields:

```toml
credential_storage = "auto"
enable_attachment_download = false
allowed_recipients = []
allowed_senders = []
report_blocked_mutations = false

[[emails]]
account_name = "work"
description = "Work mailbox"
full_name = "John Doe"
email_address = "john@example.com"
save_to_sent = true
sent_folder_name = "Sent"

[emails.incoming]
user_name = "john@example.com"
password = "your-password"
host = "imap.example.com"
port = 993
use_ssl = true
start_ssl = false
verify_ssl = true

[emails.outgoing]
user_name = "john@example.com"
password = "your-password"
host = "smtp.example.com"
port = 465
use_ssl = true
start_ssl = false
verify_ssl = true
```

`description`, `save_to_sent`, and `sent_folder_name` are optional. Remove the
entire `[emails.outgoing]` table for an IMAP-only account.

When credentials are stored in the operating system keyring, password values in
this file are replaced by the reserved `__KEYRING__` marker. Do not enter that
value as a real password. See [Credential storage](security.md#credential-storage).

## Multiple accounts

Add one `[[emails]]` entry per account:

```toml
[[emails]]
account_name = "personal"
full_name = "John Doe"
email_address = "john@example.com"

[emails.incoming]
user_name = "john@example.com"
password = "personal-password"
host = "imap.example.com"
port = 993
use_ssl = true
start_ssl = false
verify_ssl = true

[[emails]]
account_name = "work"
full_name = "John Doe"
email_address = "john@company.example"

[emails.incoming]
user_name = "john@company.example"
password = "work-password"
host = "imap.company.example"
port = 993
use_ssl = true
start_ssl = false
verify_ssl = true
```

Every `account_name` must be unique across the configuration. MCP tools use this
name to select an account.

The environment variable interface describes one account. Use TOML or the UI
when persistent configuration requires multiple accounts.

## TLS modes

Each IMAP or SMTP server has three related fields:

| Mode                       | `use_ssl` | `start_ssl` | Typical port                            |
| -------------------------- | --------- | ----------- | --------------------------------------- |
| Implicit TLS               | `true`    | `false`     | IMAP 993, SMTP 465                      |
| STARTTLS                   | `false`   | `true`      | IMAP 143, SMTP 587                      |
| Plain connection, insecure | `false`   | `false`     | Trusted local or isolated networks only |

Do not enable both implicit TLS and STARTTLS for the same server. Keep
`verify_ssl = true` unless connecting to a trusted local service with a
self-signed certificate.

Without implicit TLS or STARTTLS, credentials and message content can travel in
plaintext, and `verify_ssl` has no effect. Do not use a plain connection to a
remote mail service. Limit it to a trusted local bridge, an encrypted tunnel,
or an otherwise isolated network.

## IMAP-only accounts

SMTP configuration is optional. The MCP tool catalog is static, so `send_email`
is still advertised when every account omits SMTP; a call for an IMAP-only
account fails its capability check before SMTP access.

IMAP-only does not mean read-only. These tools can still change mailbox state:

- `save_to_mailbox`
- `set_email_flags`
- `mark_emails_as_read`
- `move_emails`
- `archive_emails`
- `delete_emails`

See [IMAP-only accounts](guides.md#imap-only-accounts) for examples.

## Saving sent email

After SMTP delivery, `save_to_sent = true` asks the server to append the sent
message to an IMAP Sent folder. This is enabled by default.

The server attempts to detect common folders, including:

- `Sent`
- `INBOX.Sent`
- `Sent Items`
- `Sent Mail`
- `[Gmail]/Sent Mail`

Set a custom folder when auto-detection is not suitable:

```toml
[[emails]]
account_name = "work"
save_to_sent = true
sent_folder_name = "INBOX.Sent"
```

Set `save_to_sent = false` to disable the IMAP append after sending. The
environment equivalents are `MCP_EMAIL_SERVER_SAVE_TO_SENT` and
`MCP_EMAIL_SERVER_SENT_FOLDER_NAME`.

## Outgoing message headers

Every outgoing MIME message carries a single top-level `MIME-Version: 1.0`
header. The configured full name is an RFC 5322 display name: punctuation such as
`@`, commas, and quotes is safely quoted, while non-ASCII display text paired with
an ASCII addr-spec is encoded without forcing SMTPUTF8. SMTP `MAIL FROM` always
uses the separate configured account email address as its RFC 5321 reverse-path.
An internationalized addr-spec or thread-header identifier requires SMTPUTF8 for
delivery and RFC 6855 UTF8 support for Draft or Sent-copy APPEND. Drafts and Sent
copies use the same correctly formatted `From` header and fail before APPEND when
the provider cannot negotiate the required UTF8 mode.

The server also adds `User-Agent: mcp-email-server` and
`X-Mailer: mcp-email-server` as de-facto application identifiers for
compatibility with providers that inspect sender-software identification. These
identifiers are fixed and contain no account-specific information.

## Global settings

| Setting                      | Default  | Description                                                                |
| ---------------------------- | -------- | -------------------------------------------------------------------------- |
| `credential_storage`         | `"auto"` | Select `auto`, `keyring`, or `plaintext` credential storage.               |
| `enable_attachment_download` | `false`  | Allow `download_attachment` to write files.                                |
| `allowed_recipients`         | `[]`     | Exact recipients; empty disables sending and recipient-bound saves.        |
| `allowed_senders`            | `[]`     | Incoming `From` patterns; empty does not restrict reading.                 |
| `report_blocked_mutations`   | `false`  | Report blocked message IDs instead of returning privacy-preserving no-ops. |

See [Security](security.md) before enabling attachment downloads or applying
allowlists.

## Environment variable reference

### Account variables

| Variable                            | Default          | Required | Description                                               |
| ----------------------------------- | ---------------- | -------- | --------------------------------------------------------- |
| `MCP_EMAIL_SERVER_ACCOUNT_NAME`     | `default`        | No       | Account identifier used by MCP tools.                     |
| `MCP_EMAIL_SERVER_FULL_NAME`        | Email local part | No       | Display name used in outgoing messages.                   |
| `MCP_EMAIL_SERVER_EMAIL_ADDRESS`    | None             | Yes      | Account email address.                                    |
| `MCP_EMAIL_SERVER_USER_NAME`        | Email address    | No       | Shared IMAP and SMTP username.                            |
| `MCP_EMAIL_SERVER_PASSWORD`         | None             | Yes      | Shared password and required environment-account trigger. |
| `MCP_EMAIL_SERVER_IMAP_HOST`        | None             | Yes      | IMAP server host.                                         |
| `MCP_EMAIL_SERVER_IMAP_PORT`        | `993`            | No       | IMAP server port.                                         |
| `MCP_EMAIL_SERVER_IMAP_SSL`         | `true`           | No       | Use implicit TLS for IMAP.                                |
| `MCP_EMAIL_SERVER_IMAP_START_SSL`   | `false`          | No       | Upgrade the IMAP connection with STARTTLS.                |
| `MCP_EMAIL_SERVER_IMAP_VERIFY_SSL`  | `true`           | No       | Verify the IMAP TLS certificate.                          |
| `MCP_EMAIL_SERVER_IMAP_USER_NAME`   | Shared username  | No       | IMAP-specific username.                                   |
| `MCP_EMAIL_SERVER_IMAP_PASSWORD`    | Shared password  | No       | Non-empty IMAP-specific password; empty uses shared.      |
| `MCP_EMAIL_SERVER_SMTP_HOST`        | None             | No       | SMTP server host; enables sending when present.           |
| `MCP_EMAIL_SERVER_SMTP_PORT`        | `465`            | No       | SMTP server port.                                         |
| `MCP_EMAIL_SERVER_SMTP_SSL`         | `true`           | No       | Use implicit TLS for SMTP.                                |
| `MCP_EMAIL_SERVER_SMTP_START_SSL`   | `false`          | No       | Upgrade the SMTP connection with STARTTLS.                |
| `MCP_EMAIL_SERVER_SMTP_VERIFY_SSL`  | `true`           | No       | Verify the SMTP TLS certificate.                          |
| `MCP_EMAIL_SERVER_SMTP_USER_NAME`   | Shared username  | No       | SMTP-specific username.                                   |
| `MCP_EMAIL_SERVER_SMTP_PASSWORD`    | Shared password  | No       | Non-empty SMTP-specific password; empty uses shared.      |
| `MCP_EMAIL_SERVER_SAVE_TO_SENT`     | `true`           | No       | Append sent messages to an IMAP Sent folder.              |
| `MCP_EMAIL_SERVER_SENT_FOLDER_NAME` | Auto-detected    | No       | Override the Sent folder name.                            |

Boolean values accept `true`, `1`, `yes`, or `on` as true, ignoring case. Other
values are treated as false. Do not add surrounding whitespace to these values.

### Global variables

| Variable                                      | Default                                  | Description                                                         |
| --------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------- |
| `MCP_EMAIL_SERVER_CONFIG_PATH`                | `~/.config/mcp-email-server/config.toml` | Use a custom TOML path.                                             |
| `MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD` | `false`                                  | Override attachment download access.                                |
| `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`         | Empty                                    | Comma-separated recipients; empty disables sending.                 |
| `MCP_EMAIL_SERVER_ALLOWED_SENDERS`            | Empty                                    | Comma-separated sender globs; empty does not restrict reading.      |
| `MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS`   | `false`                                  | Override blocked mutation reporting.                                |
| `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE`         | TOML value or `auto`                     | Override credential storage with `auto`, `keyring`, or `plaintext`. |
| `MCP_EMAIL_SERVER_LOG_LEVEL`                  | `INFO`                                   | Set the Loguru logging level, such as `DEBUG` or `WARNING`.         |

HTTP transport variables are documented separately in
[Transports](transports.md#streamable-http).

In managed mode, `MCP_EMAIL_SERVER_CONFIG_PATH` still selects the legacy source
path from which the bootstrap sidecar path is derived, but legacy account,
allowlist, attachment, mutation-reporting, and credential-storage overlays do
not replace managed catalog values.

## Reset configuration

Delete the configuration file and perform best-effort cleanup of referenced
keyring entries with:

```bash
mcp-email-server reset --confirm RESET
```

In legacy mode this operation removes all persistently configured accounts from
the legacy source. The independent bootstrap sidecar is retained, including any
managed catalog selected for a future restart; reset never needs to rewrite the
source to preserve that authority. Environment-based configuration remains
effective as long as its variables are present. In managed mode reset is rejected
before any TOML, database, or keyring mutation; select legacy and restart before
intentionally resetting the legacy source. An unparseable bootstrap sidecar also
fails closed because its selected mode cannot be established safely. Resetting a
truly absent source with no sidecar creates no directory or lock artifact. A
newly created legacy configuration parent uses `0700` on POSIX, while an existing
legacy parent is not silently changed.
