# 08. SQLite Persistence and Filesystem Security

## Scope and Ownership

Managed mode uses one owner-only SQLite file for up to three ownership classes:

- authoritative non-secret catalog state: accounts, endpoints, policy, revisions,
  secret-binding lifecycle, and import state;
- on Linux and Windows, authoritative managed secret values in the dedicated
  `managed_secret` table owned by the `SecretStore` adapter;
- rebuildable metadata projection: logical mailboxes, placement metadata,
  coverage, freshness, stale markers, and bounded maintenance state.

Tables, transactions, and repositories MUST preserve this distinction. Rebuild
or retention of projection data cannot alter catalog authority.

## Logical Schema

The exact physical schema is versioned and verified by exact-schema and unsupported-version tests. The
logical model includes:

- catalog metadata: stable ID, revision, schema version, timestamps;
- accounts: stable ID, normalized unique name, display data, lifecycle,
  revision, timestamps;
- IMAP/SMTP endpoint configuration without credentials;
- catalog defaults and account policy overrides;
- role binding state: revision, opaque internal handles, active/superseded and
  cleanup status;
- on Linux and Windows, `managed_secret` rows keyed by opaque handle, with the
  secret value isolated from all catalog and projection queries;
- import plan/application state needed for safe bounded continuation;
- logical mailbox projection keyed by account and canonical mailbox;
- observed placement rows keyed by account, mailbox, UIDVALIDITY, and UID;
- coverage/freshness/staleness metadata and bounded maintenance cursors.

The account normalized-name uniqueness constraint covers all rows, including
soft-removed tombstones. Logical mailboxes do not have a fictional business
revision; their provider epoch and coverage fields qualify observations.

Raw credentials and provider access tokens MUST NOT be stored outside the Linux
or Windows `managed_secret` table or the selected macOS system keyring. Linux and
Windows catalog copies, snapshots, and backups therefore contain plaintext
secret values and MUST retain private protection equivalent to the original;
they are never classified as non-secret databases. Bodies, raw MIME,
attachment bytes, UI bootstrap/session/CSRF state, and browser view state MUST
NOT be stored in managed SQLite.

## Schema Version and Migration

A new catalog is created at the current exact schema version. Opening:

- rejects a newer/unknown version;
- rejects missing required objects, unexpected incompatible shape, or invalid
  invariants;
- runs only explicit ordered migrations if an older version is explicitly declared supported;
- performs migrations under bounded exclusive coordination;
- records the new version only after all migration work succeeds;
- leaves no partially advertised version on rollback or crash.

No startup path silently drops/recreates authoritative tables. Projection-only
rebuild is explicit and isolated. There are no released managed-catalog users,
so this delivery makes no compatibility or automatic-migration commitment for
pre-release managed schema files. Legacy TOML, environment, and keyring import
remains supported through the explicit spec 04 workflow.

## Filesystem Layout and Locking

The database, existing `-wal` and `-shm` sidecars, the independent bootstrap
sidecar (`config.bootstrap.toml` for a `config.toml` legacy source), and every
application lock file are security-sensitive. The bootstrap lock is derived from
the sidecar as `config.bootstrap.toml.lock`; selection writes atomically replace
the sidecar and never replace the legacy source. Supported paths are local and
owned by the current user. Network filesystems and platforms that cannot provide
the required ownership/no-follow semantics fail with remediation.

Concurrent initialization and catalog access use an application lock with bounded
wait. SQLite busy timeout is finite and maps to a typed busy error. POSIX retains
the existing owner-only no-follow `fcntl` lock. Windows uses the maintained
`filelock` Windows implementation only after project-owned validation of the
lock's parent chain; the acquired lock object is then revalidated from its held
non-reparse handle. The Windows lock has a fixed bounded timeout, denies delete
sharing while held, and relies on `LockFileEx` process-termination release.
Locks are not held while contacting providers or the system keyring. A Linux or
Windows `managed_secret` insert is local database work and occurs under the same
bounded
transaction as binding activation.

This is a local single-user boundary. The implementation defends against stale
entries, unsafe permissions, links, and untrusted lower-privilege filesystem
content, but it does not claim containment from a malicious process already
running as the same OS user. Such a process can replace user-owned paths and can
usually inspect the user's process and keyring state. Same-UID hostile path
replacement is therefore outside the supported threat model; on Windows the
same statement applies to another process running under the same user SID.
Multi-user or hostile-same-principal operation requires a separately designed
privileged broker or sandbox rather than stronger wording around path preflight.

## Platform Security Profiles

POSIX storage retains the existing owner UID, `0700` directory, `0600` regular
file, directory-descriptor, no-follow, single-link, identity, and advisory-lock
contract.

Windows support is deliberately limited to an ordinary drive-letter path on a
local fixed NTFS volume with a validated parent below the volume root. Direct
volume-root storage, UNC/network paths, mapped or remote drives, device and
extended device namespaces, alternate data streams, FAT/exFAT, and unknown
filesystem types fail closed before creating a parent, opening SQLite, fetching
an attachment, or performing a keyring/authority effect. A path containing a
colon outside its drive designator is rejected.

Each existing Windows component is opened with
`FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS` as appropriate and
kept open without delete sharing for the sensitive operation. Any reparse-point
attribute or tag is rejected, including file/directory symlinks, junctions,
mount points, and provider-defined reparse points. Security and identity checks
are made from these handles, not from pathname metadata alone. Object identity
is the volume serial number and file index returned by
`GetFileInformationByHandle`; expected type, regular-disk status, size, and link
count are checked with it. An existing exact file target must have one hard link.

The current user SID owns private objects. Their DACL is protected and grants
only the current user, LocalSystem, and built-in Administrators. No other SID
may have an allow ACE with any access mask; this includes raw generic masks
before Windows maps them to file or directory rights. Existing
traversed ancestors may grant ordinary read/traverse and unrelated sibling
creation, but no untrusted SID may replace/delete the traversed component,
modify its ACL/owner, or create a reparse substitution at the sensitive parent.
A null or invalid DACL, unknown allow-ACE form, unresolved ownership, or an owner
outside the trusted set fails closed. Private immediate parents use the stricter
private-object policy.

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
Post-create validation closes the race for newly created sidecars. Because a
last SQLite close may delete and a later connection may recreate WAL/SHM under
new identities, setup rehardens and revalidates every current sidecar rather
than relying only on its pre-open name. Pre-open or setup failure closes the
connection and fails closed. A post-commit reconciliation lock race is warning-
only and deferred so a committed mutation is not reported as failed; the next
open still repeats strict validation before SQLite.

On POSIX, files are owner-only (`0600`) and private parent directories are
owner-only (`0700`); creation uses restrictive mode, no-follow/exclusive
primitives, and advisory locking. On Windows, files and directories use the
protected private DACL above, exclusive non-reparse creation, handle identity,
and the hardened lock contract. Platforms and filesystems without their complete
profile reject managed catalog and bootstrap effects before creating their
target or parent; no weaker path-based fallback is used.

The application lock remains held through SQLite connect, WAL enablement, and
post-create validation. Existing and newly created DB, `-wal`, and `-shm`
objects must be private, non-reparse, regular, single-link files whose
handle-bound identities remain stable across setup. A failed post-check closes
the connection and all pinned handles before returning a typed bounded error.

## Durable Replacement and Crash Cleanup

Windows authority, attachment, and spill writers create a cryptographically
random same-directory temporary sibling with `CREATE_NEW`, the protected private
DACL, and no delete sharing. They write through the held handle and call
`FlushFileBuffers` before replacement. An overwrite uses
`MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)` on the same
volume; `MOVEFILE_COPY_ALLOWED` is never set. No-clobber uses an equivalent
exclusive final transition rather than a check-then-rename sequence. The final
target is reopened without following reparse points and must have the temporary
object's recorded identity, expected type, size, owner, DACL, and link count.
Failure before replacement preserves an existing target; after replacement,
observers may see complete old or complete new content, never a partial write.
POSIX retains its existing same-directory durable atomic-write contract.

Cleanup never deletes by name alone. In-operation cleanup removes only the
created object after matching its recorded held/reopened identity. Bootstrap
and attachment temporaries have distinct exact prefix-plus-random-token name
shapes and a bounded matching-candidate/age scope. Windows spill roots live in a
dedicated private temporary container, so unrelated shared-temp entries cannot
starve or inflate their bounded cleanup scan. On next operation or startup, a remnant is eligible
only after non-follow open proves the expected local volume, regular
file/directory type, trusted owner, private mode/DACL, single-link condition
where applicable, and a stable identity immediately before deletion. Reparse,
foreign, permissive, renamed/substituted, excessive, or otherwise unknown
entries are left untouched and produce a bounded warning. Graceful and killed-
process cleanup follow the same rule.

## Connection and Transaction Rules

- Foreign keys are enabled and verified per connection.
- WAL is enabled only after preflight and is not assumed safe merely because
  SQLite created the sidecars.
- Busy timeout and synchronous/durability settings are explicit.
- Read/write transactions are short and bounded by row counts.
- Compare-and-swap updates include expected revision in the write predicate and
  return conflict when zero rows match.
- Network, system-keyring, browser-launch, and attachment I/O never run inside a
  transaction.
- On Linux and Windows, insertion into `managed_secret`, activation of its
  binding, the binding/account revision update, and transition of the superseded value to
  cleanup-required are one transaction; rollback leaves binding authority and
  secret rows unchanged.
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

1. Schema tests assert exact version, required constraints, foreign keys, the
   Linux/Windows dedicated `managed_secret` boundary, and absence of secret values
   from catalog/projection/body/UI-session columns.
2. Soft-removed account names remain unique, and logical mailbox rows have no
   unsupported business revision contract.
3. Pre-existing DB/WAL/SHM/lock symlinks or Windows reparse points,
   non-regular files, unexpected hard links, wrong ownership, unsafe modes/DACLs,
   insecure parents, and detected identity replacement fail closed within the
   stated local single-user trust boundary.
4. Newly created WAL/SHM files are post-validated and owner-only/private under
   the active platform profile.
5. Concurrent initialization/open, application-lock timeout, crash release, and
   SQLite busy timeout behavior is deterministic and bounded on POSIX and native
   Windows.
6. Unsupported pre-release schema versions are rejected without mutation; future crash and migration tests never advertise a partially migrated schema.
7. External network, system-keyring, and large filesystem work is absent from
   SQLite transaction scopes; Linux secret insertion and binding activation are
   atomic in one bounded transaction.
8. UIDVALIDITY invalidation and incomplete coverage are transactionally safe.
9. Retention and rebuild alter projection only and preserve every authoritative
   catalog and binding row.
10. Corrupt, incompatible, busy, insecure, and unsupported-platform states
    produce bounded typed errors with no automatic legacy fallback, partial
    parent creation, or destructive repair.
11. Native Windows NTFS tests cover symlinks, junctions, arbitrary reparse
    rejection where constructible, foreign/permissive DACLs, hard links, held
    identity, lock contention and killed-owner release, concurrent replacement,
    pre/post-replace process termination, complete-old-or-new atomicity, and
    bounded stale-artifact cleanup including substitution attempts.
12. Windows UNC, remote/non-NTFS, device namespace, and alternate-stream inputs
    fail before authority, provider, or secret-store effects; Windows writers
    use flush plus same-volume write-through replacement and never enable copy
    fallback.
