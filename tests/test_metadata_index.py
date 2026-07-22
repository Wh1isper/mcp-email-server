from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.managed import SCHEMA_VERSION, ManagedCatalog, ManagedCatalogError
from mcp_email_server.metadata_index import MetadataIndex, MetadataIndexError

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


def _account(password: str | None = None) -> EmailSettings:
    return EmailSettings(
        account_name="work",
        full_name="Work User",
        email_address="user@example.test",
        incoming=EmailServer(
            user_name="user@example.test",
            password=SecretStr(password or "not-persisted"),
            host="imap.example.test",
            port=993,
        ),
    )


def _email(uid: int, *, subject: str | None = None) -> dict[str, object]:
    date = NOW + timedelta(minutes=uid)
    return {
        "email_id": str(uid),
        "message_id": f"<{uid}@example.test>",
        "subject": subject or f"Subject {uid}",
        "from": "sender@example.test",
        "to": ["user@example.test"],
        "date": date,
        "attachments": [],
        "_internal_date": date,
        "_flags": ["\\Seen"] if uid % 2 else [],
    }


def test_legacy_index_uses_non_secret_stable_mapping_and_keyset_read(tmp_path: Path) -> None:
    path = tmp_path / "private" / "operational.sqlite3"
    index = MetadataIndex(path, "legacy")
    first_id = index.resolve_operational_account(_account("first-secret"))
    second_id = index.resolve_operational_account(_account("rotated-secret"))
    assert first_id == second_id

    emails = [_email(uid) for uid in range(1, 251)]
    index.write_snapshot(
        first_id,
        "INBOX",
        delimiter="/",
        attributes=["\\Inbox"],
        uidvalidity=101,
        uidnext=251,
        message_count=250,
        emails=emails,
        complete=True,
        observed_at=NOW,
    )
    snapshot = index.read_complete(
        first_id,
        "INBOX",
        uidvalidity=101,
        uidnext=251,
        message_count=250,
    )
    assert snapshot is not None
    assert [email["email_id"] for email in snapshot.emails] == [str(uid) for uid in range(250, 0, -1)]
    assert (
        index.read_complete(
            first_id,
            "INBOX",
            uidvalidity=101,
            uidnext=252,
            message_count=250,
        )
        is None
    )

    raw = path.read_bytes()
    assert b"first-secret" not in raw
    assert b"rotated-secret" not in raw
    with closing(sqlite3.connect(path)) as connection:
        source = connection.execute("SELECT source_fingerprint FROM legacy_source").fetchone()[0]
    assert len(source) == 64
    assert "user@example.test" not in source


def test_concurrent_first_open_converges_on_one_operational_identity(tmp_path: Path) -> None:
    path = tmp_path / "private" / "operational.sqlite3"

    def resolve() -> str:
        return MetadataIndex(path, "legacy").resolve_operational_account(_account())

    with ThreadPoolExecutor(max_workers=8) as executor:
        account_ids = list(executor.map(lambda _item: resolve(), range(8)))

    assert len(set(account_ids)) == 1
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operational_account").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == SCHEMA_VERSION


def test_partial_refresh_evicts_outside_local_window_without_claiming_complete_coverage(tmp_path: Path) -> None:
    index = MetadataIndex(tmp_path / "private" / "operational.sqlite3", "legacy")
    account_id = index.resolve_operational_account(_account())
    index.write_snapshot(
        account_id,
        "INBOX",
        delimiter="/",
        attributes=[],
        uidvalidity=10,
        uidnext=4,
        message_count=3,
        emails=[_email(1), _email(2), _email(3)],
        complete=True,
        observed_at=NOW,
    )
    index.write_snapshot(
        account_id,
        "INBOX",
        delimiter="/",
        attributes=[],
        uidvalidity=10,
        uidnext=5,
        message_count=4,
        emails=[_email(4)],
        complete=False,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert (
        index.read_complete(
            account_id,
            "INBOX",
            uidvalidity=10,
            uidnext=5,
            message_count=4,
        )
        is None
    )
    with closing(sqlite3.connect(index.path)) as connection:
        assert connection.execute("SELECT uid FROM message_metadata_projection").fetchall() == [(4,)]
        assert connection.execute("SELECT completeness FROM index_coverage").fetchone()[0] == "PARTIAL"


def test_successive_partial_windows_keep_projection_at_retention_bound(tmp_path: Path) -> None:
    index = MetadataIndex(tmp_path / "private" / "operational.sqlite3", "legacy")
    account_id = index.resolve_operational_account(_account())
    for start, uidnext in ((1, 1001), (501, 1501)):
        index.write_snapshot(
            account_id,
            "INBOX",
            delimiter="/",
            attributes=[],
            uidvalidity=10,
            uidnext=uidnext,
            message_count=2_000,
            emails=[_email(uid) for uid in range(start, start + 1_000)],
            complete=False,
            observed_at=NOW,
        )

    with closing(sqlite3.connect(index.path)) as connection:
        count, low_uid, high_uid = connection.execute(
            "SELECT COUNT(*), MIN(uid), MAX(uid) FROM message_metadata_projection"
        ).fetchone()
    assert (count, low_uid, high_uid) == (1_000, 501, 1_500)


@pytest.mark.parametrize(
    "emails, uidnext, message_count, complete, message",
    [
        ([_email(uid) for uid in range(1, 1_002)], 1_002, 1_001, False, "retention bound"),
        ([_email(1), _email(1)], 2, 2, True, "duplicate UIDs"),
        ([_email(1)], 2, 2, True, "inconsistent with provider state"),
        ([_email(2)], 2, 1, True, "inconsistent with provider state"),
    ],
)
def test_snapshot_write_rejects_unbounded_or_inconsistent_coverage(
    tmp_path: Path,
    emails: list[dict[str, object]],
    uidnext: int,
    message_count: int,
    complete: bool,
    message: str,
) -> None:
    index = MetadataIndex(tmp_path / "private" / "operational.sqlite3", "legacy")
    account_id = index.resolve_operational_account(_account())

    with pytest.raises(MetadataIndexError, match=message):
        index.write_snapshot(
            account_id,
            "INBOX",
            delimiter="/",
            attributes=[],
            uidvalidity=10,
            uidnext=uidnext,
            message_count=message_count,
            emails=emails,
            complete=complete,
            observed_at=NOW,
        )

    with closing(sqlite3.connect(index.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM message_metadata_projection").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM index_coverage").fetchone()[0] == 0


def test_uidvalidity_change_invalidates_old_projection_before_partial_write(tmp_path: Path) -> None:
    index = MetadataIndex(tmp_path / "private" / "operational.sqlite3", "legacy")
    account_id = index.resolve_operational_account(_account())
    index.write_snapshot(
        account_id,
        "INBOX",
        delimiter="/",
        attributes=[],
        uidvalidity=10,
        uidnext=3,
        message_count=2,
        emails=[_email(1), _email(2)],
        complete=True,
        observed_at=NOW,
    )
    index.write_snapshot(
        account_id,
        "INBOX",
        delimiter="/",
        attributes=[],
        uidvalidity=11,
        uidnext=2,
        message_count=1,
        emails=[_email(1, subject="New epoch")],
        complete=False,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert (
        index.read_complete(
            account_id,
            "INBOX",
            uidvalidity=10,
            uidnext=3,
            message_count=2,
        )
        is None
    )
    with closing(sqlite3.connect(index.path)) as connection:
        rows = connection.execute("SELECT uidvalidity, uid, subject FROM message_metadata_projection").fetchall()
    assert rows == [(11, 1, "New epoch")]


def test_operational_index_rejects_insecure_parent_before_creation(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX permission contract")
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    index = MetadataIndex(parent / "operational.sqlite3", "legacy")

    with pytest.raises(MetadataIndexError, match="unavailable"):
        index.ensure_ready()
    assert not index.path.exists()


def test_unmarked_unrelated_database_is_not_claimed_or_mutated(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "unrelated.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE operational_account (unrelated TEXT)")
        connection.commit()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    path.chmod(0o600)
    original_bytes = path.read_bytes()
    index = MetadataIndex(path, "legacy")

    with pytest.raises(MetadataIndexError, match="missing or incompatible"):
        index.ensure_ready()
    with closing(sqlite3.connect(path)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert tables == {"operational_account"}
    assert journal_mode.lower() == "delete"
    assert path.read_bytes() == original_bytes
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_managed_mode_never_claims_an_empty_database_without_catalog_authority(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "empty.sqlite3"
    with closing(sqlite3.connect(path)):
        pass
    path.chmod(0o600)

    with pytest.raises(MetadataIndexError, match="missing or incompatible"):
        MetadataIndex(path, "managed").ensure_ready()
    with closing(sqlite3.connect(path)) as connection:
        objects = connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert objects == []
    assert journal_mode.lower() == "delete"
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_unmarked_view_only_database_is_not_claimed_or_mutated(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "unrelated.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE VIEW unrelated AS SELECT 1 AS value")
        connection.commit()
    path.chmod(0o600)

    with pytest.raises(MetadataIndexError, match="missing or incompatible"):
        MetadataIndex(path, "legacy").ensure_ready()
    with closing(sqlite3.connect(path)) as connection:
        objects = connection.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    assert objects == [("view", "unrelated")]


def test_legacy_version_marker_must_match_canonical_definition(tmp_path: Path) -> None:
    path = tmp_path / "private" / "operational.sqlite3"
    index = MetadataIndex(path, "legacy")
    index.ensure_ready()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE schema_metadata")
        connection.execute("CREATE TABLE schema_metadata(singleton INTEGER, version INTEGER)")
        connection.execute("INSERT INTO schema_metadata VALUES (1, ?)", (SCHEMA_VERSION,))
        connection.commit()

    with pytest.raises(MetadataIndexError, match="incomplete or incompatible"):
        index.ensure_ready()


def test_coincidental_version_one_schema_is_not_migrated(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "unrelated.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE schema_metadata(singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_metadata VALUES (1, 1)")
        connection.execute("CREATE TABLE catalog(id TEXT PRIMARY KEY)")
        connection.commit()
    path.chmod(0o600)

    with pytest.raises(MetadataIndexError, match="version is unsupported"):
        MetadataIndex(path, "legacy").ensure_ready()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == 1
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"schema_metadata", "catalog"}


@pytest.mark.parametrize(
    "extra_schema",
    [
        "CREATE TABLE unrelated(value TEXT)",
        "CREATE VIEW unrelated AS SELECT 1 AS value",
        "CREATE TRIGGER unrelated AFTER INSERT ON operational_account BEGIN SELECT 1; END",
    ],
)
def test_legacy_operational_database_rejects_extra_schema_objects(
    tmp_path: Path,
    extra_schema: str,
) -> None:
    path = tmp_path / "private" / "operational.sqlite3"
    index = MetadataIndex(path, "legacy")
    index.ensure_ready()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(extra_schema)
        connection.commit()

    with pytest.raises(MetadataIndexError, match="incomplete or incompatible"):
        index.ensure_ready()


def test_corrupt_legacy_operational_database_is_rejected_without_replacement(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "operational.sqlite3"
    path.write_bytes(b"not a sqlite database")
    path.chmod(0o600)
    index = MetadataIndex(path, "legacy")

    with pytest.raises(MetadataIndexError, match="unavailable"):
        index.ensure_ready()
    assert path.read_bytes() == b"not a sqlite database"


def test_managed_version_two_missing_operational_table_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "private" / "catalog.sqlite3"
    catalog = ManagedCatalog.initialize(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE index_coverage")
        connection.commit()

    with pytest.raises(ManagedCatalogError, match="schema"):
        catalog.lifecycle()


def test_managed_version_two_rejects_table_with_same_columns_but_missing_constraints(tmp_path: Path) -> None:
    path = tmp_path / "private" / "catalog.sqlite3"
    catalog = ManagedCatalog.initialize(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE index_coverage")
        connection.execute(
            """CREATE TABLE index_coverage (
                   mailbox_id TEXT,
                   uidvalidity INTEGER,
                   uidnext INTEGER,
                   message_count INTEGER,
                   low_uid INTEGER,
                   high_uid INTEGER,
                   completeness TEXT,
                   observed_at TEXT
               )"""
        )
        connection.commit()

    with pytest.raises(ManagedCatalogError, match="schema"):
        catalog.lifecycle()


def test_failed_version_one_migration_rolls_back_schema_and_version(tmp_path: Path) -> None:
    path = tmp_path / "private" / "catalog.sqlite3"
    catalog = ManagedCatalog.initialize(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE index_coverage")
        connection.execute("CREATE TABLE index_coverage (mailbox_id TEXT PRIMARY KEY)")
        connection.execute("UPDATE schema_metadata SET version = 1")
        connection.commit()

    with pytest.raises(ManagedCatalogError, match="schema"):
        catalog.lifecycle()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == 1
        columns = [row[1] for row in connection.execute("PRAGMA table_info(index_coverage)")]
    assert columns == ["mailbox_id"]


def test_managed_version_two_rejects_same_name_index_with_wrong_shape(tmp_path: Path) -> None:
    path = tmp_path / "private" / "catalog.sqlite3"
    catalog = ManagedCatalog.initialize(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP INDEX metadata_projection_uid_desc")
        connection.execute("CREATE INDEX metadata_projection_uid_desc ON message_metadata_projection(uid ASC)")
        connection.commit()

    with pytest.raises(ManagedCatalogError, match="schema"):
        catalog.lifecycle()


def test_version_one_managed_catalog_migrates_once_and_preserves_catalog_rows(tmp_path: Path) -> None:
    path = tmp_path / "private" / "catalog.sqlite3"
    catalog = ManagedCatalog.initialize(path)
    with closing(sqlite3.connect(path)) as connection:
        for table in (
            "index_coverage",
            "message_metadata_projection",
            "mailbox_projection",
            "legacy_source",
            "operational_account",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_metadata SET version = 1")
        connection.commit()

    assert catalog.lifecycle() == "STAGING"
    assert catalog.lifecycle() == "STAGING"
    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute("SELECT version FROM schema_metadata").fetchone()[0]
        lifecycle = connection.execute("SELECT lifecycle FROM catalog").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert version == SCHEMA_VERSION
    assert lifecycle == "STAGING"
    assert "index_coverage" in tables
