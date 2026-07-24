from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.config import EmailSettings
from mcp_email_server.managed import (
    _OPERATIONAL_DATABASE_SCHEMA,
    _OPERATIONAL_SCHEMA,
    _SCHEMA,
    SCHEMA_VERSION,
    ManagedCatalogError,
    _connect,
    _execute_schema,
    _expected_schema_objects,
    _prepare_new_file,
    _schema_objects,
    _validate_managed_schema,
    _validate_operational_schema,
)

IndexMode = Literal["legacy", "managed"]
MAX_PROJECTED_METADATA_ROWS = APPLICATION_LIMITS.metadata_snapshot_rows


class MetadataIndexError(RuntimeError):
    """The rebuildable metadata projection is unavailable or inconsistent."""


def _fail(message: str) -> NoReturn:
    raise MetadataIndexError(message)


@dataclass(frozen=True)
class IndexedMailboxSnapshot:
    uidvalidity: int
    uidnext: int
    message_count: int
    emails: tuple[dict[str, Any], ...]
    observed_at: datetime


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_snapshot_coverage(
    emails: list[dict[str, Any]],
    *,
    uidnext: int,
    message_count: int,
    complete: bool,
) -> list[int]:
    if len(emails) > MAX_PROJECTED_METADATA_ROWS:
        _fail(f"Metadata snapshot exceeds the {MAX_PROJECTED_METADATA_ROWS}-row retention bound")
    try:
        observed_uids = [int(email["email_id"]) for email in emails]
    except (KeyError, TypeError, ValueError) as exc:
        raise MetadataIndexError("Provider metadata UID is invalid") from exc
    if any(uid <= 0 for uid in observed_uids):
        _fail("Provider metadata UID is invalid")
    if len(set(observed_uids)) != len(observed_uids):
        _fail("Provider metadata contains duplicate UIDs")
    if complete and (len(observed_uids) != message_count or any(uid >= uidnext for uid in observed_uids)):
        _fail("Complete metadata coverage is inconsistent with provider state")
    return observed_uids


def _legacy_fingerprint(account: EmailSettings) -> str:
    source = {
        "account_name": account.account_name,
        "email_address": account.email_address,
        "incoming": {
            "host": account.incoming.host,
            "port": account.incoming.port,
            "start_ssl": account.incoming.start_ssl,
            "use_ssl": account.incoming.use_ssl,
            "user_name": account.incoming.user_name,
            "verify_ssl": account.incoming.verify_ssl,
        },
    }
    canonical = json.dumps(source, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _schema_version(connection: sqlite3.Connection) -> sqlite3.Row | None:
    try:
        return connection.execute("SELECT version FROM schema_metadata WHERE singleton = 1").fetchone()
    except sqlite3.Error:
        return None


def _has_schema_objects(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL LIMIT 1"""
        ).fetchone()
        is not None
    )


def _initialization_in_progress(connection: sqlite3.Connection, mode: IndexMode) -> bool:
    if mode != "legacy":
        return False
    actual = _schema_objects(connection)
    expected = _expected_schema_objects(_OPERATIONAL_DATABASE_SCHEMA)
    return bool(actual) and actual.items() <= expected.items() and ("table", "schema_metadata") in actual


def _preflight_schema_ownership(connection: sqlite3.Connection, mode: IndexMode) -> None:
    """Reject unrelated existing files before any persistent journal change."""
    row = _schema_version(connection)
    if row is None:
        if _has_schema_objects(connection) or mode == "managed":
            _fail("Operational database schema is missing or incompatible")
        return
    if (mode == "managed" and row["version"] != SCHEMA_VERSION) or (
        mode == "legacy" and row["version"] not in (1, SCHEMA_VERSION)
    ):
        _fail("Operational database schema version is unsupported")
    try:
        if mode == "managed":
            _validate_managed_schema(connection, _SCHEMA)
        else:
            _validate_operational_schema(connection)
    except ManagedCatalogError as exc:
        raise MetadataIndexError("Operational database schema is incomplete or incompatible") from exc


def _initialize(  # noqa: C901 - bounded initialization and ownership checks
    path: Path,
    mode: IndexMode,
) -> None:
    if not path.exists() and not path.is_symlink():
        try:
            _prepare_new_file(path)
        except ManagedCatalogError:
            if not path.exists():
                raise
    with _connect(path, enable_wal=False) as connection:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                _preflight_schema_ownership(connection, mode)
                break
            except MetadataIndexError:
                if not _initialization_in_progress(connection, mode) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    with _connect(path) as connection:
        # Serialize initialization, then repeat all ownership checks
        # under the write lock in case another first opener won the race.
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _schema_version(connection)
            if row is None:
                if _has_schema_objects(connection) or mode == "managed":
                    _fail("Operational database schema is missing or incompatible")
                connection.execute(
                    """CREATE TABLE schema_metadata (
                           singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                           version INTEGER NOT NULL
                       )"""
                )
                _execute_schema(connection, _OPERATIONAL_SCHEMA)
                connection.execute(
                    "INSERT INTO schema_metadata(singleton, version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
                row = _schema_version(connection)
                if row is None:
                    _fail("Operational database initialization did not complete")
            if (mode == "managed" and row["version"] != SCHEMA_VERSION) or (
                mode == "legacy" and row["version"] not in (1, SCHEMA_VERSION)
            ):
                _fail("Operational database schema version is unsupported")
            try:
                if mode == "managed":
                    _validate_managed_schema(connection, _SCHEMA)
                else:
                    _validate_operational_schema(connection)
            except ManagedCatalogError as exc:
                raise MetadataIndexError("Operational database schema is incomplete or incompatible") from exc
            if row["version"] == 1:
                connection.execute(
                    "UPDATE schema_metadata SET version = ? WHERE singleton = 1 AND version = 1",
                    (SCHEMA_VERSION,),
                )
            try:
                if mode == "managed":
                    _validate_managed_schema(connection, _SCHEMA)
                else:
                    _validate_operational_schema(connection)
            except ManagedCatalogError as exc:
                raise MetadataIndexError("Operational database schema is incomplete or incompatible") from exc
        except Exception:
            connection.rollback()
            raise
        connection.commit()


class MetadataIndex:
    """Small SQLite projection used only by the bounded metadata query."""

    def __init__(self, path: Path, mode: IndexMode) -> None:
        self.path = Path(os.path.abspath(path.expanduser()))
        self.mode: IndexMode = mode

    def ensure_ready(self) -> None:
        try:
            _initialize(self.path, self.mode)
        except MetadataIndexError:
            raise
        except ManagedCatalogError as exc:
            raise MetadataIndexError("Operational database is unavailable") from exc
        except sqlite3.Error as exc:
            raise MetadataIndexError("Operational database is unavailable") from exc

    def resolve_operational_account(self, account: EmailSettings) -> str:
        self.ensure_ready()
        try:
            with _connect(self.path) as connection:
                fingerprint: str | None = None
                if self.mode == "managed":
                    row = connection.execute(
                        "SELECT id FROM managed_account WHERE name = ? AND removed_at IS NULL",
                        (account.account_name,),
                    ).fetchone()
                    if row is None:
                        _fail("Managed operational account identity is unavailable")
                    source_reference = f"managed:{row['id']}"
                    source_kind = "managed"
                else:
                    fingerprint = _legacy_fingerprint(account)
                    source_reference = f"legacy:{fingerprint}"
                    source_kind = "legacy"
                operational_id = uuid.uuid5(uuid.NAMESPACE_URL, source_reference).hex
                now = _utc_text(datetime.now(UTC))
                connection.execute(
                    """INSERT INTO operational_account(id, source_kind, source_reference, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(source_reference) DO NOTHING""",
                    (operational_id, source_kind, source_reference, now),
                )
                persisted = connection.execute(
                    "SELECT id FROM operational_account WHERE source_reference = ?", (source_reference,)
                ).fetchone()
                if persisted is None:
                    _fail("Operational account identity could not be persisted")
                operational_id = persisted["id"]
                if self.mode == "legacy":
                    if fingerprint is None:
                        _fail("Legacy source identity is unavailable")
                    connection.execute(
                        """INSERT INTO legacy_source(source_fingerprint, operational_account_id)
                           VALUES (?, ?) ON CONFLICT(source_fingerprint) DO NOTHING""",
                        (fingerprint, operational_id),
                    )
                connection.commit()
                return operational_id
        except MetadataIndexError:
            raise
        except (ManagedCatalogError, sqlite3.Error) as exc:
            raise MetadataIndexError("Operational account identity is unavailable") from exc

    def read_complete(
        self,
        operational_account_id: str,
        mailbox: str,
        *,
        uidvalidity: int,
        uidnext: int,
        message_count: int,
    ) -> IndexedMailboxSnapshot | None:
        self.ensure_ready()
        try:
            with _connect(self.path) as connection:
                # Hold one WAL read snapshot across all keyset batches so a
                # concurrent refresh cannot mix coverage and message epochs.
                connection.execute("BEGIN")
                coverage = connection.execute(
                    """SELECT m.id mailbox_id, c.observed_at
                       FROM mailbox_projection m
                       JOIN index_coverage c ON c.mailbox_id = m.id
                       WHERE m.operational_account_id = ? AND m.remote_name = ?
                         AND m.uidvalidity = ? AND c.uidvalidity = ?
                         AND c.uidnext = ? AND c.message_count = ?
                         AND c.completeness = 'COMPLETE'""",
                    (operational_account_id, mailbox, uidvalidity, uidvalidity, uidnext, message_count),
                ).fetchone()
                if coverage is None:
                    return None
                emails: list[dict[str, Any]] = []
                cursor: int | None = None
                while True:
                    if cursor is None:
                        rows = connection.execute(
                            """SELECT * FROM message_metadata_projection
                               WHERE mailbox_id = ? AND uidvalidity = ?
                               ORDER BY uid DESC LIMIT 100""",
                            (coverage["mailbox_id"], uidvalidity),
                        ).fetchall()
                    else:
                        rows = connection.execute(
                            """SELECT * FROM message_metadata_projection
                               WHERE mailbox_id = ? AND uidvalidity = ? AND uid < ?
                               ORDER BY uid DESC LIMIT 100""",
                            (coverage["mailbox_id"], uidvalidity, cursor),
                        ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        recipients = json.loads(row["recipients_json"])
                        attachments = json.loads(row["attachment_names_json"])
                        flags = json.loads(row["flags_json"])
                        if not (
                            isinstance(recipients, list)
                            and all(isinstance(item, str) for item in recipients)
                            and isinstance(attachments, list)
                            and all(isinstance(item, str) for item in attachments)
                            and isinstance(flags, list)
                            and all(isinstance(item, str) for item in flags)
                        ):
                            _fail("Indexed metadata is malformed")
                        emails.append({
                            "email_id": str(row["uid"]),
                            "message_id": row["message_id"],
                            "subject": row["subject"],
                            "from": row["sender"],
                            "to": recipients,
                            "date": _parse_utc(row["message_date"]),
                            "attachments": attachments,
                            "_internal_date": _parse_utc(row["internal_date"]) if row["internal_date"] else None,
                            "_flags": flags,
                        })
                    cursor = rows[-1]["uid"]
                if len(emails) != message_count:
                    return None
                return IndexedMailboxSnapshot(
                    uidvalidity=uidvalidity,
                    uidnext=uidnext,
                    message_count=message_count,
                    emails=tuple(emails),
                    observed_at=_parse_utc(coverage["observed_at"]),
                )
        except MetadataIndexError:
            raise
        except (ManagedCatalogError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MetadataIndexError("Indexed metadata is unavailable") from exc

    def invalidate_mailboxes(self, operational_account_id: str, mailboxes: tuple[str, ...]) -> None:
        """Mark mailbox coverage stale without rewriting provider outcome evidence."""
        self.ensure_ready()
        if not mailboxes:
            return
        try:
            with _connect(self.path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in mailboxes)
                connection.execute(
                    f"""DELETE FROM index_coverage
                        WHERE mailbox_id IN (
                            SELECT id FROM mailbox_projection
                            WHERE operational_account_id = ? AND remote_name IN ({placeholders})
                        )""",  # noqa: S608 - placeholders are generated, values remain bound
                    (operational_account_id, *mailboxes),
                )
                connection.commit()
        except MetadataIndexError:
            raise
        except (ManagedCatalogError, sqlite3.Error) as exc:
            raise MetadataIndexError("Metadata projection could not be invalidated") from exc

    def write_snapshot(
        self,
        operational_account_id: str,
        mailbox: str,
        *,
        delimiter: str | None,
        attributes: list[str],
        uidvalidity: int,
        uidnext: int,
        message_count: int,
        emails: list[dict[str, Any]],
        complete: bool,
        observed_at: datetime,
    ) -> None:
        self.ensure_ready()
        observed_uids = _validate_snapshot_coverage(
            emails,
            uidnext=uidnext,
            message_count=message_count,
            complete=complete,
        )
        mailbox_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{operational_account_id}:{mailbox}").hex
        timestamp = _utc_text(observed_at)
        try:
            with _connect(self.path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    """SELECT id, uidvalidity FROM mailbox_projection
                       WHERE operational_account_id = ? AND remote_name = ?""",
                    (operational_account_id, mailbox),
                ).fetchone()
                if previous is not None and previous["uidvalidity"] != uidvalidity:
                    connection.execute(
                        "DELETE FROM message_metadata_projection WHERE mailbox_id = ?", (previous["id"],)
                    )
                    connection.execute("DELETE FROM index_coverage WHERE mailbox_id = ?", (previous["id"],))
                connection.execute(
                    """INSERT INTO mailbox_projection(
                           id, operational_account_id, remote_name, delimiter, attributes_json, uidvalidity, observed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(operational_account_id, remote_name) DO UPDATE SET
                           delimiter = excluded.delimiter,
                           attributes_json = excluded.attributes_json,
                           uidvalidity = excluded.uidvalidity,
                           observed_at = excluded.observed_at""",
                    (
                        mailbox_id,
                        operational_account_id,
                        mailbox,
                        delimiter,
                        json.dumps(attributes, separators=(",", ":")),
                        uidvalidity,
                        timestamp,
                    ),
                )
                active_mailbox = connection.execute(
                    "SELECT id FROM mailbox_projection WHERE operational_account_id = ? AND remote_name = ?",
                    (operational_account_id, mailbox),
                ).fetchone()
                if active_mailbox is None:
                    _fail("Mailbox projection could not be persisted")
                mailbox_id = active_mailbox["id"]
                for email, uid in zip(emails, observed_uids, strict=True):
                    message_date = email["date"]
                    internal_date = email.get("_internal_date")
                    if not isinstance(message_date, datetime):
                        _fail("Provider metadata date is invalid")
                    if internal_date is not None and not isinstance(internal_date, datetime):
                        _fail("Provider internal date is invalid")
                    connection.execute(
                        """INSERT INTO message_metadata_projection(
                               mailbox_id, uidvalidity, uid, message_id, subject, sender,
                               recipients_json, message_date, internal_date, attachment_names_json,
                               flags_json, observed_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(mailbox_id, uidvalidity, uid) DO UPDATE SET
                               message_id = excluded.message_id,
                               subject = excluded.subject,
                               sender = excluded.sender,
                               recipients_json = excluded.recipients_json,
                               message_date = excluded.message_date,
                               internal_date = excluded.internal_date,
                               attachment_names_json = excluded.attachment_names_json,
                               flags_json = excluded.flags_json,
                               observed_at = excluded.observed_at""",
                        (
                            mailbox_id,
                            uidvalidity,
                            uid,
                            email.get("message_id"),
                            str(email.get("subject", "")),
                            str(email.get("from", "")),
                            json.dumps(email.get("to", []), separators=(",", ":")),
                            _utc_text(message_date),
                            _utc_text(internal_date) if internal_date is not None else None,
                            json.dumps(email.get("attachments", []), separators=(",", ":")),
                            json.dumps(email.get("_flags", []), separators=(",", ":")),
                            timestamp,
                        ),
                    )
                # Retention eviction is not a provider-removal claim. Keep only
                # the currently observed local window even when coverage is partial.
                if observed_uids:
                    placeholders = ",".join("?" for _ in observed_uids)
                    connection.execute(
                        f"""DELETE FROM message_metadata_projection
                            WHERE mailbox_id = ? AND uidvalidity = ? AND uid NOT IN ({placeholders})""",  # noqa: S608
                        (mailbox_id, uidvalidity, *observed_uids),
                    )
                else:
                    connection.execute(
                        "DELETE FROM message_metadata_projection WHERE mailbox_id = ? AND uidvalidity = ?",
                        (mailbox_id, uidvalidity),
                    )
                low_uid = min(observed_uids) if observed_uids else None
                high_uid = max(observed_uids) if observed_uids else None
                connection.execute(
                    """INSERT INTO index_coverage(
                           mailbox_id, uidvalidity, uidnext, message_count, low_uid, high_uid, completeness, observed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(mailbox_id) DO UPDATE SET
                           uidvalidity = excluded.uidvalidity,
                           uidnext = excluded.uidnext,
                           message_count = excluded.message_count,
                           low_uid = excluded.low_uid,
                           high_uid = excluded.high_uid,
                           completeness = excluded.completeness,
                           observed_at = excluded.observed_at""",
                    (
                        mailbox_id,
                        uidvalidity,
                        uidnext,
                        message_count,
                        low_uid,
                        high_uid,
                        "COMPLETE" if complete else "PARTIAL",
                        timestamp,
                    ),
                )
                connection.commit()
        except MetadataIndexError:
            raise
        except (ManagedCatalogError, sqlite3.Error, ValueError, TypeError) as exc:
            raise MetadataIndexError("Metadata snapshot could not be persisted") from exc
