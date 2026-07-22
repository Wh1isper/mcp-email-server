from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from pydantic import SecretStr

from mcp_email_server.application.management import (
    AccountDetails,
    AccountSummary,
    BindingRole,
    CredentialCleanupReport,
    DoctorReport,
    EndpointSummary,
    Lifecycle,
    ManagedPolicy,
    ManagementError,
)
from mcp_email_server.config import EmailServer, EmailSettings, Settings

SCHEMA_VERSION = 1
SQLITE_BUSY_TIMEOUT_MS = 5_000
WAL_RETRY_BUSY_TIMEOUT_MS = 100
MANAGED_KEYRING_SERVICE = "mcp-email-server-managed"
MAX_CREDENTIAL_CLEANUP_ROWS = 100
PENDING_CLEANUP_MINIMUM_AGE = timedelta(minutes=5)


class ManagedCatalogError(ManagementError):
    """A managed catalog operation failed without exposing sensitive internals."""


class ManagedCatalogSecurityError(ManagedCatalogError):
    """A managed catalog path does not meet local security requirements."""


class ManagedKeyringSecretStore:
    """Keyring-backed immutable candidate store for managed credentials."""

    def put(self, locator: str, value: str) -> None:
        import keyring

        try:
            existing = keyring.get_password(MANAGED_KEYRING_SERVICE, locator)
        except Exception as exc:
            raise ManagedCatalogError("Managed secret backend rejected the credential") from exc
        if existing is not None:
            raise ManagedCatalogError("Secret candidate already exists")
        try:
            keyring.set_password(MANAGED_KEYRING_SERVICE, locator, value)
        except Exception as exc:
            raise ManagedCatalogError("Managed secret backend rejected the credential") from exc

    def get(self, locator: str) -> str:
        import keyring

        try:
            value = keyring.get_password(MANAGED_KEYRING_SERVICE, locator)
        except Exception as exc:
            raise ManagedCatalogError("Managed secret backend is unavailable") from exc
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


def _mode_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ManagedCatalogSecurityError("Managed catalog parent must be a real directory")
    if os.name == "posix":
        metadata = path.stat()
        if metadata.st_uid != os.getuid() or _mode_bits(path) & 0o077:
            raise ManagedCatalogSecurityError("Managed catalog parent must be owner-only")


def _assert_private_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ManagedCatalogSecurityError("Managed catalog file could not be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ManagedCatalogSecurityError("Managed catalog must be a regular file and not a symlink")
    if os.name == "posix" and (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise ManagedCatalogSecurityError("Managed catalog file must be owner-only")
    return metadata


def _prepare_new_file(path: Path) -> None:
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_private_directory(parent)
    if path.exists() or path.is_symlink():
        raise ManagedCatalogError("Managed catalog already exists")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ManagedCatalogError("Managed catalog already exists") from exc
    os.close(fd)


def _secure_sidecars(path: Path) -> None:
    if os.name != "posix":
        return
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ManagedCatalogSecurityError("Managed catalog sidecar is unsafe")
        if metadata.st_uid != os.getuid():
            raise ManagedCatalogSecurityError("Managed catalog sidecar must be owned by the current user")
        try:
            os.chmod(sidecar, 0o600, follow_symlinks=False)
            checked = sidecar.lstat()
        except FileNotFoundError:
            # SQLite removes sidecars when the final concurrent connection closes.
            continue
        if not stat.S_ISREG(checked.st_mode) or checked.st_uid != os.getuid() or stat.S_IMODE(checked.st_mode) & 0o077:
            raise ManagedCatalogSecurityError("Managed catalog sidecar must be owner-only")


def _validate_existing_path(path: Path) -> os.stat_result:
    _assert_private_directory(path.parent)
    return _assert_private_file(path)


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
    if require_exists and not path.exists():
        raise ManagedCatalogError("Selected managed catalog is missing")
    before = _validate_existing_path(path)
    try:
        connection = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error as exc:
        raise ManagedCatalogError("Could not open managed catalog") from exc
    try:
        after = _assert_private_file(path)
        if os.name == "posix" and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ManagedCatalogSecurityError("Managed catalog changed while it was being opened")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        if enable_wal:
            _enable_wal(connection)
            _secure_sidecars(path)
        yield connection
    except sqlite3.DatabaseError as exc:
        raise ManagedCatalogError("Managed catalog is corrupt or unavailable") from exc
    finally:
        connection.close()
        if enable_wal:
            _secure_sidecars(path)


_AUTHORITY_SCHEMA = """
CREATE TABLE schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
CREATE TABLE catalog (
    id TEXT PRIMARY KEY CHECK (id = 'local'),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('STAGING', 'ACTIVE')),
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
    full_name TEXT NOT NULL,
    email_address TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    save_to_sent INTEGER NOT NULL CHECK (save_to_sent IN (0, 1)),
    sent_folder_name TEXT,
    removed_at TEXT,
    UNIQUE (catalog_id, name)
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
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'SUPERSEDED', 'CLEANUP_REQUIRED')),
    opaque_locator TEXT NOT NULL UNIQUE,
    supersedes_id TEXT REFERENCES secret_binding(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX one_active_binding_per_role
    ON secret_binding(account_id, role) WHERE status = 'ACTIVE';
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

_SCHEMA = _AUTHORITY_SCHEMA + _OPERATIONAL_SCHEMA
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


class ManagedCatalog:
    def __init__(self, path: Path, secret_store: ManagedKeyringSecretStore | None = None) -> None:
        self.path = Path(os.path.abspath(path.expanduser()))
        self.secret_store = secret_store or ManagedKeyringSecretStore()

    @classmethod
    def initialize(cls, path: Path) -> ManagedCatalog:
        normalized = Path(os.path.abspath(path.expanduser()))
        _prepare_new_file(normalized)
        try:
            with _connect(normalized) as connection:
                connection.executescript(_SCHEMA)
                connection.execute("INSERT INTO schema_metadata(singleton, version) VALUES (1, ?)", (SCHEMA_VERSION,))
                connection.execute(
                    """INSERT INTO catalog(
                           id, lifecycle, revision, enable_attachment_download,
                           allowed_recipients_json, allowed_senders_json, report_blocked_mutations
                       ) VALUES ('local', 'STAGING', 1, 0, '[]', '[]', 0)"""
                )
                connection.commit()
        except Exception:
            with contextlib.suppress(OSError):
                normalized.unlink()
            raise
        return cls(normalized)

    def _preflight_schema_ownership(self, connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute("SELECT version FROM schema_metadata WHERE singleton = 1").fetchone()
        except sqlite3.Error as exc:
            raise ManagedCatalogError("Managed catalog is corrupt or schema is missing or incompatible") from exc
        if row is None or row["version"] != SCHEMA_VERSION:
            raise ManagedCatalogError("Managed catalog schema version is unsupported")
        _validate_managed_schema(connection, _SCHEMA)

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with _connect(self.path, enable_wal=False) as connection:
            self._preflight_schema_ownership(connection)
        with _connect(self.path) as connection:
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

    def lifecycle(self) -> Lifecycle:
        with self._connection() as connection:
            row = connection.execute("SELECT lifecycle FROM catalog WHERE id = 'local'").fetchone()
            if row is None or row["lifecycle"] not in ("STAGING", "ACTIVE"):
                raise ManagedCatalogError("Managed catalog lifecycle is invalid")
            return row["lifecycle"]

    def policy(self) -> ManagedPolicy:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM catalog WHERE id = 'local'").fetchone()
        if row is None:
            raise ManagedCatalogError("Managed catalog row is missing")
        try:
            allowed_recipients = json.loads(row["allowed_recipients_json"])
            allowed_senders = json.loads(row["allowed_senders_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ManagedCatalogError("Managed policy data is invalid") from exc
        if not isinstance(allowed_recipients, list) or not all(isinstance(item, str) for item in allowed_recipients):
            raise ManagedCatalogError("Managed recipient policy is invalid")
        if not isinstance(allowed_senders, list) or not all(isinstance(item, str) for item in allowed_senders):
            raise ManagedCatalogError("Managed sender policy is invalid")
        return ManagedPolicy(
            revision=row["revision"],
            enable_attachment_download=bool(row["enable_attachment_download"]),
            allowed_recipients=tuple(allowed_recipients),
            allowed_senders=tuple(allowed_senders),
            report_blocked_mutations=bool(row["report_blocked_mutations"]),
        )

    def update_policy(
        self,
        *,
        expected_revision: int,
        enable_attachment_download: bool,
        allowed_recipients: tuple[str, ...],
        allowed_senders: tuple[str, ...],
        report_blocked_mutations: bool,
    ) -> int:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected catalog revision must be positive")
        if any(not item.strip() for item in (*allowed_recipients, *allowed_senders)):
            raise ManagedCatalogError("Managed policy entries must not be empty")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE catalog SET
                       revision = revision + 1,
                       enable_attachment_download = ?,
                       allowed_recipients_json = ?,
                       allowed_senders_json = ?,
                       report_blocked_mutations = ?
                   WHERE id = 'local' AND revision = ?""",
                (
                    int(enable_attachment_download),
                    json.dumps(list(allowed_recipients)),
                    json.dumps(list(allowed_senders)),
                    int(report_blocked_mutations),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ManagedCatalogError("Managed catalog revision changed; reload and retry")
            connection.commit()
        return expected_revision + 1

    def add_account(
        self,
        *,
        name: str,
        full_name: str,
        email_address: str,
        incoming: EmailServer,
        outgoing: EmailServer | None,
        save_to_sent: bool = True,
        sent_folder_name: str | None = None,
    ) -> str:
        if not name.strip() or not full_name.strip() or not email_address.strip():
            raise ManagedCatalogError("Account name, full name, and email address are required")
        account_id = uuid.uuid4().hex
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                lifecycle = connection.execute("SELECT lifecycle FROM catalog WHERE id = 'local'").fetchone()
                if lifecycle is None or lifecycle["lifecycle"] != "STAGING":
                    connection.rollback()
                    raise ManagedCatalogError("New accounts can be added only while the managed catalog is STAGING")
                connection.execute(
                    """INSERT INTO managed_account(
                           id, catalog_id, name, full_name, email_address, enabled, revision,
                           save_to_sent, sent_folder_name, removed_at
                       ) VALUES (?, 'local', ?, ?, ?, 1, 1, ?, ?, NULL)""",
                    (account_id, name, full_name, email_address, int(save_to_sent), sent_folder_name),
                )
                self._insert_endpoint(connection, account_id, "incoming", incoming)
                if outgoing is not None:
                    self._insert_endpoint(connection, account_id, "outgoing", outgoing)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ManagedCatalogError("Account name already exists or endpoint settings are invalid") from exc
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
                "SELECT id, revision FROM managed_account WHERE name = ? AND removed_at IS NULL", (name,)
            ).fetchone()
            if row is None:
                raise ManagedCatalogError("Managed account was not found")
            return row["id"], row["revision"]

    def set_secret(self, name: str, role: BindingRole, value: str) -> None:
        if not value:
            raise ManagedCatalogError("Credential must not be empty")
        account_id, expected_revision = self.account_revision(name)
        binding_id = uuid.uuid4().hex
        locator = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            endpoint = connection.execute(
                "SELECT 1 FROM endpoint WHERE account_id = ? AND role = ?", (account_id, role)
            ).fetchone()
            if endpoint is None:
                connection.rollback()
                raise ManagedCatalogError("Credential role has no configured endpoint")
            active = connection.execute(
                "SELECT id FROM secret_binding WHERE account_id = ? AND role = ? AND status = 'ACTIVE'",
                (account_id, role),
            ).fetchone()
            connection.execute(
                """INSERT INTO secret_binding(id, account_id, role, status, opaque_locator, supersedes_id)
                   VALUES (?, ?, ?, 'PENDING', ?, ?)""",
                (binding_id, account_id, role, locator, active["id"] if active else None),
            )
            connection.commit()

        self.secret_store.put(locator, value)

        old_locator: str | None = None
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT revision FROM managed_account WHERE id = ?", (account_id,)
                ).fetchone()
                pending = connection.execute("SELECT status FROM secret_binding WHERE id = ?", (binding_id,)).fetchone()
                if (
                    current is None
                    or current["revision"] != expected_revision
                    or pending is None
                    or pending["status"] != "PENDING"
                ):
                    connection.rollback()
                    cleaned = self.secret_store.delete(locator)
                    with _connect(self.path) as repair_connection:
                        repair_connection.execute(
                            "UPDATE secret_binding SET status = ? WHERE id = ? AND status = 'PENDING'",
                            ("SUPERSEDED" if cleaned else "CLEANUP_REQUIRED", binding_id),
                        )
                        repair_connection.commit()
                    raise ManagedCatalogError(  # noqa: TRY301
                        "Account changed while credential was being stored; retry"
                    )
                old = connection.execute(
                    """SELECT id, opaque_locator FROM secret_binding
                       WHERE account_id = ? AND role = ? AND status = 'ACTIVE'""",
                    (account_id, role),
                ).fetchone()
                if old is not None:
                    old_locator = old["opaque_locator"]
                    # Persist cleanup work before commit. A crash after activating
                    # the replacement must leave doctor-visible recovery state.
                    connection.execute(
                        "UPDATE secret_binding SET status = 'CLEANUP_REQUIRED' WHERE id = ?", (old["id"],)
                    )
                connection.execute("UPDATE secret_binding SET status = 'ACTIVE' WHERE id = ?", (binding_id,))
                connection.execute(
                    "UPDATE managed_account SET revision = revision + 1 WHERE id = ? AND revision = ?",
                    (account_id, expected_revision),
                )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

        if old_locator is not None and self.secret_store.delete(old_locator):
            with self._connection() as connection:
                connection.execute(
                    "UPDATE secret_binding SET status = 'SUPERSEDED' WHERE opaque_locator = ?",
                    (old_locator,),
                )
                connection.commit()
        self._retire_pending_bindings(account_id, role, active_binding_id=binding_id)

    def _retire_pending_bindings(self, account_id: str, role: BindingRole, *, active_binding_id: str) -> None:
        """Atomically claim prior candidates before external cleanup."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                """SELECT id, opaque_locator FROM secret_binding
                   WHERE account_id = ? AND role = ? AND status = 'PENDING' AND id != ?""",
                (account_id, role, active_binding_id),
            ).fetchall()
            for binding in pending:
                connection.execute(
                    "UPDATE secret_binding SET status = 'CLEANUP_REQUIRED' WHERE id = ? AND status = 'PENDING'",
                    (binding["id"],),
                )
            connection.commit()
        for binding in pending:
            if not self.secret_store.delete(binding["opaque_locator"]):
                continue
            with self._connection() as connection:
                connection.execute(
                    "UPDATE secret_binding SET status = 'SUPERSEDED' WHERE id = ? AND status = 'CLEANUP_REQUIRED'",
                    (binding["id"],),
                )
                connection.commit()

    def list_accounts(self) -> list[AccountSummary]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT a.name, a.email_address, a.enabled, a.revision,
                          EXISTS(SELECT 1 FROM endpoint e WHERE e.account_id = a.id AND e.role = 'outgoing') has_outgoing,
                          (SELECT status FROM secret_binding b WHERE b.account_id = a.id AND b.role = 'incoming'
                           ORDER BY CASE status WHEN 'ACTIVE' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END, created_at DESC LIMIT 1) incoming_binding,
                          (SELECT status FROM secret_binding b WHERE b.account_id = a.id AND b.role = 'outgoing'
                           ORDER BY CASE status WHEN 'ACTIVE' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END, created_at DESC LIMIT 1) outgoing_binding
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
                "SELECT * FROM managed_account WHERE name = ? AND removed_at IS NULL",
                (name,),
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
                   ORDER BY CASE status WHEN 'ACTIVE' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END,
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
            incoming=incoming,
            outgoing=endpoints.get("outgoing"),
            incoming_binding=bindings.get("incoming", "MISSING"),
            outgoing_binding=bindings.get("outgoing"),
        )

    def has_removed_account(self, name: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM managed_account WHERE name = ? AND removed_at IS NOT NULL",
                (name,),
            ).fetchone()
        return row is not None

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
        if not any((
            new_name is not None,
            full_name is not None,
            email_address is not None,
            incoming is not None,
            outgoing is not None,
            remove_outgoing,
            save_to_sent is not None,
            update_sent_folder,
        )):
            raise ManagedCatalogError("Account update did not specify any changes")

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
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
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ManagedCatalogError("Account name already exists or endpoint settings are invalid") from exc
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
    ) -> None:
        account = connection.execute(
            "SELECT id, enabled, revision FROM managed_account WHERE name = ? AND removed_at IS NULL",
            (name,),
        ).fetchone()
        if account is None:
            raise ManagedCatalogError("Managed account was not found")
        if account["revision"] != expected_revision:
            raise ManagedCatalogError("Managed account revision changed; reload and retry")
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
        for column, value in (
            ("name", new_name),
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
        values.extend((account["id"], expected_revision))
        cursor = connection.execute(
            f"UPDATE managed_account SET {', '.join(assignments)} WHERE id = ? AND revision = ?",  # noqa: S608
            values,
        )
        if cursor.rowcount != 1:
            raise ManagedCatalogError("Managed account revision changed; reload and retry")
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
                   WHERE name = ? AND removed_at IS NULL AND enabled = 1 AND revision = ?""",
                (name, expected_revision),
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
                   WHERE name = ? AND removed_at IS NULL""",
                (name,),
            ).fetchone()
            if account is None or account["enabled"]:
                raise ManagedCatalogError("Managed account was not found or is already enabled")
            if account["revision"] != expected_revision:
                raise ManagedCatalogError("Managed account revision changed; reload and retry")
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
                raise ManagedCatalogError("Managed account changed while credentials were validated; retry")
            if self._account_problems(connection, account["id"]):
                connection.rollback()
                raise ManagedCatalogError("Managed account changed while credentials were validated; retry")
            cursor = connection.execute(
                """UPDATE managed_account SET enabled = 1, revision = revision + 1
                   WHERE id = ? AND enabled = 0 AND revision = ?""",
                (account["id"], expected_revision),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ManagedCatalogError("Managed account changed while credentials were validated; retry")
            connection.commit()
        return expected_revision + 1

    def soft_remove_account(self, name: str, *, expected_revision: int) -> int:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected account revision must be positive")
        removed_at = datetime.now(UTC).isoformat(timespec="microseconds")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE managed_account
                   SET enabled = 0, removed_at = ?, revision = revision + 1
                   WHERE name = ? AND removed_at IS NULL AND revision = ?""",
                (removed_at, name, expected_revision),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ManagedCatalogError("Managed account was not found or revision changed")
            connection.commit()
        return expected_revision + 1

    def remove_secret(self, name: str, role: BindingRole, *, expected_revision: int) -> bool:
        if expected_revision < 1:
            raise ManagedCatalogError("Expected account revision must be positive")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                """SELECT id, enabled, revision FROM managed_account
                   WHERE name = ? AND removed_at IS NULL""",
                (name,),
            ).fetchone()
            if account is None:
                connection.rollback()
                raise ManagedCatalogError("Managed account was not found")
            if account["revision"] != expected_revision:
                connection.rollback()
                raise ManagedCatalogError("Managed account revision changed; reload and retry")
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

        cleaned = self.secret_store.delete(binding["opaque_locator"])
        if cleaned:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE secret_binding SET status = 'SUPERSEDED' WHERE id = ? AND status = 'CLEANUP_REQUIRED'",
                    (binding["id"],),
                )
                connection.commit()
        return cleaned

    def cleanup_credentials(self, *, limit: int = MAX_CREDENTIAL_CLEANUP_ROWS) -> CredentialCleanupReport:
        if not 1 <= limit <= MAX_CREDENTIAL_CLEANUP_ROWS:
            raise ManagedCatalogError(f"Credential cleanup limit must be between 1 and {MAX_CREDENTIAL_CLEANUP_ROWS}")
        pending_before = (datetime.now(UTC) - PENDING_CLEANUP_MINIMUM_AGE).strftime("%Y-%m-%d %H:%M:%S")
        with self._connection() as connection:
            candidates = connection.execute(
                """SELECT id, opaque_locator FROM secret_binding
                   WHERE status = 'CLEANUP_REQUIRED'
                      OR (status = 'PENDING' AND created_at <= ?)
                   ORDER BY created_at, id LIMIT ?""",
                (pending_before, limit),
            ).fetchall()
        cleaned = 0
        for candidate in candidates:
            # Claim stale PENDING work before external deletion. A concurrent
            # set_secret finalizer requires PENDING and therefore cannot activate
            # a candidate after cleanup has detached it.
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                claimed = connection.execute(
                    """UPDATE secret_binding SET status = 'CLEANUP_REQUIRED'
                       WHERE id = ? AND (
                           status = 'CLEANUP_REQUIRED'
                           OR (status = 'PENDING' AND created_at <= ?)
                       )""",
                    (candidate["id"], pending_before),
                )
                connection.commit()
            if claimed.rowcount != 1 or not self.secret_store.delete(candidate["opaque_locator"]):
                continue
            with self._connection() as connection:
                cursor = connection.execute(
                    """UPDATE secret_binding SET status = 'SUPERSEDED'
                       WHERE id = ? AND status = 'CLEANUP_REQUIRED'""",
                    (candidate["id"],),
                )
                connection.commit()
            cleaned += cursor.rowcount
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
            catalog = connection.execute("SELECT lifecycle FROM catalog WHERE id = 'local'").fetchone()
            if catalog is None or catalog["lifecycle"] not in ("STAGING", "ACTIVE"):
                raise ManagedCatalogError("Managed catalog lifecycle is invalid")
            counts = connection.execute(
                """SELECT COUNT(*) accounts, COALESCE(SUM(enabled), 0) enabled
                   FROM managed_account WHERE removed_at IS NULL"""
            ).fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) count FROM secret_binding WHERE status = 'PENDING'"
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
            lifecycle=catalog["lifecycle"],
            schema_version=SCHEMA_VERSION,
            account_count=counts["accounts"],
            enabled_account_count=counts["enabled"],
            pending_bindings=pending["count"],
            cleanup_required_bindings=cleanup["count"],
            problems=tuple(problems),
        )

    def activate(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT lifecycle FROM catalog WHERE id = 'local'").fetchone()
            if row is None:
                connection.rollback()
                raise ManagedCatalogError("Managed catalog row is missing")
            problems = self._problems(connection)
            if problems:
                connection.rollback()
                raise ManagedCatalogError("Managed catalog is incomplete: " + ", ".join(problems))
            connection.execute("UPDATE catalog SET lifecycle = 'ACTIVE', revision = revision + 1 WHERE id = 'local'")
            connection.commit()

    def load_account(self, name: str, *, require_active_catalog: bool = False) -> EmailSettings:
        """Resolve one enabled account and only its secret bindings."""
        with self._connection() as connection:
            catalog = connection.execute("SELECT lifecycle FROM catalog WHERE id = 'local'").fetchone()
            if catalog is None or (require_active_catalog and catalog["lifecycle"] != "ACTIVE"):
                raise ManagedCatalogError("Managed catalog is not active")
            account = connection.execute(
                """SELECT * FROM managed_account
                   WHERE name = ? AND enabled = 1 AND removed_at IS NULL""",
                (name,),
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
                    "SELECT role, opaque_locator FROM secret_binding WHERE account_id = ? AND status = 'ACTIVE'",
                    (account["id"],),
                )
            }
        incoming = self._resolve_endpoint(endpoints.get("incoming"), bindings.get("incoming"))
        outgoing = self._resolve_endpoint(endpoints.get("outgoing"), bindings.get("outgoing"))
        if incoming is None:
            raise ManagedCatalogError("Enabled managed account has no active incoming credential")
        return EmailSettings(
            account_name=account["name"],
            full_name=account["full_name"],
            email_address=account["email_address"],
            incoming=incoming,
            outgoing=outgoing,
            save_to_sent=bool(account["save_to_sent"]),
            sent_folder_name=account["sent_folder_name"],
        )

    def load_settings(self, *, require_active: bool = True) -> Settings:
        with self._connection() as connection:
            catalog = connection.execute("SELECT * FROM catalog WHERE id = 'local'").fetchone()
            if catalog is None:
                raise ManagedCatalogError("Managed catalog row is missing")
            if require_active and catalog["lifecycle"] != "ACTIVE":
                raise ManagedCatalogError("Selected managed catalog is not active")
            if require_active:
                problems = self._problems(connection, require_enabled=False)
                if problems:
                    raise ManagedCatalogError("Selected managed catalog is incomplete")
            accounts = connection.execute(
                "SELECT * FROM managed_account WHERE enabled = 1 AND removed_at IS NULL ORDER BY name"
            ).fetchall()
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
                incoming = self._resolve_endpoint(endpoints.get("incoming"), bindings.get("incoming"))
                outgoing = self._resolve_endpoint(endpoints.get("outgoing"), bindings.get("outgoing"))
                if incoming is None:
                    raise ManagedCatalogError("Enabled managed account has no active incoming credential")
                resolved.append(
                    EmailSettings(
                        account_name=account["name"],
                        full_name=account["full_name"],
                        email_address=account["email_address"],
                        incoming=incoming,
                        outgoing=outgoing,
                        save_to_sent=bool(account["save_to_sent"]),
                        sent_folder_name=account["sent_folder_name"],
                    )
                )
            try:
                allowed_recipients = json.loads(catalog["allowed_recipients_json"])
                allowed_senders = json.loads(catalog["allowed_senders_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ManagedCatalogError("Managed policy data is invalid") from exc
            if not isinstance(allowed_recipients, list) or not all(
                isinstance(item, str) for item in allowed_recipients
            ):
                raise ManagedCatalogError("Managed recipient policy is invalid")
            if not isinstance(allowed_senders, list) or not all(isinstance(item, str) for item in allowed_senders):
                raise ManagedCatalogError("Managed sender policy is invalid")

        return Settings.model_construct(
            emails=resolved,
            providers=[],
            db_location=self.path.as_posix(),
            enable_attachment_download=bool(catalog["enable_attachment_download"]),
            allowed_recipients=allowed_recipients,
            allowed_senders=allowed_senders,
            report_blocked_mutations=bool(catalog["report_blocked_mutations"]),
            credential_storage="keyring",
        )

    def _resolve_endpoint(self, endpoint: sqlite3.Row | None, binding: sqlite3.Row | None) -> EmailServer | None:
        if endpoint is None and binding is None:
            return None
        if endpoint is None or binding is None:
            raise ManagedCatalogError("Managed endpoint and credential binding are inconsistent")
        secret = self.secret_store.get(binding["opaque_locator"])
        return EmailServer(
            user_name=endpoint["user_name"],
            password=SecretStr(secret),
            host=endpoint["host"],
            port=endpoint["port"],
            use_ssl=bool(endpoint["use_ssl"]),
            start_ssl=bool(endpoint["start_ssl"]),
            verify_ssl=bool(endpoint["verify_ssl"]),
        )
