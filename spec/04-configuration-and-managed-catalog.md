# 04. Configuration and Managed Catalog

## Authority Selection

Bootstrap authority contains the explicit mode, selected catalog path, and an
independent monotonic bootstrap revision. It is stored in the private sibling
sidecar derived from the legacy source path: `config.toml` uses
`config.bootstrap.toml`. The selected catalog uses `managed_db_location`;
historical `db_location` remains legacy operational metadata and does not imply
a managed selection. A selected managed catalog is management authority even
while legacy mail mode remains active, so the sidecar and parent meet the strict
security contract in either mode. The source TOML remains an independent,
byte-preserved import and rollback source; selection, initialization, and import
cutover never rewrite it. Read-only compatibility may consume a pre-release
combined source/bootstrap file; a later selection write, or the authority step
before a legacy source write, materializes the selection into the sidecar without
that materialization itself changing source bytes. Legacy store, reset, and
credential migration preserve sidecar authority under
one transaction lock covering source load, keyring effects, source commit, and
checked cleanup. The sidecar contains no managed account configuration or secret.
The process reads it once, validates it, and freezes the result. Management
writes compare the expected bootstrap revision while holding the bounded
owner-only no-follow sidecar lock before atomic replacement; the catalog revision
remains a separate concurrency domain.

Selection rules:

1. A machine with no bootstrap selection follows the historical legacy behavior
   for backward compatibility.
2. An explicit `legacy` selection uses legacy TOML plus current environment
   composition.
3. An explicit `managed` selection requires the named catalog to exist and pass
   filesystem/schema checks. Account readiness is evaluated per account rather
   than as a catalog-wide activation gate.
4. A persisted managed selection never falls back to legacy when validation,
   credential, provider, or runtime work fails.
5. Changing selection takes effect only after process restart.

Environment variables may compose the legacy view and may locate bootstrap
configuration, but MUST NOT override managed account, policy, revision, or
secret-binding authority.

## Catalog Authority

A managed catalog has a stable identifier, exact schema version, and monotonic
catalog revision. It has no `STAGING`/`ACTIVE` lifecycle and no catalog activation
operation. On Linux it also contains the owner-only `managed_secret` store; on
macOS and other supported non-Linux platforms bindings resolve through the system
keyring, as specified in spec 05.

- **initialize** creates a secure catalog idempotently or reports a typed conflict
  with an existing incompatible target. Idempotent adoption is limited to the
  exact requested path when it remains owner-only and structurally valid; it
  preserves all catalog data and revisions. The local UI uses a backend-selected
  `managed.sqlite3` in the shared parent of the active legacy source and bootstrap
  sidecar, plus a bootstrap compare-and-swap rather than accepting a
  browser-chosen path. A bootstrap
  persistence failure after catalog commit leaves that catalog available for an
  explicit retry rather than deleting it.
- A fresh installation initializes and selects managed mode in one operation.
- If effective v1 legacy accounts, providers, environment configuration, or
  policy exist, initialization records the managed migration destination but
  keeps legacy runtime selected. A reviewed, fully successful import with no
  unsupported provider types compare-and-swaps selection to managed
  automatically. Import failure or unsupported provider types leave legacy
  selected; the source is never deleted or rewritten.
- **select managed** revalidates the exact catalog revision and structural
  authority, then compare-and-swaps bootstrap authority. Incomplete accounts do
  not block selection; they remain doctor problems and are omitted individually
  from runtime discovery until complete.
- **select legacy** compare-and-swaps bootstrap authority without opening the
  selected managed catalog, so an unavailable catalog does not block explicit
  recovery.
- The running process continues using its frozen prior selection and tells the
  operator that restart is required only when effective runtime authority changes.

## Managed Account Lifecycle

Managed account operations are available through the shared application service:

- list/show bounded non-secret summaries;
- create with unique normalized name and validated non-secret endpoints;
- update display/name, endpoints, or account policy;
- disable and re-enable;
- soft-remove;
- set, rotate, remove, and clean up credentials through spec 05;
- test IMAP and SMTP connectivity with explicit role selection.

Every selected-catalog mutation supplies the reviewed catalog identity and
bootstrap revision in addition to the expected account or catalog revision. The
service binds that exact catalog instance and rechecks selection before each
catalog access, so another catalog with coincidentally equal numeric revisions
cannot receive the write. On mismatch, it returns a typed conflict containing
only a bounded current non-secret summary
and requires the caller to review/retry. The UI MUST NOT auto-replay a stale
mutation. Live legacy and managed authority is capped at 1,000 configured
accounts; managed creation checks that aggregate inside the catalog transaction.
Recipient and sender policy collections are each capped at 1,000 entries.

Soft removal preserves the stable identity, audit timestamps, binding cleanup
state, and normalized-name tombstone. It disables provider work immediately.
Hard purge is deferred, and the normalized name remains reserved: a removed
account name cannot be reused in this delivery.

Connectivity tests use the same current authority, late secret resolution,
provider TLS policy, limits, and redaction as mail workflows. They do not save a
credential or enable an account as a side effect. Failures
retain one bounded stable category (`timeout`, `endpoint_unavailable`,
`credential_unavailable`, `authentication_or_provider_rejected`, or
`tls_or_connection_failed`) plus safe remediation, never a raw provider message.
Endpoint presence is checked before role-specific secret resolution, and typed
provider authentication, timeout, and transport failures retain their category.
Endpoint port range and mutually exclusive implicit-TLS/STARTTLS modes are
validated before CLI secret collection and again at the application boundary.

## Low-level Agent Management CLI

The `config` and `account` command groups are the low-level agent management API.
They intentionally retain exact operational vocabulary such as catalog,
revision, binding state, and restart state rather than translating it into Web UI
task language. Connectivity service access remains here through
`account test`; it is a diagnostic and does not grant an agent permission to
perform another management command.

Every finite `config` and `account` command, plus legacy `reset` and credential
migration, supports a leaf `--json` result mode. Success emits exactly one UTF-8
JSON document with `schema_version: 1`, `ok: true`, a stable command identifier,
explicit data, and an always-present warning array. Parsed application failures
emit one document with `schema_version: 1`, `ok: false`, the command identifier,
a typed stable `error.code`, and that code's fixed safe message while preserving
the nonzero exit status. Framework usage errors that occur before command
dispatch remain Click errors in this version. Successful mutations report the
relevant resulting account, binding, catalog, bootstrap-revision, or restart-state
fields needed for the next low-level call. Catalog services return these
committed outcomes directly; a CLI adapter MUST NOT perform a fallible second
status read that can turn a committed mutation into an `ok: false` result.

Presentation DTOs explicitly select safe fields; they never recursively expose
application dataclasses, local configuration/database paths, preview tokens,
secret values, or secret locators. Credential-migration JSON exposes bounded
cleanup counts, completion state, and warning codes rather than reusable keyring
entry names; exact entries remain a human-facing text diagnostic. Bootstrap
failures are mapped to bounded path-free remediation before entering
agent-readable JSON. Interactive legacy import apply rejects JSON mode because its reviewed
preview and same-process confirmation cannot be represented as one result
document. JSON is only a stable presentation envelope and never grants command
authority. Secret-writing commands accept secrets only from user-controlled
masked input or stdin and require `--password-stdin` in JSON mode so a prompt
cannot corrupt stdout; this supports user-owned automation and does not authorize
an agent to receive credentials.

Default text output remains the human interface. Account list JSON includes an
explicit empty array, account show includes all mutable non-secret fields, and
status includes catalog presence, restart requirement, schema/revision, account
counts, and credential-state counts. Destructive legacy reset requires
exact `RESET` confirmation in both output modes.

## Managed Policy

Catalog defaults and account overrides form effective policy. Policy includes at
least:

- allowed mail mutation classes;
- attachment materialization enablement and size ceilings;
- provider TLS requirements;
- relevant request/result limits where configurable;
- sent-copy behavior and safe fallback choices.

Policy updates are revisioned. Recipient addresses are extracted, trimmed,
lowercased, empty-filtered, and stably deduplicated; sender glob patterns are
trimmed, lowercased, empty-filtered, and stably deduplicated. Managed updates and
legacy composition use the same canonicalizers. The UI presents each allowed
recipient and sender as an individual add/edit/remove item rather than a
comma-separated field. Empty collections have deliberately different semantics:
empty allowed recipients disables sending, while empty allowed senders does not
restrict reading. Permissive changes do not bypass capability or input
validation. Restrictive changes take effect on the next independent effect
because authority is revalidated at operation boundaries.

## Legacy Mode

Legacy mode preserves established TOML and environment composition. New managed
catalog semantics MUST NOT reinterpret legacy precedence. An absent or empty
role-specific environment password falls back to the required non-empty shared
password. Managed bootstrap authority remains unavailable without the required
POSIX owner/no-follow/lock primitives, but legacy-only store, reset, and
credential migration retain their historical platform writer under process-local
serialization. A fresh reset has no filesystem effect; a newly created POSIX
legacy configuration parent is `0700`, and an existing parent is not silently
changed.

MCP exposes no legacy or managed account writer. The historical
`add_email_account` tool is removed because its credential-bearing arguments
cannot be made into a portable secret channel by host approval or elicitation.
Release notes direct existing users to interactive CLI or authenticated local
Web UI setup.

CLI and Web UI do not become general-purpose legacy TOML editors. They may offer
an explicit, securely prompted legacy compatibility command where required,
show bounded legacy status, and guide an explicit managed migration. Agent
integrations hand the user to those interfaces under spec 11; they do not write
configuration or collect credentials themselves.

## Explicit Legacy Import

Import is preview-first and never implicit.

### Preview

Preview reads a bounded, frozen legacy source snapshot, canonicalizes accounts,
and returns a deterministic non-secret plan. It uses redacted models, detects
environment password presence by enumerating variable names without retrieving
values, does not read keyring entries, and applies count, file,
and controlled-string limits before fingerprinting or caching:

- source fingerprint and expiry/creation time;
- planned creates, safe matches, conflicts, skips, and warnings;
- endpoint and policy summaries without secret values or reusable locators;
- each required credential role and its source class (`plaintext`, `keyring`, or
  `environment`), without probing source availability;
- target catalog identifier and exact expected catalog, policy, and account
  revisions.

The source is the effective legacy runtime view: stored TOML accounts and
provider warnings, exact-name environment account replacement/addition, and
boolean/allowlist environment precedence. This composition is shared with
legacy runtime semantics. The fingerprint covers all non-secret source fields
that affect apply, including effective endpoint configuration and secret source
identity without revealing its value. The separate `migrate-credentials`
compatibility command remains stored-TOML-only. Preview capability state has a
ten-minute lifetime, is expired on access, and is capped at 32 entries per
process.

### Apply

Apply requires explicit confirmation plus the exact preview token/fingerprint and
expected target revisions. A plan with no account, credential, or policy changes
requires no confirmation, but its apply/finalize operation still executes the
credential proofs and guarded cutover; adapters MUST NOT return early. CLI apply
prints the complete non-secret plan before reading confirmation in the same
process; UI apply binds confirmation to its one-time preview token. The private
preview is also bound to the selected catalog
path and bootstrap revision, so another catalog cannot consume it even when
numeric catalog revisions coincide. Apply re-reads both source and target before
each required credential resolution and rechecks selection immediately before
every account, credential, and policy write. It advances only revisions caused
by its own successful steps; unrelated fresh reads never replace reviewed
expected revisions. Any material drift returns `preview_stale` before the next
secret or catalog write.

Automatic cutover is one guarded backend operation. It holds the shared
source/selection lock, rechecks the exact sidecar mode, revision, and catalog path,
then rechecks the complete non-secret source snapshot and privately re-resolves
all imported secret values. While that lock remains held, it acquires a SQLite
`BEGIN IMMEDIATE` writer fence and verifies the exact final catalog revision plus
every expected account revision and enabled state. The sidecar compare-and-swap
occurs before either fence is released. Source drift, private credential drift,
catalog or account drift, unsupported providers, or any `attention_required`
outcome prevents cutover and leaves legacy selected.

Before secret access, planning rejects source names that collide after managed
NFKC/casefold normalization, marks normalized destination collisions as
conflicts, and checks aggregate account capacity. For each bounded item apply
uses the spec 05 save protocol and minimal revisioned catalog transactions. On
Linux, secret insertion and active binding/revision commit together in managed
SQLite; on a keyring-backed platform, failure before binding activation leaves
binding authority unchanged and no provisional binding is persisted. When apply
is otherwise eligible for automatic cutover, an `unchanged` account with active
bindings resolves each current legacy role and the corresponding managed active secret privately;
every role must compare equal. The legacy values then join the final source-lock
recheck while managed account revisions join the catalog writer fence. A mismatch
is an explicit credential conflict and cannot cut over or overwrite either side.
A cleanup-required result is surfaced as explicit attention and is never reported
as clean completion. Only a truly `MISSING` binding may be filled by import; a
cleanup-required binding is a conflict until bounded cleanup completes. Safe
continuation requires the same source snapshot and current durable import state.
Import never deletes or rewrites legacy TOML, environment, or keyring sources.
Initialization keeps legacy selected while such a source awaits review. After a
complete confirmed apply, import automatically compare-and-swaps managed
selection only when every source account type is supported, no attention remains,
and the guarded final source, private-secret, and target checks all pass. Any
failure, drift, unsupported provider, or cleanup attention leaves legacy selected
for uninterrupted recovery.

## Writer Fences

All legacy CLI/UI writers fail before mutation in managed mode. No legacy writer
is registered through MCP. All managed writers require a selected or explicitly
targeted secure managed catalog and never mutate legacy state. Fences live in
application services, not only CLI/UI command checks.

## Management Status and Doctor

Bounded status distinguishes durable selected mode/catalog from frozen running
mode/catalog, reports bootstrap existence and revision, summarizes effective
legacy source presence without resolving secrets, and includes catalog schema/revision, account counts, incomplete credential
states, and restart requirement. Agent-readable CLI output omits the
local catalog path; richer user-operated interfaces may show a safely displayable
path. A missing, corrupt, incompatible, or insecure selected catalog produces a bounded unavailable
category while preserving the bootstrap state needed to select legacy; status
does not silently fall back. Doctor performs opt-in bounded checks for
bootstrap, file security, schema, binding consistency, secret resolution, and
provider connectivity. Results expose categories and remediation, never values,
SQL, raw provider responses, or reusable locators.

## Acceptance Criteria

1. Missing bootstrap retains only the historical implicit-legacy rule; explicit
   managed selection fails closed with no fallback.
2. Fresh initialization, v1-preserving migration preparation, successful-import
   cutover, bootstrap-CAS selection, unavailable-catalog recovery, and restart
   semantics are covered through CLI and UI; no catalog activation exists.
3. Every account, policy, catalog, import, binding, and bootstrap mutation
   rejects stale revisions with a bounded current summary; account and policy
   cardinality limits are enforced on both read and write boundaries, including
   the full 1,000-entry recipient and sender policy limit.
4. Soft removal disables provider work and permanently reserves the normalized
   name in this delivery.
5. Every finite management command has a tested single-document JSON success
   contract; dispatched failures have stable codes, no secret/locator fields, and
   preserve exit semantics.
6. CLI endpoint/state preflight occurs before secret input, and connectivity
   failures expose only the approved stable categories and remediation.
7. The MCP catalog contains no account writer in either mode; legacy setup is
   available only through securely interactive CLI/UI with migration guidance.
8. Import preview is deterministic and secret-free across TOML, keyring
   references, environment accounts, and environment policy precedence; apply is
   target-bound, leaves source bytes unchanged, and rejects source, private-secret,
   catalog, or account drift through the final fenced cutover before creating a
   mixed endpoint/credential result or selecting managed mode.
9. Partial import and external cleanup failures return recoverable durable state;
   failed credential saves preserve prior binding authority, and only Linux
   managed-SQLite insertion claims atomicity with activation.
10. CLI and UI share management application semantics, but adapter scope is
    intentional: provider connectivity tests remain CLI diagnostics and have no
    Web UI route or control; neither interface edits legacy TOML as its normal
    management model.
