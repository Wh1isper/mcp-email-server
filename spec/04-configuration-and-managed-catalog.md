# 04. Configuration and Managed Catalog

## Authority Selection

Bootstrap configuration contains the explicit mode and, for managed mode, the
selected catalog path. It contains no managed account configuration or secret.
The process reads it once, validates it, and freezes the result.

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
  conflict with an existing incompatible target.
- **staging** permits management and import but cannot serve mail workflows.
- **activate** validates schema, security, required configuration, and invariant
  consistency before a revisioned transition to active.
- **select** atomically updates bootstrap authority after the active catalog is
  revalidated.
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
mutation.

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

Policy updates are revisioned. Permissive changes do not bypass capability or
input validation. Restrictive changes take effect on the next independent
effect because authority is revalidated at operation boundaries.

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
- whether each required credential can be resolved at apply time;
- target catalog identifier and expected catalog/account revisions.

The fingerprint covers all source fields that affect apply, including effective
endpoint configuration and secret source identity without revealing its value.

### Apply

Apply requires explicit confirmation plus the exact preview token/fingerprint and
expected target revisions. It re-reads both source and target. Any material drift
returns `preview_stale` before secret or catalog writes.

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

Bounded status reports mode, selected path in a safely displayable form, catalog
lifecycle/schema/revision, account counts by lifecycle, incomplete credential
states, and restart requirement. Doctor performs opt-in bounded checks for
bootstrap, file security, schema, binding consistency, secret resolution, and
provider connectivity. Results expose categories and remediation, never values,
SQL, raw provider responses, or reusable locators.

## Acceptance Criteria

1. Missing bootstrap retains only the historical implicit-legacy rule; explicit
   managed selection fails closed with no fallback.
2. Initialize, activate, select, and restart semantics are distinct and covered
   through CLI and UI.
3. Every account, policy, lifecycle, import, and binding mutation rejects stale
   revisions with a bounded current summary.
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
