# 02. Domain Model and Authority

## Purpose

This document defines the shared domain language and sources of truth used by
all workflows. It does not define storage rows, protocol wire models, browser
sessions, or framework objects.

## Core Entities

### Operational account

An operational account is the stable identity used for policy, provider access,
and metadata partitioning.

- Managed accounts have an immutable internal identifier, unique normalized
  name, non-secret endpoint configuration, lifecycle state, revision, and opaque
  secret bindings.
- Legacy accounts derive their operational identity deterministically from the
  frozen effective legacy configuration.
- Display name, username, endpoint, enabled state, and credential can change
  without changing a managed account identifier.
- A soft-removed managed account remains a tombstone. Its normalized name stays
  reserved until a future explicit hard-purge design.

Lifecycle states are:

```text
enabled <-> disabled -> removed
```

`removed` is terminal in this delivery. Disabled and removed accounts cannot be
selected for provider work.

### Endpoint

An endpoint is non-secret connection configuration for one provider role:
protocol, host, port, username, and transport policy. IMAP and SMTP endpoints are
separate values even when they happen to share fields.

### Policy

Policy is local authority for whether an otherwise valid effect is allowed and
for its limits. Policy does not prove provider capability or success. Effective
policy is evaluated from a fresh account snapshot before each independent
effect.

### Capability

Capability is current evidence that a provider can support a protocol operation.
It can become stale and MUST be checked at the operation boundary when safety
depends on it. A configured preference does not create a capability.

### Secret binding

A secret binding is an opaque association between an account endpoint role and
a secret managed by `SecretStore`. It is not the secret value. Application DTOs
may contain bounded binding status or an opaque internal handle, but public DTOs
MUST NOT expose reusable locators.

### Mailbox and placement

A mailbox has a provider name, delimiter, and attributes. A durable observed
message placement is identified by:

```text
operational account + canonical mailbox + UIDVALIDITY + UID
```

UID is only meaningful within a mailbox epoch. Changing UIDVALIDITY invalidates
prior placement observations for that logical mailbox.

The existing public numeric email ID is a compatibility identifier. It does not
encode or prove the listing epoch. A mutation using it MUST resolve the current
selected account and mailbox, validate current authority, and bracket the effect
with current provider state where feasible. A future public epoch-bound ID
requires a versioned interface change.

### Metadata observation

A metadata observation is a bounded local projection of provider evidence. It
may contain envelope fields, flags, size, internal date, UID identity, coverage,
and freshness data. It is not provider authority and can be discarded or
rebuilt.

### Revision

A revision is a monotonic concurrency token for a mutable local aggregate such
as catalog metadata, account configuration, policy, or secret-binding state. A
mutation supplies its expected revision. Mismatch yields a conflict rather than
overwriting newer state.

Logical mailbox observations do not need an independent business revision;
their identity and coverage tokens provide projection qualification.

## Authority Matrix

| Concept                                      | Authority                                | Consequence                                          |
| -------------------------------------------- | ---------------------------------------- | ---------------------------------------------------- |
| process mode and selected catalog path       | bootstrap snapshot                       | frozen until restart                                 |
| managed account, policy, lifecycle, revision | selected managed catalog                 | no environment or TOML override                      |
| managed secret value                         | `SecretStore`                            | never copied to catalog or ordinary cache            |
| managed binding lifecycle                    | selected catalog                         | value operations coordinate through revisioned state |
| legacy effective account                     | TOML plus environment composition        | available only in legacy mode                        |
| provider capability                          | current provider session evidence        | rechecked when safety depends on it                  |
| mailbox/message state                        | IMAP                                     | index absence cannot delete provider truth           |
| SMTP delivery                                | SMTP response evidence                   | not inferred from sent-copy state                    |
| sent-copy placement                          | IMAP APPEND evidence                     | independent from SMTP delivery                       |
| attachment destination                       | caller request plus current local policy | exact path, no implicit rewrite                      |

## Provider Outcomes

Every externally visible provider target or effect maps to one of:

- **success**: sufficient positive protocol evidence exists;
- **failure**: sufficient negative evidence shows the effect did not complete;
- **unknown**: interruption or ambiguous response prevents proof either way;
- **success with local warning**: provider success is known but projection,
  cleanup, or secondary local bookkeeping failed;
- **reconciliation required**: a durable local lifecycle cannot complete safely
  without an explicit follow-up action.

Unknown is not failure and MUST NOT be automatically retried for a non-idempotent
effect. Cleanup-required is not silent success.

## Bounded Result

A bounded result has all of:

- validated request cardinality and string/byte limits;
- bounded provider work once cardinality is knowable;
- bounded local CPU, memory, rows, and filesystem bytes;
- bounded per-item detail and aggregate serialization;
- explicit truncation, rejection, or continuation semantics;
- a deadline where the boundary can enforce one safely.

The basic IMAP SEARCH UID response can be unbounded before cardinality is known.
It is the documented residual: the command has a deadline and its returned UID
set is rejected above the candidate ceiling before subsequent fetch or result
construction.

## Cross-domain Invariants

1. Managed selection never falls back to legacy.
2. Secret values never become domain values or ordinary DTO fields.
3. Independent provider effects revalidate account, policy, binding, and needed
   capability before beginning.
4. Network and `SecretStore` access never runs inside a SQLite transaction.
5. Partial observation never proves deletion by absence.
6. Provider success remains success when only local projection fails.
7. Ambiguous non-idempotent effects are not replayed automatically.
8. Every public collection, error-detail collection, and serialized result has a
   ceiling.
9. Revisions prevent stale management forms or concurrent commands from silent
   overwrite.
10. Interface adapters may translate domain results but cannot redefine them.

## Domain Purity

Domain types MUST NOT depend on FastMCP, Typer, Starlette, React, SQLite,
keyring, IMAP/SMTP client classes, or concrete filesystem APIs. Protocol- and
storage-specific values are mapped at ports and adapters.

## Acceptance Criteria

1. Application, CLI, MCP, and UI code use the same definitions for account
   identity, revision conflict, provider outcome, and bounded result.
2. Tests prove account rename/update does not change managed identity and soft
   removal continues to reserve the normalized name.
3. No raw secret or reusable secret locator appears in domain/public DTOs,
   equality representations, serialization, or exceptions.
4. UIDVALIDITY change invalidates old placement observations, while the public
   numeric ID is never documented as listing-epoch proof.
5. Unknown effects and cleanup-required outcomes are distinct from ordinary
   failure and ordinary success at every adapter.
6. Domain modules import no interface framework, persistence engine, keyring, or
   concrete mail client.
