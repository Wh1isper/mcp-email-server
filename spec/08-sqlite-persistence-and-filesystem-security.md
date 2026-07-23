# 08. SQLite Persistence and Filesystem Security

## Scope and Ownership

Managed mode uses one SQLite file for two ownership classes:

- authoritative non-secret catalog state: catalog lifecycle, accounts,
  endpoints, policy, revisions, secret-binding lifecycle, and import state;
- rebuildable metadata projection: logical mailboxes, placement metadata,
  coverage, freshness, stale markers, and bounded maintenance state.

Tables, transactions, and repositories MUST preserve this distinction. Rebuild
or retention of projection data cannot alter catalog authority.

## Logical Schema

The exact physical schema is versioned and verified by migration tests. The
logical model includes:

- catalog metadata: stable ID, lifecycle, revision, schema version, timestamps;
- accounts: stable ID, normalized unique name, display data, lifecycle,
  revision, timestamps;
- IMAP/SMTP endpoint configuration without credentials;
- catalog defaults and account policy overrides;
- role binding state: revision, opaque internal handles, pending/superseded and
  cleanup/repair status;
- import plan/application state needed for safe bounded resume;
- logical mailbox projection keyed by account and canonical mailbox;
- observed placement rows keyed by account, mailbox, UIDVALIDITY, and UID;
- coverage/freshness/staleness metadata and bounded maintenance cursors.

The account normalized-name uniqueness constraint covers all rows, including
soft-removed tombstones. Logical mailboxes do not have a fictional business
revision; their provider epoch and coverage fields qualify observations.

Raw credentials, bodies, raw MIME, attachment bytes, UI bootstrap/session/CSRF
state, browser view state, and provider access tokens MUST NOT be stored.

## Schema Version and Migration

A new catalog is created at the current exact schema version. Opening:

- rejects a newer/unknown version;
- rejects missing required objects, unexpected incompatible shape, or invalid
  invariants;
- runs only explicit ordered migrations from supported older versions;
- performs migrations under bounded exclusive coordination;
- records the new version only after all migration work succeeds;
- leaves no partially advertised version on rollback or crash.

No startup path silently drops/recreates authoritative tables. Projection-only
rebuild is explicit and isolated.

## Filesystem Layout and Locking

The database, existing `-wal` and `-shm` sidecars, bootstrap file, and any
application lock file are security-sensitive. Supported paths are local and
owned by the current user. Network filesystems and platforms that cannot provide
the required ownership/no-follow semantics fail with remediation.

Concurrent initialization and activation use an application lock with bounded
wait. SQLite busy timeout is finite and maps to a typed busy error. Locks are not
held while contacting providers or `SecretStore`.

## Mandatory Pre-open Sequence

Existing sidecars must be validated before SQLite can touch them:

```text
canonicalize intended paths without following final symlinks
    -> validate parent chain
    -> validate existing DB, WAL, SHM, and lock entries
    -> acquire secure application lock
    -> repeat validation after lock
    -> open/connect SQLite
    -> configure journal mode and create required files
    -> revalidate DB, WAL, SHM, lock identity/owner/type/mode
    -> serve work
```

Preflight rejects any existing symlink, non-regular file, wrong owner, unsafe
mode, unexpected hard-link condition where detectable, insecure parent, or path
identity replacement. It MUST happen before connect and before enabling WAL.
Post-create validation closes the race for newly created sidecars. Failure closes
the connection and fails closed.

Files are owner-only (`0600` or stricter equivalent); private parent directories
are owner-only (`0700` or stricter equivalent). Creation uses restrictive umask
and no-follow/exclusive primitives where available. Unsupported guarantees are
reported, not silently weakened.

## Connection and Transaction Rules

- Foreign keys are enabled and verified per connection.
- WAL is enabled only after preflight and is not assumed safe merely because
  SQLite created the sidecars.
- Busy timeout and synchronous/durability settings are explicit.
- Read/write transactions are short and bounded by row counts.
- Compare-and-swap updates include expected revision in the write predicate and
  return conflict when zero rows match.
- Network, `SecretStore`, browser launch, and attachment I/O never run inside a
  transaction.
- Repository methods return domain/application values and typed errors, not raw
  rows or SQL messages.

## Projection Consistency

Provider reads are completed before projection writes. A projection transaction
may upsert one bounded observation batch and coverage state. UIDVALIDITY change
atomically marks/replaces the old projection epoch before it can answer current
queries. Interrupted/partial scans do not advance complete coverage.

A provider mutation marks affected projection data stale or applies a proven
bounded update after provider evidence. Projection failure returns a warning and
does not rewrite provider outcome.

## Retention, Health, and Rebuild

Retention is bounded by rows, bytes represented, account/mailbox count per pass,
and deletion batch. It removes projection data only. Health queries have strict
row/time/result limits and summarize schema, WAL/security state, projection
counts, oldest/newest observation, stale coverage, and maintenance needs without
leaking SQL or message content.

Rebuild:

- requires explicit management intent;
- can clear/recreate projection objects only;
- preserves catalog, accounts, policy, bindings, import state, and revisions;
- does not contact providers while holding schema transactions;
- reports busy/corrupt/insecure state safely.

## Corruption and Recovery

SQLite corruption, I/O failure, incompatible schema, insecure replacement, or
unsafe sidecar causes managed startup/operation failure with bounded remediation.
The application does not fall back to legacy and does not automatically destroy
or overwrite the database. Offline backup/restore and authoritative hard reset
remain separate operator procedures outside this delivery.

## Acceptance Criteria

1. Schema tests assert exact version, required constraints, foreign keys, and
   absence of secret/body/UI-session columns.
2. Soft-removed account names remain unique, and logical mailbox rows have no
   unsupported business revision contract.
3. Pre-existing DB/WAL/SHM/lock symlinks, non-regular files, wrong ownership,
   unsafe modes, insecure parents, and replacement races fail before unsafe
   SQLite access.
4. Newly created WAL/SHM files are post-validated and owner-only.
5. Concurrent initialize/activate and busy timeout behavior is deterministic and
   bounded.
6. Crash and migration tests never advertise a partially migrated schema.
7. External network, secret, and large filesystem work is absent from SQLite
   transaction scopes.
8. UIDVALIDITY invalidation and incomplete coverage are transactionally safe.
9. Retention and rebuild alter projection only and preserve every authoritative
   catalog and binding row.
10. Corrupt, incompatible, busy, and insecure states produce bounded typed errors
    with no automatic legacy fallback or destructive repair.
