# 04. Configuration and Managed Catalog

## Authority Selection

Bootstrap configuration contains the explicit mode, selected catalog path, and
an independent monotonic bootstrap revision. It contains no managed account
configuration or secret. The process reads it once, validates it, and freezes
the result. Management writes compare the expected bootstrap revision while
holding a bounded owner-only no-follow sibling lock before atomic replacement;
the catalog revision remains a separate concurrency domain.

Selection rules:

1. A machine with no bootstrap selection follows the historical legacy behavior
   for backward compatibility.
2. An explicit `legacy` selection uses legacy TOML plus current environment
   composition.
3. An explicit `managed` selection requires the named catalog to exist, pass
   filesystem/schema checks, and be active.
4. A persisted managed selection never falls back to legacy when validation,
   credential, provider, or runtime work fails.
5. Changing selection takes effect only after process restart.

Environment variables may compose the legacy view and may locate bootstrap
configuration, but MUST NOT override managed account, policy, lifecycle, or
secret-binding authority.

## Catalog Lifecycle

A managed catalog has a stable identifier, schema version, lifecycle, and catalog
revision. Its lifecycle is:

```text
absent -> staging -> active
```

- **initialize** creates a secure staging catalog idempotently or reports a typed
  conflict with an existing incompatible target. Idempotent adoption is limited
  to the exact requested path when it remains an owner-only, structurally valid
  `STAGING` catalog; it preserves all staging data and revisions. A bootstrap
  compare-and-swap or persistence failure after catalog commit leaves that
  catalog available for an explicit retry rather than deleting it.
- **staging** permits management and import but cannot serve mail workflows.
- **activate** validates schema, security, required configuration, and invariant
  consistency before a revisioned transition to active.
- **select managed** revalidates the exact active catalog revision, then
  compare-and-swaps bootstrap authority.
- **select legacy** compare-and-swaps bootstrap authority without opening the
  selected managed catalog, so an unavailable catalog does not block explicit
  recovery.
- The running process continues using its frozen prior selection and tells the
  operator that restart is required.

Activation and selection are separate actions. Neither silently imports legacy
state or deletes the legacy configuration.

## Managed Account Lifecycle

Managed account operations are available through the shared application service:

- list/show bounded non-secret summaries;
- create with unique normalized name and validated non-secret endpoints;
- update display/name, endpoints, or account policy;
- disable and re-enable;
- soft-remove;
- set, rotate, remove, and clean up credentials through spec 05;
- test IMAP and SMTP connectivity with explicit role selection.

Every mutation supplies the expected account or catalog revision. On mismatch,
it returns a typed conflict containing only a bounded current non-secret summary
and requires the caller to review/retry. The UI MUST NOT auto-replay a stale
mutation. Live legacy and managed authority is capped at 1,000 configured
accounts; managed creation checks that aggregate inside the catalog transaction.
Recipient and sender policy collections are each capped at 1,000 entries.

Soft removal preserves the stable identity, audit timestamps, binding cleanup
state, and normalized-name tombstone. It disables provider work immediately.
Hard purge and name reuse are deferred.

Connectivity tests use the same current authority, late secret resolution,
provider TLS policy, limits, and redaction as mail workflows. They do not save a
credential, enable an account, or activate a catalog as a side effect.

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
legacy composition use the same canonicalizers. Permissive changes do not bypass
capability or input validation. Restrictive changes take effect on the next
independent effect because authority is revalidated at operation boundaries.

## Legacy Mode

Legacy mode preserves established TOML and environment composition. New managed
catalog semantics MUST NOT reinterpret legacy precedence.

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
and returns a deterministic non-secret plan:

- source fingerprint and expiry/creation time;
- planned creates, safe matches, conflicts, skips, and warnings;
- endpoint and policy summaries without secret values or reusable locators;
- each required credential role and its source class (`plaintext` or `keyring`),
  without probing source availability;
- target catalog identifier and exact expected catalog, policy, and account
  revisions.

The fingerprint covers all source fields that affect apply, including effective
endpoint configuration and secret source identity without revealing its value.

### Apply

Apply requires explicit confirmation plus the exact preview token/fingerprint and
expected target revisions. CLI apply prints the complete non-secret plan before
reading confirmation in the same process; UI apply binds confirmation to its
one-time preview token. Apply re-reads both source and target before each required
credential resolution and destination write. It advances only revisions caused by
its own successful steps; unrelated fresh reads never replace reviewed expected
revisions. Any material drift returns `preview_stale` before the next secret or
catalog write.

For each bounded item it uses the credential candidate protocol and minimal
revisioned catalog transactions. A failure returns typed per-item outcomes and
repair/cleanup state; it does not invent all-or-nothing atomicity across SQLite,
legacy source, and `SecretStore`. Safe resume requires the same source snapshot
and current durable import state. Import never deletes or rewrites legacy source.

## Writer Fences

All legacy CLI/UI writers fail before mutation in managed mode. No legacy writer
is registered through MCP. All managed writers require a selected or explicitly
targeted secure managed catalog and never mutate legacy state. Fences live in
application services, not only CLI/UI command checks.

## Management Status and Doctor

Bounded status reports mode, bootstrap revision, selected path in a safely
displayable form, catalog lifecycle/schema/revision, account counts by lifecycle,
incomplete credential states, and restart requirement. A missing, corrupt,
incompatible, or insecure selected catalog produces a bounded unavailable
category while preserving the bootstrap state needed to select legacy; status
does not silently fall back. Doctor performs opt-in bounded checks for
bootstrap, file security, schema, binding consistency, secret resolution, and
provider connectivity. Results expose categories and remediation, never values,
SQL, raw provider responses, or reusable locators.

## Acceptance Criteria

1. Missing bootstrap retains only the historical implicit-legacy rule; explicit
   managed selection fails closed with no fallback.
2. Initialize, activate, bootstrap-CAS selection, unavailable-catalog recovery,
   and restart semantics are distinct and covered through CLI and UI.
3. Every account, policy, lifecycle, import, binding, and bootstrap mutation
   rejects stale revisions with a bounded current summary; account and policy
   cardinality limits are enforced on both read and write boundaries.
4. Soft removal disables provider work and permanently reserves the normalized
   name in this delivery.
5. The MCP catalog contains no account writer in either mode; legacy setup is
   available only through securely interactive CLI/UI with migration guidance.
6. Import preview is deterministic and secret-free; apply rejects source or
   target drift before creating a mixed endpoint/credential result.
7. Partial import and external cleanup failures return recoverable durable state
   without claiming cross-store atomicity.
8. CLI and UI expose equivalent managed capabilities and application semantics;
   neither edits legacy TOML as its normal management model.
