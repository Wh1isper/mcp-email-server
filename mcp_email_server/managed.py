from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import time
import unicodedata
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import NoReturn, Protocol

from pydantic import SecretStr

from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.management import (
    AccountDetails,
    AccountRemovalResult,
    AccountSummary,
    BindingRole,
    CredentialCleanupReport,
    CredentialMutationResult,
    CredentialRemovalResult,
    DoctorReport,
    EndpointSummary,
    IndexHealth,
    ManagedPolicy,
    ManagementError,
    RevisionConflictError,
)
from mcp_email_server.config import EmailServer, EmailSettings, Settings
from mcp_email_server.imap_keywords import ImapKeywordAccount, ImapKeywordTag
from mcp_email_server.log import logger
from mcp_email_server.windows_security import (
    WindowsFileIdentity,
    WindowsSecurityError,
    create_private_file,
    harden_private_file,
    secure_file_lock,
    validate_private_directory,
    validate_private_file,
    windows_security_supported,
)

SCHEMA_VERSION = 4
_MIGRATION_SOURCE_VERSION = 3
SQLITE_BUSY_TIMEOUT_MS = 5_000
WAL_RETRY_BUSY_TIMEOUT_MS = 100
MANAGED_KEYRING_SERVICE = "mcp-email-server-managed"
MAX_CREDENTIAL_CLEANUP_ROWS = APPLICATION_LIMITS.credential_cleanup_rows


def _normalize_account_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).strip().casefold()
    if not normalized:
        raise ManagedCatalogError("Account name must not be empty")
    return normalized


class ManagedCatalogError(ManagementError):
    """A managed catalog operation failed without exposing sensitive internals."""


class ManagedSecretStoreUnavailableError(ManagedCatalogError):
    """The selected secret-store backend is unavailable for a credential write."""


class ManagedCatalogInitializationConflictError(ManagedCatalogError):
    """An existing initialization target is not a compatible managed catalog."""


class _ManagedCatalogAlreadyExistsError(ManagedCatalogError):
    """Exclusive catalog creation found an existing regular-file target."""


class ManagedRevisionConflictError(ManagedCatalogError, RevisionConflictError):
    """A typed optimistic conflict raised by the managed catalog adapter."""


class ManagedCatalogSecurityError(ManagedCatalogError):
    """A managed catalog path does not meet local security requirements."""


@dataclass(frozen=True, repr=False)
class _ManagedAuthoritySnapshot:
    """Internal comparable authority; opaque locators must never be represented."""

    catalog_revision: int
    policy: ManagedPolicy
    account_id: str
    account_revision: int
    name: str
    full_name: str
    email_address: str
    save_to_sent: bool
    sent_folder_name: str | None
    tags: tuple[ImapKeywordTag, ...]
    incoming: EndpointSummary
    outgoing: EndpointSummary | None
    binding_ids: tuple[tuple[BindingRole, str], ...]
    binding_locators: tuple[tuple[BindingRole, str], ...]


@dataclass(frozen=True)
class ManagedAccountResolution:
    """Secret-safe public result from a revisioned selected-account resolution."""

    account: EmailSettings
    policy: ManagedPolicy


class ManagedSecretStore(Protocol):
    """Internal store for opaque managed credential values."""

    def put(self, locator: str, value: str) -> None: ...

    def get(self, locator: str) -> str: ...

    def delete(self, locator: str) -> bool: ...


class ManagedKeyringSecretStore:
    """Operating-system keyring store for managed credentials."""

    def put(self, locator: str, value: str) -> None:
        import keyring
        from keyring.errors import InitError, KeyringLocked, NoKeyringError

        unavailable_errors = (InitError, KeyringLocked, NoKeyringError)
        try:
            existing = keyring.get_password(MANAGED_KEYRING_SERVICE, locator)
        except unavailable_errors as exc:
            raise ManagedSecretStoreUnavailableError(
                "Managed secret backend is locked or unavailable",
                reason="credential_store_unavailable",
            ) from exc
        except Exception as exc:
            raise ManagedCatalogError(
                "Managed secret backend rejected the credential",
                reason="credential_store_unavailable",
            ) from exc
        if existing is not None:
            raise ManagedCatalogError("Secret candidate already exists")
        try:
            keyring.set_password(MANAGED_KEYRING_SERVICE, locator, value)
        except unavailable_errors as exc:
            raise ManagedSecretStoreUnavailableError(
                "Managed secret backend is locked or unavailable",
                reason="credential_store_unavailable",
            ) from exc
        except Exception as exc:
            raise ManagedCatalogError(
                "Managed secret backend rejected the credential",
                reason="credential_store_unavailable",
            ) from exc

    def get(self, locator: str) -> str:
        import keyring

        try:
            value = keyring.get_password(MANAGED_KEYRING_SERVICE, locator)
        except Exception as exc:
            raise ManagedCatalogError(
                "Managed secret backend is unavailable",
                reason="credential_store_unavailable",
            ) from exc
        if value is None:
            raise ManagedCatalogError("An active managed credential is missing")
        return value

    def delete(self, locator: str) -> bool:
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.delete_password(MANAGED_KEYRING_SERVICE, locator)
        except KeyringError:
            try:
                return keyring.get_password(MANAGED_KEYRING_SERVICE, locator) is None
            except Exception:
                return False
        except Exception:
            return False
        return True


class ManagedSqliteSecretStore:
    """Private SQLite secret store used by managed catalogs on Linux and Windows."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def put_in_transaction(connection: sqlite3.Connection, locator: str, value: str) -> None:
        """Insert a secret through the catalog's active SQLite transaction."""
        try:
            connection.execute(
                "INSERT INTO managed_secret(locator, secret_value) VALUES (?, ?)",
                (locator, value),
            )
        except sqlite3.Error as exc:
            raise ManagedCatalogError(
                "Managed secret backend rejected the credential",
                reason="credential_store_unavailable",
            ) from exc

    def put(self, locator: str, value: str) -> None:
        try:
            with _connect(self.path) as connection:
                self.put_in_transaction(connection, locator, value)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ManagedCatalogError("Secret locator already exists") from exc
        except sqlite3.Error as exc:
            raise ManagedCatalogError(
                "Managed secret backend rejected the credential",
                reason="credential_store_unavailable",
            ) from exc

    def get(self, locator: str) -> str:
        try:
            with _connect(self.path) as connection:
                row = connection.execute(
                    "SELECT secret_value FROM managed_secret WHERE locator = ?",
                    (locator,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ManagedCatalogError(
                "Managed secret backend is unavailable",
                reason="credential_store_unavailable",
            ) from exc
        if row is None:
            raise ManagedCatalogError("An active managed credential is missing")
        return row["secret_value"]

    def delete(self, locator: str) -> bool:
        try:
            with _connect(self.path) as connection:
                connection.execute("DELETE FROM managed_secret WHERE locator = ?", (locator,))
                connection.commit()
        except (ManagedCatalogError, sqlite3.Error):
            return False
        return True


_SECURE_CATALOG_FILES_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "getuid")
    and importlib.util.find_spec("fcntl") is not None
) or (os.name == "nt" and windows_security_supported())

SecurityMetadata = os.stat_result | WindowsFileIdentity


def _identity_key(metadata: SecurityMetadata) -> tuple[int, int]:
    if isinstance(metadata, WindowsFileIdentity):  # pragma: no cover - native Windows CI
        return metadata.key
    return (metadata.st_dev, metadata.st_ino)


def _require_secure_catalog_files() -> None:
    if not _SECURE_CATALOG_FILES_SUPPORTED:
        raise ManagedCatalogSecurityError(
            "Managed catalogs are unavailable because this platform cannot enforce owner-only no-follow files"
        )


def _assert_private_directory(path: Path) -> None:
    _require_secure_catalog_files()
    if os.name == "nt":  # pragma: no cover - native Windows CI
        try:
            validate_private_directory(path)
        except WindowsSecurityError as exc:
            raise ManagedCatalogSecurityError(
                "Managed catalog parent does not satisfy the Windows private-storage policy"
            ) from exc
        return
    current_uid = os.getuid()
    for index, candidate in enumerate((path, *path.parents)):
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise ManagedCatalogSecurityError("Managed catalog parent chain could not be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ManagedCatalogSecurityError("Managed catalog parent chain must contain real directories")
        mode = stat.S_IMODE(metadata.st_mode)
        if index == 0:
            if metadata.st_uid != current_uid or mode & 0o077:
                raise ManagedCatalogSecurityError("Managed catalog parent must be owner-only")
        elif metadata.st_uid not in {0, current_uid} or (mode & 0o022 and not metadata.st_mode & stat.S_ISVTX):
            raise ManagedCatalogSecurityError("Managed catalog ancestor permissions are unsafe")


def _assert_private_file(
    path: Path,
    *,
    label: str = "Managed catalog file",
    allow_disappearing: bool = False,
) -> SecurityMetadata:
    if os.name == "nt":  # pragma: no cover - native Windows CI
        try:
            return validate_private_file(path)
        except FileNotFoundError:
            raise
        except WindowsSecurityError as exc:
            raise ManagedCatalogSecurityError(f"{label} does not satisfy the Windows private-file policy") from exc
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        # WAL/SHM files may disappear when the final SQLite connection closes.
        # Callers that require existence handle this separately.
        raise
    except OSError as exc:
        raise ManagedCatalogSecurityError(f"{label} could not be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ManagedCatalogSecurityError(f"{label} must be a regular file and not a symlink")
    _require_secure_catalog_files()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ManagedCatalogSecurityError(f"{label} must be owner-only")
    if metadata.st_nlink == 0 and allow_disappearing:
        raise FileNotFoundError(path)
    if metadata.st_nlink != 1:
        raise ManagedCatalogSecurityError(f"{label} must not have additional hard links")
    return metadata


def _prepare_new_file(path: Path) -> None:
    _require_secure_catalog_files()
    if os.name == "nt":  # pragma: no cover - native Windows CI
        try:
            create_private_file(path)
        except FileExistsError as exc:
            raise _ManagedCatalogAlreadyExistsError("Managed catalog already exists") from exc
        except WindowsSecurityError as exc:
            raise ManagedCatalogSecurityError("Managed catalog could not be created safely on Windows") from exc
        return
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_private_directory(parent)
    if path.is_symlink():
        raise ManagedCatalogSecurityError("Managed catalog must not be a symlink")
    if path.exists():
        raise _ManagedCatalogAlreadyExistsError("Managed catalog already exists")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise _ManagedCatalogAlreadyExistsError("Managed catalog already exists") from exc
    os.close(fd)


def _validate_sidecars(path: Path) -> dict[str, tuple[int, int]]:
    identities: dict[str, tuple[int, int]] = {}
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        try:
            metadata = _assert_private_file(
                sidecar,
                label="Managed catalog sidecar",
                allow_disappearing=True,
            )
        except FileNotFoundError:
            continue
        else:
            identities[suffix] = _identity_key(metadata)
    return identities


def _prevalidate_sidecars_before_lock(path: Path) -> None:
    # SQLite creates inherited-DACL sidecars before this adapter hardens them.
    # On Windows, another local connection must wait on the setup lock rather
    # than reject that bounded intermediate state outside the lock.
    if os.name != "nt":  # pragma: no branch - native Windows CI
        _validate_sidecars(path)


def _harden_windows_sidecars(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - native Windows CI
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            try:
                harden_private_file(sidecar)
            except FileNotFoundError:
                continue
            except WindowsSecurityError as exc:
                raise ManagedCatalogSecurityError(
                    "New managed catalog sidecar could not be secured on Windows"
                ) from exc


def _reconcile_windows_sidecars(path: Path) -> None:
    """Best-effort post-commit reconciliation; the next open still fails closed."""
    if os.name == "nt":  # pragma: no cover - native Windows CI
        try:
            with _application_path_lock(path):
                _harden_windows_sidecars(path)
                _validate_sidecars(path)
        except ManagedCatalogError:
            # The transaction may already be committed. Do not invite an unsafe
            # retry by reporting a lock/reconciliation race as an operation failure.
            # Every subsequent open repeats strict pre-validation before SQLite.
            logger.warning("Deferred Windows managed-catalog sidecar reconciliation")


def _validate_existing_path(path: Path) -> SecurityMetadata:
    _assert_private_directory(path.parent)
    return _assert_private_file(path)


def _lock_path(path: Path) -> Path:
    return Path(f"{path}.lock")


@contextlib.contextmanager
def _application_path_lock(path: Path) -> Iterator[None]:
    """Serialize security-sensitive open/setup without following the lock path."""
    lock_path = _lock_path(path)
    if os.name == "nt":  # pragma: no cover - native Windows CI
        try:
            with secure_file_lock(lock_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1_000):
                yield
        except WindowsSecurityError as exc:
            if "busy" in str(exc).lower():
                raise ManagedCatalogError("Managed catalog lock is busy") from exc
            raise ManagedCatalogSecurityError("Managed catalog lock could not be opened safely") from exc
        return
    if lock_path.exists() or lock_path.is_symlink():
        _assert_private_file(lock_path, label="Managed catalog lock")
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ManagedCatalogSecurityError("Managed catalog lock could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        checked = _assert_private_file(lock_path, label="Managed catalog lock")
        if (opened.st_dev, opened.st_ino) != _identity_key(checked):
            raise ManagedCatalogSecurityError("Managed catalog lock changed while it was opened")
        import fcntl

        deadline = time.monotonic() + SQLITE_BUSY_TIMEOUT_MS / 1_000
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ManagedCatalogError("Managed catalog lock is busy") from exc
                time.sleep(0.05)
        yield
    finally:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _enable_wal(connection: sqlite3.Connection) -> None:
    deadline = time.monotonic() + SQLITE_BUSY_TIMEOUT_MS / 1_000
    connection.execute(f"PRAGMA busy_timeout = {WAL_RETRY_BUSY_TIMEOUT_MS}")
    try:
        while True:
            try:
                current = connection.execute("PRAGMA journal_mode").fetchone()[0]
                if str(current).lower() == "wal":
                    return
                changed = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(changed).lower() == "wal":
                    return
                raise ManagedCatalogError("Managed catalog could not enable WAL mode")
            except sqlite3.OperationalError as exc:
                remaining = deadline - time.monotonic()
                if "locked" not in str(exc).lower() or remaining <= 0:
                    raise
                time.sleep(min(0.1, remaining))
    finally:
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


@contextlib.contextmanager
def _connect(
    path: Path,
    *,
    require_exists: bool = True,
    enable_wal: bool = True,
) -> Iterator[sqlite3.Connection]:
    path = Path(os.path.abspath(path.expanduser()))
    if require_exists:
        try:
            _validate_existing_path(path)
        except FileNotFoundError as exc:
            raise ManagedCatalogError("Selected managed catalog is missing") from exc

    connection: sqlite3.Connection | None = None
    sidecar_setup_started = False
    try:
        # Existing DB, WAL, SHM, and lock entries are inspected before SQLite is
        # allowed to open or enable WAL. The lock closes concurrent app setup races.
        _assert_private_directory(path.parent)
        _prevalidate_sidecars_before_lock(path)
        with _application_path_lock(path):
            before = _validate_existing_path(path)
            _validate_sidecars(path)
            sidecar_setup_started = True
            try:
                connection = sqlite3.connect(path, timeout=5.0)
            except sqlite3.Error as exc:
                raise ManagedCatalogError("Could not open managed catalog") from exc
            after = _assert_private_file(path)
            if _identity_key(before) != _identity_key(after):
                raise ManagedCatalogSecurityError("Managed catalog changed while it was being opened")
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            if enable_wal:
                _enable_wal(connection)
                _harden_windows_sidecars(path)
                _validate_sidecars(path)
            final = _assert_private_file(path)
            if _identity_key(before) != _identity_key(final):
                raise ManagedCatalogSecurityError("Managed catalog changed during SQLite setup")
        yield connection
    except sqlite3.DatabaseError as exc:
        raise ManagedCatalogError("Managed catalog is corrupt or unavailable") from exc
    finally:
        if connection is not None:
            connection.close()
        if enable_wal and sidecar_setup_started:
            # WAL/SHM may be created during the transaction after setup. Reconcile
            # on Windows without turning an already committed mutation into a
            # retryable-looking failure; the next open still prevalidates strictly.
            _reconcile_windows_sidecars(path)


_AUTHORITY_SCHEMA = """
CREATE TABLE schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
CREATE TABLE catalog (
    id TEXT PRIMARY KEY CHECK (id = 'local'),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    enable_attachment_download INTEGER NOT NULL CHECK (enable_attachment_download IN (0, 1)),
    allowed_recipients_json TEXT NOT NULL,
    allowed_senders_json TEXT NOT NULL,
    report_blocked_mutations INTEGER NOT NULL CHECK (report_blocked_mutations IN (0, 1))
);
CREATE TABLE managed_account (
    id TEXT PRIMARY KEY,
    catalog_id TEXT NOT NULL REFERENCES catalog(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    email_address TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    save_to_sent INTEGER NOT NULL CHECK (save_to_sent IN (0, 1)),
    sent_folder_name TEXT,
    removed_at TEXT,
    UNIQUE (catalog_id, normalized_name)
);
CREATE TABLE endpoint (
    account_id TEXT NOT NULL REFERENCES managed_account(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('incoming', 'outgoing')),
    host TEXT NOT NULL,
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    use_ssl INTEGER NOT NULL CHECK (use_ssl IN (0, 1)),
    start_ssl INTEGER NOT NULL CHECK (start_ssl IN (0, 1)),
    verify_ssl INTEGER NOT NULL CHECK (verify_ssl IN (0, 1)),
    user_name TEXT NOT NULL,
    PRIMARY KEY (account_id, role),
    CHECK (NOT (use_ssl = 1 AND start_ssl = 1))
);
CREATE TABLE secret_binding (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES managed_account(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('incoming', 'outgoing')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'CLEANUP_REQUIRED')),
    opaque_locator TEXT NOT NULL UNIQUE,
    supersedes_id TEXT REFERENCES secret_binding(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX one_active_binding_per_role
    ON secret_binding(account_id, role) WHERE status = 'ACTIVE';
CREATE TABLE managed_secret (
    locator TEXT PRIMARY KEY,
    secret_value TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_OPERATIONAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS operational_account (
    id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('managed', 'legacy')),
    source_reference TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_source (
    source_fingerprint TEXT PRIMARY KEY,
    operational_account_id TEXT NOT NULL UNIQUE REFERENCES operational_account(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS mailbox_projection (
    id TEXT PRIMARY KEY,
    operational_account_id TEXT NOT NULL REFERENCES operational_account(id) ON DELETE CASCADE,
    remote_name TEXT NOT NULL,
    delimiter TEXT,
    attributes_json TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL CHECK (uidvalidity > 0),
    observed_at TEXT NOT NULL,
    UNIQUE (operational_account_id, remote_name)
);
CREATE TABLE IF NOT EXISTS message_metadata_projection (
    mailbox_id TEXT NOT NULL REFERENCES mailbox_projection(id) ON DELETE CASCADE,
    uidvalidity INTEGER NOT NULL CHECK (uidvalidity > 0),
    uid INTEGER NOT NULL CHECK (uid > 0),
    message_id TEXT,
    subject TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipients_json TEXT NOT NULL,
    message_date TEXT NOT NULL,
    internal_date TEXT,
    attachment_names_json TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (mailbox_id, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS index_coverage (
    mailbox_id TEXT PRIMARY KEY REFERENCES mailbox_projection(id) ON DELETE CASCADE,
    uidvalidity INTEGER NOT NULL CHECK (uidvalidity > 0),
    uidnext INTEGER NOT NULL CHECK (uidnext > 0),
    message_count INTEGER NOT NULL CHECK (message_count >= 0),
    low_uid INTEGER,
    high_uid INTEGER,
    completeness TEXT NOT NULL CHECK (completeness IN ('PARTIAL', 'COMPLETE')),
    observed_at TEXT NOT NULL,
    CHECK ((low_uid IS NULL AND high_uid IS NULL) OR (low_uid > 0 AND high_uid >= low_uid))
);
CREATE INDEX IF NOT EXISTS metadata_projection_uid_desc
    ON message_metadata_projection(mailbox_id, uidvalidity, uid DESC);
"""

_SCHEMA_V4_ADDITIONS = """
ALTER TABLE catalog
    ADD COLUMN enable_attachment_content INTEGER NOT NULL DEFAULT 0
        CHECK (enable_attachment_content IN (0, 1));
ALTER TABLE managed_account
    ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';
"""

_SCHEMA_V3 = _AUTHORITY_SCHEMA + _OPERATIONAL_SCHEMA
_SCHEMA = _AUTHORITY_SCHEMA + _SCHEMA_V4_ADDITIONS + _OPERATIONAL_SCHEMA
_OPERATIONAL_DATABASE_SCHEMA = (
    """
CREATE TABLE schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
"""
    + _OPERATIONAL_SCHEMA
)


def _execute_schema(connection: sqlite3.Connection, schema: str) -> None:
    statement = ""
    for line in schema.splitlines():
        statement = f"{statement}\n{line}" if statement else line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ManagedCatalogError("Managed catalog schema definition is incomplete")


def _normalized_schema_sql(value: str) -> str:
    return " ".join(value.split())


def _schema_objects(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        """SELECT type, name, sql FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
           ORDER BY type, name"""
    )
    return {(row["type"], row["name"]): _normalized_schema_sql(row["sql"]) for row in rows}


@cache
def _expected_schema_objects(schema: str) -> dict[tuple[str, str], str]:
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(schema)
        return _schema_objects(connection)


def _validate_operational_schema(connection: sqlite3.Connection) -> None:
    expected = _expected_schema_objects(_OPERATIONAL_DATABASE_SCHEMA)
    if _schema_objects(connection) != expected:
        raise ManagedCatalogError("Operational database schema is incomplete or incompatible")


def _validate_managed_schema(connection: sqlite3.Connection, schema: str) -> None:
    if _schema_objects(connection) != _expected_schema_objects(schema):
        raise ManagedCatalogError("Managed catalog schema is missing or incompatible")


def _read_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT version FROM schema_metadata WHERE singleton = 1").fetchone()
    if row is None or not isinstance(row["version"], int):
        raise ManagedCatalogError("Managed catalog schema version is unsupported")
    return row["version"]


def _require_migration_source(version: int) -> None:
    if version != _MIGRATION_SOURCE_VERSION:
        raise ManagedCatalogError("Managed catalog schema version is unsupported")


def _require_migration_update(cursor: sqlite3.Cursor) -> None:
    if cursor.rowcount != 1:
        raise ManagedCatalogError("Managed catalog schema changed during migration")


def _parse_tags_json(value: object) -> tuple[ImapKeywordTag, ...]:
    if not isinstance(value, str):
        raise ManagedCatalogError("Managed account tag data is invalid")
    try:
        return ImapKeywordAccount.model_validate({"tags": json.loads(value)}).tags
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ManagedCatalogError("Managed account tag data is invalid") from exc


def _validate_managed_invariants(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ManagedCatalogError("Managed catalog contains invalid references")
    catalogs = connection.execute(
        """SELECT id, revision, enable_attachment_download, enable_attachment_content,
                  allowed_recipients_json, allowed_senders_json, report_blocked_mutations
             FROM catalog"""
    ).fetchall()
    if len(catalogs) != 1 or catalogs[0]["id"] != "local":
        raise ManagedCatalogError("Managed catalog authority row is invalid")
    catalog = catalogs[0]
    if not isinstance(catalog["revision"], int):
        raise ManagedCatalogError("Managed catalog authority state is invalid")
    if catalog["revision"] < 1:
        raise ManagedCatalogError("Managed catalog authority revision is invalid")
    for field in ("allowed_recipients_json", "allowed_senders_json"):
        try:
            values = json.loads(catalog[field])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ManagedCatalogError("Managed catalog policy data is invalid") from exc
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ManagedCatalogError("Managed catalog policy data is invalid")
    accounts = connection.execute("SELECT name, normalized_name, tags_json FROM managed_account").fetchall()
    if any(row["normalized_name"] != _normalize_account_name(row["name"]) for row in accounts):
        raise ManagedCatalogError("Managed account identity data is invalid")
    for row in accounts:
        _parse_tags_json(row["tags_json"])


class ManagedCatalog:
    def __init__(self, path: Path, secret_store: ManagedSecretStore | None = None) -> None:
        self.path = Path(os.path.abspath(path.expanduser()))
        if secret_store is not None:
            self.secret_store = secret_store
        elif sys.platform.startswith("linux") or sys.platform == "win32":
            self.secret_store = ManagedSqliteSecretStore(self.path)
        else:
            self.secret_store = ManagedKeyringSecretStore()

    @classmethod
    def initialize(cls, path: Path) -> ManagedCatalog:
        normalized = Path(os.path.abspath(path.expanduser()))
        try:
            _prepare_new_file(normalized)
        except _ManagedCatalogAlreadyExistsError:
            existing = cls(normalized)
            try:
                existing.catalog_revision()
            except ManagedCatalogSecurityError:
                raise
            except ManagedCatalogError as exc:
                raise ManagedCatalogInitializationConflictError(
                    "Existing target is not a compatible managed catalog"
                ) from exc
            return existing
        try:
            with _connect(normalized) as connection:
                connection.executescript(_SCHEMA)
                connection.execute("INSERT INTO schema_metadata(singleton, version) VALUES (1, ?)", (SCHEMA_VERSION,))
                connection.execute(
                    """INSERT INTO catalog(
                           id, revision, enable_attachment_download, enable_attachment_content,
                           allowed_recipients_json, allowed_senders_json, report_blocked_mutations
                       ) VALUES ('local', 1, 0, 0, '[]', '[]', 0)"""
                )
                connection.commit()
        except Exception:
            with contextlib.suppress(OSError):
                normalized.unlink()
            raise
        return cls(normalized)

    def _preflight_schema_ownership(self, connection: sqlite3.Connection) -> int:
        try:
            version = _read_schema_version(connection)
        except sqlite3.Error as exc:
            raise ManagedCatalogError("Managed catalog is corrupt or schema is missing or incompatible") from exc
        if version == SCHEMA_VERSION:
            _validate_managed_schema(connection, _SCHEMA)
        elif version == _MIGRATION_SOURCE_VERSION:
            _validate_managed_schema(connection, _SCHEMA_V3)
        else:
            raise ManagedCatalogError("Managed catalog schema version is unsupported")
        return version

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = _read_schema_version(connection)
            if version == SCHEMA_VERSION:
                _validate_managed_schema(connection, _SCHEMA)
                _validate_managed_invariants(connection)
                connection.commit()
                return
            _require_migration_source(version)
            _validate_managed_schema(connection, _SCHEMA_V3)
            _execute_schema(connection, _SCHEMA_V4_ADDITIONS)
            _validate_managed_schema(connection, _SCHEMA)
            _validate_managed_invariants(connection)
            updated = connection.execute(
                "UPDATE schema_metadata SET version = ? WHERE singleton = 1 AND version = ?",
                (SCHEMA_VERSION, _MIGRATION_SOURCE_VERSION),
            )
            _require_migration_update(updated)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with _connect(self.path, enable_wal=False) as connection:
            version = self._preflight_schema_ownership(connection)
        with _connect(self.path) as connection:
            if version == _MIGRATION_SOURCE_VERSION:
                self._migrate_v3_to_v4(connection)
            self._validate_schema(connection)
            yield connection

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute("SELECT version FROM schema_metadata WHERE singleton = 1").fetchone()
        except sqlite3.Error as exc:
            raise ManagedCatalogError("Managed catalog schema is missing or incompatible") from exc
        if row is None or row["version"] != SCHEMA_VERSION:
            raise ManagedCatalogError("Managed catalog schema version is unsupported")
        _validate_managed_schema(connection, _SCHEMA)
        _validate_managed_invariants(connection)

    def catalog_revision(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT revision FROM catalog WHERE id = 'local'").fetchone()
        if row is None or not isinstance(row["revision"], int) or row["revision"] < 1:
            raise ManagedCatalogError("Managed catalog revision is invalid")
        return row["revision"]

    @contextlib.contextmanager
    def import_cutover_guard(
        self,
        *,
        expected_catalog_revision: int,
        expected_account_revisions: tuple[tuple[str, int, bool], ...],
    ) -> Iterator[None]:
        """Fence managed writers while final reviewed import state is selected."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                catalog = connection.execute("SELECT revision FROM catalog WHERE id = 'local'").fetchone()
                if catalog is None or catalog["revision"] != expected_catalog_revision:
                    raise ManagedRevisionConflictError("catalog")
                for name, expected_revision, expected_enabled in expected_account_revisions:
                    account = connection.execute(
                        """SELECT revision, enabled, removed_at FROM managed_account
                           WHERE normalized_name = ?""",
                        (_normalize_account_name(name),),
                    ).fetchone()
                    if (
                        account is None
                        or account["revision"] != expected_revision
                        or bool(account["enabled"]) is not expected_enabled
                        or account["removed_at"] is not None
                    ):
                        raise ManagedRevisionConflictError("account", name=name)
                yield
            finally:
                if connection.in_transaction:
                    connection.rollback()

    @staticmethod
    def _policy_from_row(row: sqlite3.Row) -> ManagedPolicy:
        try:
            allowed_recipients = json.loads(row["allowed_recipients_json"])
            allowed_senders = json.loads(row["allowed_senders_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ManagedCatalogError("Managed policy data is invalid") from exc
        if not isinstance(allowed_recipients, list) or not all(isinstance(item, str) for item in allowed_recipients):
            raise ManagedCatalogError("Managed recipient policy is invalid")
        if not isinstance(allowed_senders, list) or not all(isinstance(item, str) for item in allowed_senders):
            raise ManagedCatalogError("Managed sender policy is invalid")
        if len(allowed_recipients) > APPLICATION_LIMITS.policy_entries:
            raise ManagedCatalogError("Managed recipient policy exceeds the application limit")
        if len(allowed_senders) > APPLICATION_LIMITS.policy_entries:
            raise ManagedCatalogError("Managed sender policy exceeds the application limit")
        return ManagedPolicy(
            revision=row["revision"],
            enable_attachment_download=bool(row["enable_attachment_download"]),
            enable_attachment_content=bool(row["enable_attachment_content"]),
            allowed_recipients=tuple(allowed_recipients),
            allowed_senders=tuple(allowed_senders),
            report_blocked_mutations=bool(row["report_blocked_mutations"]),
        )

    def policy(self) -> ManagedPolicy:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM catalog WHERE id = 'local'").fetchone()
        if row is None:
            raise ManagedCatalogError("Managed catalog row is missing")
        return self._policy_from_row(row)

    def update_policy(
        self,
        *,
        expected_revision: int,
        enable_attachment_download: bool,
        allowed_recipients: tuple[str, ...],
        allowed_senders: tuple[str, ...],
        report_blocked_mutations: bool,
        enable_attachment_content: bool = False,
    ) -> int:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected catalog revision must be positive")
        if any(not item.strip() for item in (*allowed_recipients, *allowed_senders)):
            raise ManagedCatalogError("Managed policy entries must not be empty")
        if len(allowed_recipients) > APPLICATION_LIMITS.policy_entries:
            raise ManagedCatalogError("Managed recipient policy exceeds the application limit")
        if len(allowed_senders) > APPLICATION_LIMITS.policy_entries:
            raise ManagedCatalogError("Managed sender policy exceeds the application limit")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE catalog SET
                       revision = revision + 1,
                       enable_attachment_download = ?,
                       enable_attachment_content = ?,
                       allowed_recipients_json = ?,
                       allowed_senders_json = ?,
                       report_blocked_mutations = ?
                   WHERE id = 'local' AND revision = ?""",
                (
                    int(enable_attachment_download),
                    int(enable_attachment_content),
                    json.dumps(list(allowed_recipients)),
                    json.dumps(list(allowed_senders)),
                    int(report_blocked_mutations),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ManagedRevisionConflictError("catalog")
            connection.commit()
        return expected_revision + 1

    def add_account(
        self,
        *,
        name: str,
        full_name: str,
        email_address: str,
        incoming: EmailServer | EndpointSummary,
        outgoing: EmailServer | EndpointSummary | None,
        save_to_sent: bool = True,
        sent_folder_name: str | None = None,
        tags: tuple[ImapKeywordTag, ...] = (),
        expected_revision: int | None = None,
    ) -> str:
        if not name.strip() or not full_name.strip() or not email_address.strip():
            raise ManagedCatalogError("Account name, full name, and email address are required")
        account_id = uuid.uuid4().hex
        normalized_name = _normalize_account_name(name)
        validated_tags = ImapKeywordAccount(tags=tags).tags
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                catalog = connection.execute("SELECT revision FROM catalog WHERE id = 'local'").fetchone()
                if catalog is None:
                    connection.rollback()
                    raise ManagedCatalogError("Managed catalog row is missing")
                if expected_revision is not None and catalog["revision"] != expected_revision:
                    connection.rollback()
                    raise ManagedRevisionConflictError("catalog")
                account_count = connection.execute(
                    "SELECT COUNT(*) count FROM managed_account WHERE removed_at IS NULL"
                ).fetchone()["count"]
                if account_count >= APPLICATION_LIMITS.configured_accounts:
                    connection.rollback()
                    raise ManagedCatalogError(
                        "Managed account count exceeds the application limit",
                        reason="account_limit_reached",
                    )
                existing = connection.execute(
                    "SELECT 1 FROM managed_account WHERE normalized_name = ?",
                    (normalized_name,),
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    raise ManagedCatalogError(
                        "Account name already exists",
                        reason="account_name_exists",
                    )
                connection.execute(
                    """INSERT INTO managed_account(
                           id, catalog_id, name, normalized_name, full_name, email_address,
                           enabled, revision, save_to_sent, sent_folder_name, tags_json, removed_at
                       ) VALUES (?, 'local', ?, ?, ?, ?, 1, 1, ?, ?, ?, NULL)""",
                    (
                        account_id,
                        name.strip(),
                        normalized_name,
                        full_name,
                        email_address,
                        int(save_to_sent),
                        sent_folder_name,
                        json.dumps([tag.model_dump(mode="json") for tag in validated_tags]),
                    ),
                )
                self._insert_endpoint(connection, account_id, "incoming", incoming)
                if outgoing is not None:
                    self._insert_endpoint(connection, account_id, "outgoing", outgoing)
                cursor = connection.execute(
                    "UPDATE catalog SET revision = revision + 1 WHERE id = 'local' AND revision = ?",
                    (catalog["revision"],),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise ManagedRevisionConflictError("catalog")
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ManagedCatalogError("Account or endpoint settings are invalid") from exc
        return account_id

    @staticmethod
    def _insert_endpoint(
        connection: sqlite3.Connection,
        account_id: str,
        role: BindingRole,
        endpoint: EmailServer | EndpointSummary,
    ) -> None:
        connection.execute(
            """INSERT INTO endpoint(
                   account_id, role, host, port, use_ssl, start_ssl, verify_ssl, user_name
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_id,
                role,
                endpoint.host,
                endpoint.port,
                int(endpoint.use_ssl),
                int(endpoint.start_ssl),
                int(endpoint.verify_ssl),
                endpoint.user_name,
            ),
        )

    def account_revision(self, name: str) -> tuple[str, int]:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT id, revision FROM managed_account
                   WHERE normalized_name = ? AND removed_at IS NULL""",
                (_normalize_account_name(name),),
            ).fetchone()
            if row is None:
                raise ManagedCatalogError("Managed account was not found")
            return row["id"], row["revision"]

    def _discard_secret(self, locator: str) -> None:
        with contextlib.suppress(Exception):
            self.secret_store.delete(locator)

    def _store_verified_secret(self, locator: str, value: str) -> None:
        try:
            self.secret_store.put(locator, value)
            stored_value = self.secret_store.get(locator)
        except Exception:
            self._discard_secret(locator)
            raise
        if stored_value != value:
            self._discard_secret(locator)
            raise ManagedCatalogError(
                "Managed secret backend could not verify the credential",
                reason="credential_store_unavailable",
            )

    @staticmethod
    def _raise_activation_conflict(connection: sqlite3.Connection, account_name: str) -> NoReturn:
        connection.rollback()
        raise ManagedRevisionConflictError("account", name=account_name)

    def _activate_secret_binding(
        self,
        *,
        account_id: str,
        account_name: str,
        role: BindingRole,
        value: str,
        expected_revision: int,
        binding_id: str,
        locator: str,
        local_store: ManagedSqliteSecretStore | None,
    ) -> str | None:
        committed = False
        old_locator: str | None = None
        try:
            with self._connection() as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    raise ManagedCatalogError(
                        "Managed credential storage is unavailable",
                        reason="credential_store_unavailable",
                    ) from exc
                account = connection.execute(
                    """SELECT revision FROM managed_account
                       WHERE id = ? AND removed_at IS NULL""",
                    (account_id,),
                ).fetchone()
                endpoint = connection.execute(
                    "SELECT 1 FROM endpoint WHERE account_id = ? AND role = ?",
                    (account_id, role),
                ).fetchone()
                if account is None or account["revision"] != expected_revision or endpoint is None:
                    self._raise_activation_conflict(connection, account_name)
                if local_store is not None:
                    local_store.put_in_transaction(connection, locator, value)
                old = connection.execute(
                    """SELECT id, opaque_locator FROM secret_binding
                       WHERE account_id = ? AND role = ? AND status = 'ACTIVE'""",
                    (account_id, role),
                ).fetchone()
                if old is not None:
                    old_locator = old["opaque_locator"]
                    connection.execute(
                        "UPDATE secret_binding SET status = 'CLEANUP_REQUIRED' WHERE id = ?",
                        (old["id"],),
                    )
                connection.execute(
                    """INSERT INTO secret_binding(
                           id, account_id, role, status, opaque_locator, supersedes_id
                       ) VALUES (?, ?, ?, 'ACTIVE', ?, ?)""",
                    (binding_id, account_id, role, locator, old["id"] if old is not None else None),
                )
                revised = connection.execute(
                    "UPDATE managed_account SET revision = revision + 1 WHERE id = ? AND revision = ?",
                    (account_id, expected_revision),
                )
                if revised.rowcount != 1:
                    self._raise_activation_conflict(connection, account_name)
                try:
                    connection.commit()
                except Exception:
                    committed = not connection.in_transaction
                    raise
                committed = True
        except Exception:
            if not committed:
                raise
            logger.warning("Credential activation committed but post-commit storage validation failed")
        return old_locator

    def _finalize_replaced_secret(self, old_locator: str | None) -> int:
        if old_locator is None:
            return 0
        try:
            if not self.secret_store.delete(old_locator):
                return 1
            with self._connection() as connection:
                connection.execute(
                    """UPDATE secret_binding SET status = 'SUPERSEDED'
                       WHERE opaque_locator = ? AND status = 'CLEANUP_REQUIRED'""",
                    (old_locator,),
                )
                connection.commit()
        except Exception:
            logger.warning("Credential rotation committed but cleanup finalization requires reconciliation")
            return 1
        return 0

    def _cleanup_required_count(self, account_id: str, role: BindingRole) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM secret_binding
                   WHERE account_id = ? AND role = ? AND status = 'CLEANUP_REQUIRED'""",
                (account_id, role),
            ).fetchone()
        if row is None:
            raise ManagedCatalogError("Managed credential cleanup status is unavailable")
        return row["count"]

    def set_secret(
        self,
        name: str,
        role: BindingRole,
        value: str,
        *,
        expected_revision: int | None = None,
    ) -> CredentialMutationResult:
        """Store and activate a credential without persisting provisional binding state."""
        if not value:
            raise ManagedCatalogError("Credential must not be empty")
        account_id, current_revision = self.account_revision(name)
        if expected_revision is None:
            expected_revision = current_revision
        elif expected_revision < 1 or expected_revision != current_revision:
            raise ManagedRevisionConflictError("account", name=name)

        with self._connection() as connection:
            endpoint = connection.execute(
                "SELECT 1 FROM endpoint WHERE account_id = ? AND role = ?",
                (account_id, role),
            ).fetchone()
        if endpoint is None:
            raise ManagedCatalogError("Credential role has no configured endpoint")

        binding_id = uuid.uuid4().hex
        locator = uuid.uuid4().hex
        local_store = (
            self.secret_store
            if isinstance(self.secret_store, ManagedSqliteSecretStore) and self.secret_store.path == self.path
            else None
        )
        if local_store is None:
            self._store_verified_secret(locator, value)
        try:
            old_locator = self._activate_secret_binding(
                account_id=account_id,
                account_name=name,
                role=role,
                value=value,
                expected_revision=expected_revision,
                binding_id=binding_id,
                locator=locator,
                local_store=local_store,
            )
        except Exception:
            if local_store is None:
                self._discard_secret(locator)
            raise

        cleanup_hint = self._finalize_replaced_secret(old_locator)
        try:
            cleanup_required = max(cleanup_hint, self._cleanup_required_count(account_id, role))
        except Exception:
            logger.warning("Credential activation committed but cleanup status could not be reloaded")
            cleanup_required = max(1, cleanup_hint)
        return CredentialMutationResult(
            "active_cleanup_required" if cleanup_required else "active",
            revision=expected_revision + 1,
            cleanup_required=cleanup_required,
        )

    def list_accounts(self) -> list[AccountSummary]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT a.name, a.email_address, a.enabled, a.revision,
                          EXISTS(SELECT 1 FROM endpoint e WHERE e.account_id = a.id AND e.role = 'outgoing') has_outgoing,
                          (SELECT status FROM secret_binding b WHERE b.account_id = a.id AND b.role = 'incoming'
                           ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END, created_at DESC LIMIT 1) incoming_binding,
                          (SELECT status FROM secret_binding b WHERE b.account_id = a.id AND b.role = 'outgoing'
                           ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END, created_at DESC LIMIT 1) outgoing_binding
                   FROM managed_account a WHERE a.removed_at IS NULL ORDER BY a.name"""
            ).fetchall()
        return [
            AccountSummary(
                name=row["name"],
                email_address=row["email_address"],
                enabled=bool(row["enabled"]),
                revision=row["revision"],
                has_outgoing=bool(row["has_outgoing"]),
                incoming_binding=row["incoming_binding"] or "MISSING",
                outgoing_binding=row["outgoing_binding"],
            )
            for row in rows
        ]

    @staticmethod
    def _endpoint_summary(row: sqlite3.Row) -> EndpointSummary:
        return EndpointSummary(
            host=row["host"],
            port=row["port"],
            use_ssl=bool(row["use_ssl"]),
            start_ssl=bool(row["start_ssl"]),
            verify_ssl=bool(row["verify_ssl"]),
            user_name=row["user_name"],
        )

    def show_account(self, name: str) -> AccountDetails:
        with self._connection() as connection:
            account = connection.execute(
                """SELECT * FROM managed_account
                   WHERE normalized_name = ? AND removed_at IS NULL""",
                (_normalize_account_name(name),),
            ).fetchone()
            if account is None:
                raise ManagedCatalogError("Managed account was not found")
            endpoints = {
                row["role"]: self._endpoint_summary(row)
                for row in connection.execute("SELECT * FROM endpoint WHERE account_id = ?", (account["id"],))
            }
            bindings: dict[str, str] = {}
            for row in connection.execute(
                """SELECT role, status FROM secret_binding
                   WHERE account_id = ?
                   ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END,
                            created_at DESC""",
                (account["id"],),
            ):
                bindings.setdefault(row["role"], row["status"])
        incoming = endpoints.get("incoming")
        if incoming is None:
            raise ManagedCatalogError("Managed account has no incoming endpoint")
        return AccountDetails(
            name=account["name"],
            full_name=account["full_name"],
            email_address=account["email_address"],
            enabled=bool(account["enabled"]),
            revision=account["revision"],
            save_to_sent=bool(account["save_to_sent"]),
            sent_folder_name=account["sent_folder_name"],
            tags=_parse_tags_json(account["tags_json"]),
            incoming=incoming,
            outgoing=endpoints.get("outgoing"),
            incoming_binding=bindings.get("incoming", "MISSING"),
            outgoing_binding=bindings.get("outgoing"),
        )

    def has_removed_account(self, name: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM managed_account
                   WHERE normalized_name = ? AND removed_at IS NOT NULL""",
                (_normalize_account_name(name),),
            ).fetchone()
        return row is not None

    @staticmethod
    def _ensure_account_name_available(connection: sqlite3.Connection, name: str) -> None:
        existing = connection.execute(
            "SELECT 1 FROM managed_account WHERE normalized_name = ?",
            (_normalize_account_name(name),),
        ).fetchone()
        if existing is not None:
            raise ManagedCatalogError(
                "Account name already exists",
                reason="account_name_exists",
            )

    def update_account(
        self,
        name: str,
        *,
        expected_revision: int,
        new_name: str | None = None,
        full_name: str | None = None,
        email_address: str | None = None,
        incoming: EmailServer | EndpointSummary | None = None,
        outgoing: EmailServer | EndpointSummary | None = None,
        remove_outgoing: bool = False,
        save_to_sent: bool | None = None,
        sent_folder_name: str | None = None,
        update_sent_folder: bool = False,
        tags: tuple[ImapKeywordTag, ...] | None = None,
    ) -> int:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected account revision must be positive")
        if new_name is not None and not new_name.strip():
            raise ManagedCatalogError("Account name must not be empty")
        if full_name is not None and not full_name.strip():
            raise ManagedCatalogError("Full name must not be empty")
        if email_address is not None and not email_address.strip():
            raise ManagedCatalogError("Email address must not be empty")
        if outgoing is not None and remove_outgoing:
            raise ManagedCatalogError("Outgoing endpoint update and removal are mutually exclusive")
        validated_tags = ImapKeywordAccount(tags=tags).tags if tags is not None else None
        if not any((
            new_name is not None,
            full_name is not None,
            email_address is not None,
            incoming is not None,
            outgoing is not None,
            remove_outgoing,
            save_to_sent is not None,
            update_sent_folder,
            validated_tags is not None,
        )):
            raise ManagedCatalogError("Account update did not specify any changes")

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if new_name is not None and _normalize_account_name(new_name) != _normalize_account_name(name):
                    self._ensure_account_name_available(connection, new_name)
                self._perform_account_update(
                    connection,
                    name=name,
                    expected_revision=expected_revision,
                    new_name=new_name,
                    full_name=full_name,
                    email_address=email_address,
                    incoming=incoming,
                    outgoing=outgoing,
                    remove_outgoing=remove_outgoing,
                    save_to_sent=save_to_sent,
                    sent_folder_name=sent_folder_name,
                    update_sent_folder=update_sent_folder,
                    tags=validated_tags,
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ManagedCatalogError("Account or endpoint settings are invalid") from exc
            except Exception:
                connection.rollback()
                raise
        return expected_revision + 1

    def _perform_account_update(  # noqa: C901 - one bounded optimistic transaction
        self,
        connection: sqlite3.Connection,
        *,
        name: str,
        expected_revision: int,
        new_name: str | None,
        full_name: str | None,
        email_address: str | None,
        incoming: EmailServer | EndpointSummary | None,
        outgoing: EmailServer | EndpointSummary | None,
        remove_outgoing: bool,
        save_to_sent: bool | None,
        sent_folder_name: str | None,
        update_sent_folder: bool,
        tags: tuple[ImapKeywordTag, ...] | None,
    ) -> None:
        account = connection.execute(
            """SELECT id, enabled, revision FROM managed_account
               WHERE normalized_name = ? AND removed_at IS NULL""",
            (_normalize_account_name(name),),
        ).fetchone()
        if account is None:
            raise ManagedCatalogError("Managed account was not found")
        if account["revision"] != expected_revision:
            raise ManagedRevisionConflictError("account", name=name)
        if remove_outgoing:
            binding = connection.execute(
                """SELECT 1 FROM secret_binding
                   WHERE account_id = ? AND role = 'outgoing' AND status != 'SUPERSEDED'""",
                (account["id"],),
            ).fetchone()
            if binding is not None:
                raise ManagedCatalogError("Remove the outgoing credential before removing its endpoint")
            connection.execute(
                "DELETE FROM endpoint WHERE account_id = ? AND role = 'outgoing'",
                (account["id"],),
            )
        if incoming is not None:
            connection.execute("DELETE FROM endpoint WHERE account_id = ? AND role = 'incoming'", (account["id"],))
            self._insert_endpoint(connection, account["id"], "incoming", incoming)
        if outgoing is not None:
            connection.execute("DELETE FROM endpoint WHERE account_id = ? AND role = 'outgoing'", (account["id"],))
            self._insert_endpoint(connection, account["id"], "outgoing", outgoing)

        assignments = ["revision = revision + 1"]
        values: list[object] = []
        if new_name is not None:
            assignments.extend(("name = ?", "normalized_name = ?"))
            values.extend((new_name.strip(), _normalize_account_name(new_name)))
        for column, value in (
            ("full_name", full_name),
            ("email_address", email_address),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if save_to_sent is not None:
            assignments.append("save_to_sent = ?")
            values.append(int(save_to_sent))
        if update_sent_folder:
            assignments.append("sent_folder_name = ?")
            values.append(sent_folder_name)
        if tags is not None:
            assignments.append("tags_json = ?")
            values.append(json.dumps([tag.model_dump(mode="json") for tag in tags]))
        values.extend((account["id"], expected_revision))
        cursor = connection.execute(
            f"UPDATE managed_account SET {', '.join(assignments)} WHERE id = ? AND revision = ?",  # noqa: S608
            values,
        )
        if cursor.rowcount != 1:
            raise ManagedRevisionConflictError("account", name=name)
        if account["enabled"] and self._account_problems(connection, account["id"]):
            raise ManagedCatalogError("Enabled managed account update would leave incomplete endpoints")
        if incoming is not None:
            connection.execute(
                """DELETE FROM mailbox_projection WHERE operational_account_id IN (
                       SELECT id FROM operational_account WHERE source_reference = ?
                   )""",
                (f"managed:{account['id']}",),
            )

    def disable_account(self, name: str, *, expected_revision: int) -> int:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected account revision must be positive")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE managed_account SET enabled = 0, revision = revision + 1
                   WHERE normalized_name = ? AND removed_at IS NULL AND enabled = 1 AND revision = ?""",
                (_normalize_account_name(name), expected_revision),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ManagedCatalogError("Managed account was not found, already disabled, or revision changed")
            connection.commit()
        return expected_revision + 1

    def _account_problems(self, connection: sqlite3.Connection, account_id: str) -> list[BindingRole]:
        roles = {
            row["role"] for row in connection.execute("SELECT role FROM endpoint WHERE account_id = ?", (account_id,))
        }
        bindings = {
            row["role"]
            for row in connection.execute(
                "SELECT role FROM secret_binding WHERE account_id = ? AND status = 'ACTIVE'",
                (account_id,),
            )
        }
        problems: list[BindingRole] = []
        if "incoming" not in roles or "incoming" not in bindings:
            problems.append("incoming")
        if ("outgoing" in roles) != ("outgoing" in bindings):
            problems.append("outgoing")
        return problems

    def enable_account(self, name: str, *, expected_revision: int) -> int:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected account revision must be positive")
        with self._connection() as connection:
            account = connection.execute(
                """SELECT id, enabled, revision FROM managed_account
                   WHERE normalized_name = ? AND removed_at IS NULL""",
                (_normalize_account_name(name),),
            ).fetchone()
            if account is None or account["enabled"]:
                raise ManagedCatalogError("Managed account was not found or is already enabled")
            if account["revision"] != expected_revision:
                raise ManagedRevisionConflictError("account", name=name)
            if self._account_problems(connection, account["id"]):
                raise ManagedCatalogError("Managed account is incomplete")
            bindings = connection.execute(
                """SELECT role, opaque_locator FROM secret_binding
                   WHERE account_id = ? AND status = 'ACTIVE' ORDER BY role""",
                (account["id"],),
            ).fetchall()

        # Secret backend access must not hold a SQLite write transaction.
        for binding in bindings:
            self.secret_store.get(binding["opaque_locator"])

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT enabled, revision FROM managed_account
                   WHERE id = ? AND removed_at IS NULL""",
                (account["id"],),
            ).fetchone()
            if current is None or current["enabled"] or current["revision"] != expected_revision:
                connection.rollback()
                raise ManagedRevisionConflictError(
                    "account",
                    name=name,
                    message="Managed account changed while credentials were validated; retry",
                )
            if self._account_problems(connection, account["id"]):
                connection.rollback()
                raise ManagedRevisionConflictError(
                    "account",
                    name=name,
                    message="Managed account changed while credentials were validated; retry",
                )
            cursor = connection.execute(
                """UPDATE managed_account SET enabled = 1, revision = revision + 1
                   WHERE id = ? AND enabled = 0 AND revision = ?""",
                (account["id"], expected_revision),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ManagedRevisionConflictError(
                    "account",
                    name=name,
                    message="Managed account changed while credentials were validated; retry",
                )
            connection.commit()
        return expected_revision + 1

    def soft_remove_account(self, name: str, *, expected_revision: int) -> AccountRemovalResult:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected account revision must be positive")
        removed_at = datetime.now(UTC).isoformat(timespec="microseconds")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                """SELECT id, revision FROM managed_account
                   WHERE normalized_name = ? AND removed_at IS NULL""",
                (_normalize_account_name(name),),
            ).fetchone()
            if account is None or account["revision"] != expected_revision:
                connection.rollback()
                raise ManagedRevisionConflictError("account", name=name)
            total_candidates = connection.execute(
                """SELECT COUNT(*) AS count FROM secret_binding
                   WHERE account_id = ? AND status != 'SUPERSEDED'""",
                (account["id"],),
            ).fetchone()["count"]
            candidates = connection.execute(
                """SELECT id, opaque_locator FROM secret_binding
                   WHERE account_id = ? AND status != 'SUPERSEDED'
                   ORDER BY created_at, id LIMIT ?""",
                (account["id"], MAX_CREDENTIAL_CLEANUP_ROWS),
            ).fetchall()
            connection.execute(
                """UPDATE secret_binding SET status = 'CLEANUP_REQUIRED'
                   WHERE account_id = ? AND status != 'SUPERSEDED'""",
                (account["id"],),
            )
            cursor = connection.execute(
                """UPDATE managed_account
                   SET enabled = 0, removed_at = ?, revision = revision + 1
                   WHERE id = ? AND removed_at IS NULL AND revision = ?""",
                (removed_at, account["id"], expected_revision),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ManagedRevisionConflictError("account", name=name)
            connection.commit()

        cleaned_ids: list[str] = []
        for candidate in candidates:
            try:
                if self.secret_store.delete(candidate["opaque_locator"]):
                    cleaned_ids.append(candidate["id"])
            except Exception:
                logger.warning("Soft removal committed but credential cleanup requires reconciliation")
        cleaned = 0
        if cleaned_ids:
            try:
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for binding_id in cleaned_ids:
                        cursor = connection.execute(
                            """UPDATE secret_binding SET status = 'SUPERSEDED'
                               WHERE id = ? AND status = 'CLEANUP_REQUIRED'""",
                            (binding_id,),
                        )
                        cleaned += cursor.rowcount
                    connection.commit()
            except Exception:
                logger.warning("Soft removal committed but cleanup bookkeeping requires reconciliation")
                cleaned = 0
        return AccountRemovalResult(
            revision=expected_revision + 1,
            credentials_examined=len(candidates),
            credentials_cleaned=cleaned,
            cleanup_required=total_candidates - cleaned,
        )

    def remove_secret(self, name: str, role: BindingRole, *, expected_revision: int) -> CredentialRemovalResult:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected account revision must be positive")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                """SELECT id, enabled, revision FROM managed_account
                   WHERE normalized_name = ? AND removed_at IS NULL""",
                (_normalize_account_name(name),),
            ).fetchone()
            if account is None:
                connection.rollback()
                raise ManagedCatalogError("Managed account was not found")
            if account["revision"] != expected_revision:
                connection.rollback()
                raise ManagedRevisionConflictError("account", name=name)
            if account["enabled"]:
                connection.rollback()
                raise ManagedCatalogError("Disable the managed account before removing an active credential")
            binding = connection.execute(
                """SELECT id, opaque_locator FROM secret_binding
                   WHERE account_id = ? AND role = ? AND status = 'ACTIVE'""",
                (account["id"], role),
            ).fetchone()
            if binding is None:
                connection.rollback()
                raise ManagedCatalogError("Managed account has no active credential for that role")
            connection.execute(
                "UPDATE secret_binding SET status = 'CLEANUP_REQUIRED' WHERE id = ?",
                (binding["id"],),
            )
            connection.execute(
                "UPDATE managed_account SET revision = revision + 1 WHERE id = ? AND revision = ?",
                (account["id"], expected_revision),
            )
            connection.commit()

        cleaned = False
        try:
            cleaned = self.secret_store.delete(binding["opaque_locator"])
            if cleaned:
                with self._connection() as connection:
                    connection.execute(
                        "UPDATE secret_binding SET status = 'SUPERSEDED' WHERE id = ? AND status = 'CLEANUP_REQUIRED'",
                        (binding["id"],),
                    )
                    connection.commit()
        except Exception:
            logger.warning("Credential removal committed but cleanup finalization requires reconciliation")
            cleaned = False
        return CredentialRemovalResult(
            status="removed" if cleaned else "removed_cleanup_required",
            revision=expected_revision + 1,
            cleanup_required=0 if cleaned else 1,
        )

    def cleanup_credentials(
        self,
        *,
        limit: int = MAX_CREDENTIAL_CLEANUP_ROWS,
        expected_revision: int | None = None,
    ) -> CredentialCleanupReport:
        if not 1 <= limit <= MAX_CREDENTIAL_CLEANUP_ROWS:
            raise ManagedCatalogError(f"Credential cleanup limit must be between 1 and {MAX_CREDENTIAL_CLEANUP_ROWS}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            catalog = connection.execute("SELECT revision FROM catalog WHERE id = 'local'").fetchone()
            if catalog is None:
                connection.rollback()
                raise ManagedCatalogError("Managed catalog row is missing")
            if expected_revision is not None and catalog["revision"] != expected_revision:
                connection.rollback()
                raise ManagedRevisionConflictError("catalog")
            candidates = connection.execute(
                """SELECT id, opaque_locator FROM secret_binding
                   WHERE status = 'CLEANUP_REQUIRED'
                   ORDER BY created_at, id LIMIT ?""",
                (limit,),
            ).fetchall()
            if candidates:
                connection.execute(
                    "UPDATE catalog SET revision = revision + 1 WHERE id = 'local' AND revision = ?",
                    (catalog["revision"],),
                )
            connection.commit()
        cleaned = 0
        for candidate in candidates:
            try:
                if not self.secret_store.delete(candidate["opaque_locator"]):
                    continue
                with self._connection() as connection:
                    cursor = connection.execute(
                        """UPDATE secret_binding SET status = 'SUPERSEDED'
                           WHERE id = ? AND status = 'CLEANUP_REQUIRED'""",
                        (candidate["id"],),
                    )
                    connection.commit()
                cleaned += cursor.rowcount
            except Exception:
                logger.warning("Credential cleanup was claimed but finalization requires reconciliation")
        return CredentialCleanupReport(
            examined=len(candidates),
            cleaned=cleaned,
            remaining=len(candidates) - cleaned,
        )

    def _problems(self, connection: sqlite3.Connection, *, require_enabled: bool = True) -> list[str]:
        problems: list[str] = []
        accounts = connection.execute(
            "SELECT id, name, enabled FROM managed_account WHERE removed_at IS NULL ORDER BY name"
        ).fetchall()
        if require_enabled and not any(bool(row["enabled"]) for row in accounts):
            problems.append("no_enabled_account")
        for account in accounts:
            if not account["enabled"]:
                continue
            incoming_endpoint = connection.execute(
                "SELECT 1 FROM endpoint WHERE account_id = ? AND role = 'incoming'", (account["id"],)
            ).fetchone()
            incoming_binding = connection.execute(
                "SELECT 1 FROM secret_binding WHERE account_id = ? AND role = 'incoming' AND status = 'ACTIVE'",
                (account["id"],),
            ).fetchone()
            if incoming_endpoint is None or incoming_binding is None:
                problems.append(f"account_incomplete:{account['name']}:incoming")
            outgoing_endpoint = connection.execute(
                "SELECT 1 FROM endpoint WHERE account_id = ? AND role = 'outgoing'", (account["id"],)
            ).fetchone()
            outgoing_binding = connection.execute(
                "SELECT 1 FROM secret_binding WHERE account_id = ? AND role = 'outgoing' AND status = 'ACTIVE'",
                (account["id"],),
            ).fetchone()
            if (outgoing_endpoint is None) != (outgoing_binding is None):
                problems.append(f"account_incomplete:{account['name']}:outgoing")
        return problems

    def doctor(self) -> DoctorReport:
        with self._connection() as connection:
            catalog = connection.execute("SELECT revision FROM catalog WHERE id = 'local'").fetchone()
            if catalog is None:
                raise ManagedCatalogError("Managed catalog row is missing")
            counts = connection.execute(
                """SELECT COUNT(*) accounts, COALESCE(SUM(enabled), 0) enabled
                   FROM managed_account WHERE removed_at IS NULL"""
            ).fetchone()
            cleanup = connection.execute(
                "SELECT COUNT(*) count FROM secret_binding WHERE status = 'CLEANUP_REQUIRED'"
            ).fetchone()
            problems = self._problems(connection)
            active_bindings = connection.execute(
                """SELECT a.name, b.role, b.opaque_locator
                   FROM secret_binding b
                   JOIN managed_account a ON a.id = b.account_id
                   WHERE b.status = 'ACTIVE' AND a.enabled = 1 AND a.removed_at IS NULL
                   ORDER BY a.name, b.role"""
            ).fetchall()
        for binding in active_bindings:
            try:
                self.secret_store.get(binding["opaque_locator"])
            except ManagedCatalogError:
                problems.append(f"active_secret_unavailable:{binding['name']}:{binding['role']}")
        return DoctorReport(
            schema_version=SCHEMA_VERSION,
            catalog_revision=catalog["revision"],
            account_count=counts["accounts"],
            enabled_account_count=counts["enabled"],
            cleanup_required_bindings=cleanup["count"],
            problems=tuple(problems),
        )

    def index_health(self) -> IndexHealth:
        with self._connection() as connection:
            indexed_accounts = connection.execute("SELECT COUNT(*) count FROM operational_account").fetchone()["count"]
            pending = connection.execute(
                "SELECT COUNT(*) count FROM index_coverage WHERE completeness = 'PARTIAL'"
            ).fetchone()["count"]
        problems = ("partial_coverage",) if pending else ()
        return IndexHealth(
            status="degraded" if problems else "healthy",
            indexed_accounts=min(indexed_accounts, APPLICATION_LIMITS.metadata_candidates),
            pending_operations=min(pending, APPLICATION_LIMITS.metadata_candidates),
            problems=problems,
        )

    def validate_ready(self) -> None:
        """Validate selected non-secret catalog structure and authority metadata."""
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM catalog WHERE id = 'local'").fetchone() is None:
                raise ManagedCatalogError("Managed catalog row is missing")

    def _read_account_authority(
        self,
        name: str,
        *,
        roles: tuple[BindingRole, ...],
    ) -> _ManagedAuthoritySnapshot:
        with self._connection() as connection:
            catalog = connection.execute("SELECT * FROM catalog WHERE id = 'local'").fetchone()
            if catalog is None:
                raise ManagedCatalogError("Managed catalog row is missing")
            account = connection.execute(
                """SELECT * FROM managed_account
                   WHERE normalized_name = ? AND enabled = 1 AND removed_at IS NULL""",
                (_normalize_account_name(name),),
            ).fetchone()
            if account is None:
                raise ManagedCatalogError("Managed account was not found or is disabled")
            endpoints = {
                row["role"]: row
                for row in connection.execute("SELECT * FROM endpoint WHERE account_id = ?", (account["id"],))
            }
            bindings = {
                row["role"]: row
                for row in connection.execute(
                    """SELECT id, role, opaque_locator FROM secret_binding
                       WHERE account_id = ? AND status = 'ACTIVE'""",
                    (account["id"],),
                )
            }
        incoming_row = endpoints.get("incoming")
        incoming_binding = bindings.get("incoming")
        if incoming_row is None or incoming_binding is None:
            raise ManagedCatalogError("Enabled managed account has no active incoming binding")
        outgoing_row = endpoints.get("outgoing")
        outgoing_binding = bindings.get("outgoing")
        if (outgoing_row is None) != (outgoing_binding is None):
            raise ManagedCatalogError("Managed outgoing endpoint and credential binding are inconsistent")
        for role in roles:
            if role not in endpoints or role not in bindings:
                raise ManagedCatalogError(f"Managed account has no active {role} binding")
        return _ManagedAuthoritySnapshot(
            catalog_revision=catalog["revision"],
            policy=self._policy_from_row(catalog),
            account_id=account["id"],
            account_revision=account["revision"],
            name=account["name"],
            full_name=account["full_name"],
            email_address=account["email_address"],
            save_to_sent=bool(account["save_to_sent"]),
            sent_folder_name=account["sent_folder_name"],
            tags=_parse_tags_json(account["tags_json"]),
            incoming=self._endpoint_summary(incoming_row),
            outgoing=self._endpoint_summary(outgoing_row) if outgoing_row is not None else None,
            binding_ids=tuple((role, bindings[role]["id"]) for role in sorted(bindings)),
            binding_locators=tuple((role, bindings[role]["opaque_locator"]) for role in sorted(bindings)),
        )

    @staticmethod
    def _email_server(endpoint: EndpointSummary, secret: str) -> EmailServer:
        return EmailServer(
            user_name=endpoint.user_name,
            password=SecretStr(secret),
            host=endpoint.host,
            port=endpoint.port,
            use_ssl=endpoint.use_ssl,
            start_ssl=endpoint.start_ssl,
            verify_ssl=endpoint.verify_ssl,
        )

    def resolve_account(
        self,
        name: str,
        *,
        roles: tuple[BindingRole, ...] = ("incoming",),
    ) -> ManagedAccountResolution:
        """Resolve selected roles and reject any authority drift around SecretStore reads."""
        if len(set(roles)) != len(roles) or any(role not in ("incoming", "outgoing") for role in roles):
            raise ManagedCatalogError("Managed account roles are invalid")
        initial = self._read_account_authority(name, roles=roles)
        # Revalidate after request/policy inspection and before resolving values.
        if self._read_account_authority(name, roles=roles) != initial:
            raise ManagedCatalogError("Managed account authority changed; reload and retry")
        locators = dict(initial.binding_locators)
        secrets = {role: self.secret_store.get(locators[role]) for role in roles}
        # SecretStore access may block while account, policy, endpoint, or binding
        # authority changes. Never combine a fresh value with a stale snapshot.
        current = self._read_account_authority(name, roles=roles)
        if current != initial:
            raise ManagedCatalogError("Managed account authority changed; reload and retry")
        incoming = self._email_server(current.incoming, secrets.get("incoming", ""))
        outgoing = (
            self._email_server(current.outgoing, secrets.get("outgoing", "")) if current.outgoing is not None else None
        )
        return ManagedAccountResolution(
            account=EmailSettings(
                account_name=current.name,
                full_name=current.full_name,
                email_address=current.email_address,
                incoming=incoming,
                outgoing=outgoing,
                save_to_sent=current.save_to_sent,
                sent_folder_name=current.sent_folder_name,
                tags=current.tags,
            ),
            policy=current.policy,
        )

    def load_account(
        self,
        name: str,
        *,
        roles: tuple[BindingRole, ...] = ("incoming",),
    ) -> EmailSettings:
        """Compatibility projection for one securely revisioned account resolution."""
        return self.resolve_account(name, roles=roles).account

    def load_settings(self) -> Settings:
        with self._connection() as connection:
            catalog = connection.execute("SELECT * FROM catalog WHERE id = 'local'").fetchone()
            if catalog is None:
                raise ManagedCatalogError("Managed catalog row is missing")
            accounts = connection.execute(
                """SELECT * FROM managed_account
                   WHERE enabled = 1 AND removed_at IS NULL ORDER BY name LIMIT ?""",
                (APPLICATION_LIMITS.configured_accounts + 1,),
            ).fetchall()
            if len(accounts) > APPLICATION_LIMITS.configured_accounts:
                raise ManagedCatalogError("Managed account count exceeds the application limit")
            resolved: list[EmailSettings] = []
            for account in accounts:
                endpoints = {
                    row["role"]: row
                    for row in connection.execute("SELECT * FROM endpoint WHERE account_id = ?", (account["id"],))
                }
                bindings = {
                    row["role"]: row
                    for row in connection.execute(
                        "SELECT role, opaque_locator FROM secret_binding WHERE account_id = ? AND status = 'ACTIVE'",
                        (account["id"],),
                    )
                }
                incoming_row = endpoints.get("incoming")
                incoming_binding = bindings.get("incoming")
                outgoing_row = endpoints.get("outgoing")
                outgoing_binding = bindings.get("outgoing")
                if incoming_row is None or incoming_binding is None:
                    continue
                if (outgoing_row is None) != (outgoing_binding is None):
                    continue
                incoming = self._resolve_endpoint(incoming_row, incoming_binding, resolve_secret=False)
                outgoing = self._resolve_endpoint(outgoing_row, outgoing_binding, resolve_secret=False)
                if not isinstance(incoming, EmailServer):
                    continue
                resolved.append(
                    EmailSettings(
                        account_name=account["name"],
                        full_name=account["full_name"],
                        email_address=account["email_address"],
                        incoming=incoming,
                        outgoing=outgoing,
                        save_to_sent=bool(account["save_to_sent"]),
                        sent_folder_name=account["sent_folder_name"],
                        tags=_parse_tags_json(account["tags_json"]),
                    )
                )
            policy = self._policy_from_row(catalog)

        return Settings.model_construct(
            emails=resolved,
            providers=[],
            db_location=self.path.as_posix(),
            enable_attachment_download=policy.enable_attachment_download,
            enable_attachment_content=policy.enable_attachment_content,
            allowed_recipients=list(policy.allowed_recipients),
            allowed_senders=list(policy.allowed_senders),
            report_blocked_mutations=policy.report_blocked_mutations,
            credential_storage="keyring",
        )

    def _resolve_endpoint(
        self,
        endpoint: sqlite3.Row | None,
        binding: sqlite3.Row | None,
        *,
        resolve_secret: bool,
    ) -> EmailServer | None:
        if endpoint is None and binding is None:
            return None
        if endpoint is None or binding is None:
            raise ManagedCatalogError("Managed endpoint and credential binding are inconsistent")
        secret = self.secret_store.get(binding["opaque_locator"]) if resolve_secret else ""
        return EmailServer(
            user_name=endpoint["user_name"],
            password=SecretStr(secret),
            host=endpoint["host"],
            port=endpoint["port"],
            use_ssl=bool(endpoint["use_ssl"]),
            start_ssl=bool(endpoint["start_ssl"]),
            verify_ssl=bool(endpoint["verify_ssl"]),
        )
