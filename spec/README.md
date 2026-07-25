# Local Email App Specifications

This directory defines the normative product and architecture contract for the
Local Email App. The documents are organized by domain and design ownership.
They describe the intended behavior and acceptance conditions; they do not claim
that a requirement is implemented merely because it appears here.

User-facing behavior belongs under [`docs/`](../docs/). Implementation plans,
research notes, migration diaries, and test logs do not belong in these specs.

## Product Definition

The Local Email App connects one operating-system user on one machine to existing
IMAP and SMTP accounts. It exposes mail workflows to MCP clients and provides a
local management plane through CLI and an authenticated loopback Web UI.

The product has two explicit configuration modes:

- **legacy** preserves the existing TOML, environment-composition, and legacy
  credential behavior;
- **managed** uses SQLite as the authority for non-secret configuration and a
  `SecretStore` as the authority for credentials.

IMAP remains authoritative for mailbox membership, message placement, metadata,
and flags. SMTP responses remain authoritative for delivery evidence. SQLite
contains local authority only where explicitly stated and otherwise contains
bounded, rebuildable observations.

## Domain Map

| Spec                                                                                                   | Owning concern                                                                                |
| ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| [`01-system-context.md`](01-system-context.md)                                                         | actors, scope, process model, trust boundaries, and global invariants                         |
| [`02-domain-model-and-authority.md`](02-domain-model-and-authority.md)                                 | domain language, identities, sources of truth, revisions, and policy ownership                |
| [`03-application-architecture.md`](03-application-architecture.md)                                     | layers, services, ports, composition, request boundaries, and resource lifecycle              |
| [`04-configuration-and-managed-catalog.md`](04-configuration-and-managed-catalog.md)                   | bootstrap, legacy/managed selection, catalog authority, account lifecycle, policy, and import |
| [`05-credentials-and-secret-lifecycle.md`](05-credentials-and-secret-lifecycle.md)                     | secret authority, late resolution, candidate rotation, removal, cleanup, and redaction        |
| [`06-mail-read-model-and-metadata-index.md`](06-mail-read-model-and-metadata-index.md)                 | mailbox discovery, metadata projection, coverage, provider fallback, bodies, and attachments  |
| [`07-mail-mutations-and-provider-effects.md`](07-mail-mutations-and-provider-effects.md)               | mark, append, move, archive, delete, SMTP, sent-copy, cancellation, and uncertain outcomes    |
| [`08-sqlite-persistence-and-filesystem-security.md`](08-sqlite-persistence-and-filesystem-security.md) | logical schema, transactions, migrations, exact ownership, WAL, permissions, and retention    |
| [`09-local-management-ui.md`](09-local-management-ui.md)                                               | React UI scope, loopback server, bootstrap/session security, concurrency, and packaging       |
| [`10-mcp-interface-and-compatibility.md`](10-mcp-interface-and-compatibility.md)                       | stdio baseline, mail-only catalog, schemas, compatibility, bounds, and errors                 |
| [`11-agent-integration-and-safe-setup.md`](11-agent-integration-and-safe-setup.md)                     | Codex/Claude Code integration, safe CLI/UI handoff, installation, and no-secret agent rules   |
| [`12-delivery-validation-and-evolution.md`](12-delivery-validation-and-evolution.md)                   | verification map, test layers, package/release gates, documentation, and future change rules  |

Cross-references point to the owning document rather than duplicating its full
contract. This README is the only ordered navigation map.

## Sources of Truth

| Information                                        | Authority                   | Other local representations                         |
| -------------------------------------------------- | --------------------------- | --------------------------------------------------- |
| selected mode and managed database path            | bootstrap configuration     | process-frozen startup snapshot                     |
| managed non-secret accounts and policy             | managed SQLite catalog      | immutable operation snapshots                       |
| managed secret values                              | `SecretStore`               | no value cache or ordinary SQLite field             |
| managed secret binding state                       | managed SQLite catalog      | bounded status DTOs without locators                |
| legacy stored accounts and global policy           | legacy TOML                 | environment-composited runtime view in legacy mode  |
| environment-defined accounts and overrides         | current process environment | legacy effective snapshot only                      |
| mailbox membership, placement, metadata, and flags | IMAP                        | bounded SQLite observation                          |
| message delivery                                   | SMTP protocol evidence      | typed delivery outcome                              |
| sent-copy placement                                | IMAP APPEND evidence        | typed secondary outcome and stale projection marker |
| UI authentication/session state                    | current UI process memory   | secure cookie and one-time bootstrap token          |

## Cross-cutting Invariants

1. A persisted managed selection never falls back to legacy automatically.
2. The selected mode is frozen for a process. Changing it requires restart.
3. Account and policy authority is revalidated before each independent provider
   effect.
4. Managed credentials are resolved only for the selected account and required
   endpoint role, immediately before provider construction. Secret values do not
   enter application DTOs, process-wide settings caches, SQLite, any MCP input or
   result, agent chat/skill state, URLs, logs, diagnostics, or browser storage.
5. Network, `SecretStore`, and large filesystem operations never run inside a
   SQLite transaction.
6. A durable IMAP placement identity includes operational account, mailbox,
   UIDVALIDITY, and UID. The existing public numeric email ID is a compatibility
   identifier, not proof of the listing epoch.
7. Partial observation never proves provider deletion by absence.
8. Body reads use PEEK unless the caller explicitly requests a separate read
   mutation.
9. Scoped delete and move fallback never issue mailbox-wide bare `EXPUNGE`.
10. An ambiguous provider effect is reported as unknown and is never
    automatically replayed.
11. SMTP delivery and sent-copy APPEND are separate effects; sent-copy failure
    never causes SMTP resubmission.
12. Public requests, provider work after cardinality is known, local work,
    serialized results, and error details have explicit bounds. Basic IMAP
    SEARCH's pre-cardinality UID response is the sole documented provider-payload
    residual and is constrained by a command deadline plus a strict
    post-response candidate ceiling.
13. The stdio tool catalog remains stable for the life of a process; current
    authority and capability are enforced at call time.
14. Attachment materialization preserves the caller's exact requested
    destination for compatibility, but only after explicit enablement, fresh
    policy validation, bounded fetch, and no-follow filesystem checks.
15. The local Web UI binds only to `127.0.0.1`, uses an ephemeral port by
    default, has no share/remote mode, and treats every browser request as
    untrusted until authenticated and CSRF-validated.
16. MCP exposes no account or credential management. Optional agent skills hand
    users off to interactive CLI/UI and never collect or relay credentials.

## Product Surfaces

| Surface               | Responsibility                                                                   |
| --------------------- | -------------------------------------------------------------------------------- |
| MCP stdio             | bounded mail discovery and mail workflows; no account management or secret entry |
| CLI                   | complete headless management, recovery, automation, and safe secret input        |
| Local Web UI          | complete graphical management plane; no mailbox reader or composer               |
| Agent skill/plugin    | optional non-secret discovery and user handoff to CLI/UI                         |
| SSE / Streamable HTTP | retained compatibility commands, not the target managed architecture             |
| MCP Apps              | not part of this delivery; requires a separate accepted proposal                 |

The existing public `mcp-email-server ui` command remains the graphical entry
point and is replaced in place by the embedded local React UI. There is no new
`webui` command and no daemon.

## Scope

The delivery governed by these specs includes:

- immediate managed initialization for fresh installs, v1-safe reviewed migration, selection, and diagnostics;
- account create/read/update/disable/re-enable/soft-remove;
- managed policy and credential lifecycle;
- preview-first, explicit legacy import;
- shared application services for CLI, Web UI, and MCP adapters;
- bounded mailbox, metadata, body, attachment, and mutation workflows;
- a bounded metadata projection in SQLite;
- an authenticated loopback management UI packaged inside Python artifacts;
- a repository-distributed Codex/Claude Code integration that guides safe setup
  without accepting credentials;
- removal of the historical secret-bearing MCP account-add tool with migration
  guidance to interactive CLI/UI;
- complete contract, security, package, and GreenMail validation.

It does not include:

- hosted or multi-user service behavior;
- a daemon or background synchronizer;
- a remotely supported management API;
- MCP Apps;
- full offline mail replication;
- FTS, persistent bodies, raw MIME, or attachment payload storage;
- QRESYNC/VANISHED optimization;
- generic operation journals, leases, evidence ledgers, or continuity systems;
- online backup/restore or hard purge;
- exactly-once guarantees for ambiguous IMAP or SMTP effects.

## Specification Language

`MUST`, `MUST NOT`, `SHOULD`, and `MAY` are normative. Each detailed document
ends with acceptance criteria. Verification evidence is centralized in
[`12-delivery-validation-and-evolution.md`](12-delivery-validation-and-evolution.md),
so specs remain stable contracts instead of implementation logs.
