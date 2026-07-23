# Configuration

> **Version scope:** Managed mode and the embedded React UI on this page are
> Local Email App V2 behavior. See [Version availability](getting-started.md#version-availability)
> before using these commands with a PyPI installation.

mcp-email-server supports two explicitly selectable configuration modes:

- `legacy` keeps account configuration in TOML and composes documented
  environment overlays;
- `managed` keeps non-secret account configuration in SQLite and credentials in
  the operating system keyring.

A missing configuration file or a pre-managed TOML file without `mode` remains
`legacy` for backward compatibility. Every new bootstrap write records
`bootstrap_version = 1`, a monotonic `bootstrap_revision`, and an explicit mode.
Selection uses that revision as a compare-and-swap token under a bounded private
lock; it is separate from the managed catalog revision.

## Configuration file

The default path is:

```text
~/.config/mcp-email-server/config.toml
```

Set `MCP_EMAIL_SERVER_CONFIG_PATH` to use a different path. Relative paths are
resolved against the server process's working directory.

On first use, if the current file does not exist and a legacy file exists at
`~/.config/zerolib/mcp_email_server/config.toml`, the legacy file is copied to
the current location automatically. An explicitly managed file at the old path
is not copied as an import-time side effect.

## Local management UI

`mcp-email-server ui [--no-open] [--port PORT]` provides the complete managed
management plane through an embedded React application. It always binds to
`127.0.0.1`; the default port `0` asks the operating system for an ephemeral
port. There is no host, share, debug, reload, daemon, or remote-UI option. The
default opens the one-time URL directly in the browser; `--no-open` requires an
attached terminal and prints the URL only there. A noninteractive invocation
fails before serving rather than exposing its bootstrap token in logs.

The UI can initialize, inspect, activate, and select a managed catalog; manage
accounts, credentials, connectivity, and policy; preview/apply legacy import;
and run doctor, cleanup, repair, and index-health workflows. Mutable operations
use optimistic catalog or account revisions. A conflict displays a current
bounded non-secret summary and requires explicit review rather than automatic
replay. Legacy mode remains inspectable and importable, but the UI is not a
legacy TOML editor.

The browser process is only an adapter over the same application services as
the CLI. It has no mail, generic RPC, filesystem browser, OpenAPI, or MCP App
surface. Managed catalog and attachment effects, plus oversized-result spill,
require the documented POSIX filesystem primitives and fail closed when those
primitives are unavailable. See [Security](security.md#local-management-ui-security)
for its one-time bootstrap, platform, and session boundaries.

## Managed CLI setup

Managed setup deliberately separates catalog activation from mode selection:

```bash
mcp-email-server config init --database ~/.config/mcp-email-server/catalog.sqlite3

mcp-email-server account add work \
  --email john@example.com \
  --full-name "John Doe" \
  --imap-host imap.example.com \
  --imap-user john@example.com

mcp-email-server account test work incoming
mcp-email-server config activate
mcp-email-server config select managed
```

`account add` prompts for the IMAP password with masked input. Add SMTP with
`--smtp-host`, `--smtp-port`, and `--smtp-user`; the command then prompts for a
separate SMTP password. For non-interactive setup, pass `--password-stdin` and
provide one line for IMAP, followed by one SMTP line when SMTP is configured.
Passwords are never accepted as ordinary command-line values.

`config init` creates a `STAGING` SQLite catalog and records its path while
leaving legacy mode selected. Retrying the same path safely adopts an existing
owner-only, valid `STAGING` catalog without resetting its data or revisions; an
active, corrupt, foreign, or insecure target is rejected. This makes a retry
possible if catalog creation committed but the bootstrap compare-and-swap did
not. Initialization is rejected while managed mode is selected so it cannot
replace the active authority with a staging database. Account creation is
likewise limited to `STAGING`; use `account set-secret` for credential
rotation after activation. `config activate` requires at least one
enabled account with an active IMAP credential and a complete optional SMTP
pair. `config select managed` accepts only an `ACTIVE` catalog. Restart every
MCP server process after `config select`.

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
mcp-email-server account repair-secret work incoming resume --expected-revision 5
mcp-email-server account test work incoming
mcp-email-server account disable work --expected-revision 3
mcp-email-server account enable work --expected-revision 4
mcp-email-server config cleanup-credentials
mcp-email-server config select legacy
```

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
clear that list. Every update requires the revision shown by `config policy`.

`account repair-secret` never accepts a secret. It explicitly resumes or rolls
back the single already-staged ambiguous candidate at the supplied account
revision. `config index-health` prints bounded rebuildable-projection status and
problems. `account test ACCOUNT [incoming|outgoing]` exits nonzero for a typed
connection failure and never prints a success sentence in that case.

### Managed account lifecycle

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
first detaches the active binding in SQLite, then attempts keyring deletion. If
the external deletion cannot be confirmed, `config doctor` reports
`CLEANUP_REQUIRED`; run the bounded `config cleanup-credentials --limit 100`
command after keyring access is restored. Re-enabling validates all required
active credentials outside a SQLite transaction and rejects concurrent revision
changes. If a candidate write outcome is ambiguous, doctor and the UI report
`PENDING_REPAIR_REQUIRED`. The UI offers explicit resume or rollback against the
current account revision; cleanup never guesses which candidate should become
active.

`account remove NAME --expected-revision REV --confirm NAME` is an intentional
soft removal. The exact name confirmation is required. The row no longer appears
as an available account, while its stable operational identity, endpoints, and
binding metadata remain for bounded repair; hard purge is not part of this
release. Before committing the tombstone, the service marks every referenced
credential candidate `CLEANUP_REQUIRED`, then attempts bounded keyring deletion.
Successfully deleted values are finalized as superseded; failures remain visible
to `config doctor` and `config cleanup-credentials`.

Legacy and managed authority each allow at most 1,000 configured live accounts,
1,000 recipient allowlist entries, and 1,000 sender allowlist entries. Managed
writes reject larger snapshots before commit. Discovery also applies the shared
8 MiB canonical serialized-result ceiling and fails with `limit_exceeded` rather
than truncating authority data.

### Import stored legacy configuration

Import is explicit and preview-first. Initialize a staging catalog, then run:

```bash
mcp-email-server config import-legacy
mcp-email-server config import-legacy --apply
# Review the displayed plan, then type IMPORT at the prompt.
```

The preview reads only stored TOML accounts and policy. It ignores environment
accounts and overlays, does not resolve keyring values, does not write the
catalog, and reports provider-style accounts as unsupported. Apply is accepted
only for a `STAGING` catalog. It resolves the required stored plaintext or
keyring credentials through the normal managed candidate workflow, leaves the
source TOML and legacy keyring entries unchanged, and never activates or selects
the destination automatically.

Each preview has a random one-time token, a SHA-256 fingerprint of the bounded
non-secret source snapshot, creation time, a ten-minute lifetime, and exact
catalog, policy, and per-account target revisions. Account rows show their full
non-secret endpoint/TLS/user/save-to-sent settings and whether each credential
comes from plaintext TOML or the legacy keyring; no secret value or reusable
locator is shown. CLI `--apply` prints this plan before it prompts for the exact
word `IMPORT`, so confirmation cannot be supplied before review. Apply consumes
the token and rejects expiry or source/target drift before the next credential
resolution or write; it advances only revisions caused by its own completed
steps.

A repeated import is deterministic: exact matching accounts and policy are
reported unchanged, missing candidate bindings can be resumed, and a changed,
renamed, or previously soft-removed destination is reported as a conflict before
any secret resolution or destination write. If the stored source account changes
between planning and credential resolution, apply fails and requires a new
preview rather than mixing an old endpoint with a new secret. Resolve conflicts
manually, preview again, test imported endpoints, then activate and select the
catalog.

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

Managed storage uses exact schema version 2 for account authority and the
operational projection. A verified version 1 catalog is migrated transactionally;
coincidental version markers or partially compatible schemas are never claimed. Unsupported, corrupt, or insecure
managed storage fails closed. In legacy mode, an unavailable or unsafe operational
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
`MCP_EMAIL_SERVER_PASSWORD`, and `MCP_EMAIL_SERVER_IMAP_HOST` are all present.
The generic password remains required even when separate IMAP or SMTP password
variables are provided.

`migrate-credentials` is intentionally different: it migrates only the stored
TOML configuration and ignores environment-provided accounts and overrides.

MCP and the local UI never persist an environment-composited legacy runtime
view. Legacy TOML/environment/keyring behavior remains a compatibility input and
an explicit import source; account management is available only through the
managed CLI and authenticated local UI.

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

## Global settings

| Setting                      | Default  | Description                                                                |
| ---------------------------- | -------- | -------------------------------------------------------------------------- |
| `credential_storage`         | `"auto"` | Select `auto`, `keyring`, or `plaintext` credential storage.               |
| `enable_attachment_download` | `false`  | Allow `download_attachment` to write files.                                |
| `allowed_recipients`         | `[]`     | Restrict recipients used by `send_email` and `save_to_mailbox`.            |
| `allowed_senders`            | `[]`     | Restrict incoming messages by `From` address pattern.                      |
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
| `MCP_EMAIL_SERVER_IMAP_PASSWORD`    | Shared password  | No       | IMAP-specific password.                                   |
| `MCP_EMAIL_SERVER_SMTP_HOST`        | None             | No       | SMTP server host; enables sending when present.           |
| `MCP_EMAIL_SERVER_SMTP_PORT`        | `465`            | No       | SMTP server port.                                         |
| `MCP_EMAIL_SERVER_SMTP_SSL`         | `true`           | No       | Use implicit TLS for SMTP.                                |
| `MCP_EMAIL_SERVER_SMTP_START_SSL`   | `false`          | No       | Upgrade the SMTP connection with STARTTLS.                |
| `MCP_EMAIL_SERVER_SMTP_VERIFY_SSL`  | `true`           | No       | Verify the SMTP TLS certificate.                          |
| `MCP_EMAIL_SERVER_SMTP_USER_NAME`   | Shared username  | No       | SMTP-specific username.                                   |
| `MCP_EMAIL_SERVER_SMTP_PASSWORD`    | Shared password  | No       | SMTP-specific password.                                   |
| `MCP_EMAIL_SERVER_SAVE_TO_SENT`     | `true`           | No       | Append sent messages to an IMAP Sent folder.              |
| `MCP_EMAIL_SERVER_SENT_FOLDER_NAME` | Auto-detected    | No       | Override the Sent folder name.                            |

Boolean values accept `true`, `1`, `yes`, or `on` as true, ignoring case. Other
values are treated as false. Do not add surrounding whitespace to these values.

### Global variables

| Variable                                      | Default                                  | Description                                                               |
| --------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| `MCP_EMAIL_SERVER_CONFIG_PATH`                | `~/.config/mcp-email-server/config.toml` | Use a custom TOML path.                                                   |
| `MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD` | `false`                                  | Override attachment download access.                                      |
| `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`         | Empty                                    | Comma-separated recipient addresses; an empty value clears the TOML list. |
| `MCP_EMAIL_SERVER_ALLOWED_SENDERS`            | Empty                                    | Comma-separated sender globs; an empty value clears the TOML list.        |
| `MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS`   | `false`                                  | Override blocked mutation reporting.                                      |
| `MCP_EMAIL_SERVER_CREDENTIAL_STORAGE`         | TOML value or `auto`                     | Override credential storage with `auto`, `keyring`, or `plaintext`.       |
| `MCP_EMAIL_SERVER_LOG_LEVEL`                  | `INFO`                                   | Set the Loguru logging level, such as `DEBUG` or `WARNING`.               |

HTTP transport variables are documented separately in
[Transports](transports.md#streamable-http).

In managed mode, `MCP_EMAIL_SERVER_CONFIG_PATH` still selects the bootstrap
file, but legacy account, allowlist, attachment, mutation-reporting, and
credential-storage overlays do not replace managed catalog values.

## Reset configuration

Delete the configuration file and perform best-effort cleanup of referenced
keyring entries with:

```bash
mcp-email-server reset
```

In legacy mode this operation removes all persistently configured accounts.
Environment-based configuration remains effective as long as its variables are
present. In managed mode it is rejected before any TOML, database, or keyring
mutation; select legacy and restart before intentionally resetting the legacy
source. An unparseable bootstrap also fails closed because its selected mode
cannot be established safely.
