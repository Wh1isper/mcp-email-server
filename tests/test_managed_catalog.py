from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.managed import (
    SQLITE_BUSY_TIMEOUT_MS,
    WAL_RETRY_BUSY_TIMEOUT_MS,
    ManagedCatalog,
    ManagedCatalogError,
    ManagedCatalogSecurityError,
    _enable_wal,
)


class FakeSecretStore:
    def __init__(self, on_put: Callable[[str, str], None] | None = None) -> None:
        self.values: dict[str, str] = {}
        self.on_put = on_put
        self.put_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self.fail_put = False
        self.fail_delete = False
        self.fail_get = False

    def put(self, locator: str, value: str) -> None:
        self.put_calls.append((locator, value))
        if self.on_put is not None:
            self.on_put(locator, value)
        if self.fail_put:
            raise ManagedCatalogError("backend unavailable")
        assert locator not in self.values
        self.values[locator] = value

    def get(self, locator: str) -> str:
        if self.fail_get:
            raise ManagedCatalogError("backend unavailable")
        try:
            return self.values[locator]
        except KeyError as exc:
            raise ManagedCatalogError("missing") from exc

    def delete(self, locator: str) -> bool:
        self.delete_calls.append(locator)
        if self.fail_delete:
            return False
        self.values.pop(locator, None)
        return True


def _server(*, host: str = "imap.example.test") -> EmailServer:
    return EmailServer(
        user_name="alice@example.test",
        password="not-persisted",
        host=host,
        port=993,
        use_ssl=True,
    )


def _catalog(tmp_path: Path, store: FakeSecretStore | None = None) -> ManagedCatalog:
    parent = tmp_path / "managed"
    parent.mkdir(mode=0o700)
    if os.name == "posix":
        parent.chmod(0o700)
    initialized = ManagedCatalog.initialize(parent / "catalog.sqlite3")
    return ManagedCatalog(initialized.path, secret_store=store) if store is not None else initialized


def _add_account(catalog: ManagedCatalog, *, outgoing: bool = False) -> None:
    catalog.add_account(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=_server(),
        outgoing=_server(host="smtp.example.test") if outgoing else None,
    )


def test_initialize_creates_minimal_private_staging_catalog(tmp_path):
    catalog = _catalog(tmp_path)

    assert catalog.lifecycle() == "STAGING"
    with closing(sqlite3.connect(catalog.path)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert tables == {
        "schema_metadata",
        "catalog",
        "managed_account",
        "endpoint",
        "secret_binding",
        "operational_account",
        "legacy_source",
        "mailbox_projection",
        "message_metadata_projection",
        "index_coverage",
    }
    assert journal_mode.lower() == "wal"
    assert busy_timeout <= 5000
    if os.name == "posix":
        assert stat.S_IMODE(catalog.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(catalog.path.parent.stat().st_mode) == 0o700


def test_add_and_activate_round_trip_never_persists_secret(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)

    catalog.set_secret("alice", "incoming", "super-secret-password")
    catalog.activate()
    settings = catalog.load_settings()

    account = settings.emails[0]
    assert account.account_name == "alice"
    assert account.incoming.password.get_secret_value() == "super-secret-password"
    assert b"super-secret-password" not in catalog.path.read_bytes()
    assert catalog.lifecycle() == "ACTIVE"


def test_activation_requires_complete_enabled_account(tmp_path):
    catalog = _catalog(tmp_path, FakeSecretStore())
    _add_account(catalog)

    with pytest.raises(ManagedCatalogError, match="incomplete"):
        catalog.activate()

    assert catalog.lifecycle() == "STAGING"


def test_one_account_can_be_resolved_while_another_is_incomplete(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "alice-secret")
    catalog.add_account(
        name="bob",
        full_name="Bob",
        email_address="bob@example.test",
        incoming=EmailServer(
            user_name="bob@example.test",
            password="not-persisted",
            host="imap.example.test",
            port=993,
        ),
        outgoing=None,
    )

    account = catalog.load_account("alice")

    assert account.account_name == "alice"
    assert account.incoming.password.get_secret_value() == "alice-secret"


def test_outgoing_endpoint_requires_active_outgoing_binding(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog, outgoing=True)
    catalog.set_secret("alice", "incoming", "incoming-secret")

    with pytest.raises(ManagedCatalogError, match="outgoing"):
        catalog.activate()

    catalog.set_secret("alice", "outgoing", "outgoing-secret")
    catalog.activate()
    account = catalog.load_settings().emails[0]
    assert account.outgoing is not None
    assert account.outgoing.password.get_secret_value() == "outgoing-secret"


def test_pending_binding_is_committed_before_external_secret_write(tmp_path):
    observed: list[tuple[str, bool]] = []
    catalog_holder: list[ManagedCatalog] = []

    def observe(locator: str, _value: str) -> None:
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            row = connection.execute(
                "SELECT status, opaque_locator FROM secret_binding WHERE opaque_locator = ?", (locator,)
            ).fetchone()
        observed.append((row[0], row[1] == locator))

    store = FakeSecretStore(on_put=observe)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)

    catalog.set_secret("alice", "incoming", "secret")

    assert observed == [("PENDING", True)]


def test_secret_write_failure_leaves_repairable_pending_binding(tmp_path):
    store = FakeSecretStore()
    store.fail_put = True
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)

    with pytest.raises(ManagedCatalogError, match="backend unavailable"):
        catalog.set_secret("alice", "incoming", "secret")

    report = catalog.doctor()
    assert report.pending_bindings == 1
    assert report.problems == ("account_incomplete:alice:incoming",)


def test_revision_conflict_cleans_candidate_and_preserves_no_active_binding(tmp_path):
    catalog_holder: list[ManagedCatalog] = []

    def race(_locator: str, _value: str) -> None:
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            connection.execute("UPDATE managed_account SET revision = revision + 1 WHERE name = 'alice'")
            connection.commit()

    store = FakeSecretStore(on_put=race)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)

    with pytest.raises(ManagedCatalogError, match="changed"):
        catalog.set_secret("alice", "incoming", "secret")

    assert store.values == {}
    assert len(store.delete_calls) == 1
    assert catalog.list_accounts()[0].incoming_binding == "SUPERSEDED"
    assert catalog.doctor().pending_bindings == 0


def test_successful_retry_retires_pending_binding_from_failed_write(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    store.fail_put = True
    with pytest.raises(ManagedCatalogError):
        catalog.set_secret("alice", "incoming", "first-secret")
    store.fail_put = False

    catalog.set_secret("alice", "incoming", "replacement-secret")

    assert catalog.doctor().pending_bindings == 0
    assert catalog.list_accounts()[0].incoming_binding == "ACTIVE"


def test_rotation_activates_new_secret_before_old_cleanup_and_reports_failure(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "old-secret")
    old_locator = next(iter(store.values))
    store.fail_delete = True

    catalog.set_secret("alice", "incoming", "new-secret")

    settings = catalog.load_settings(require_active=False)
    assert settings.emails[0].incoming.password.get_secret_value() == "new-secret"
    assert old_locator in store.delete_calls
    assert catalog.doctor().cleanup_required_bindings == 1


def test_disabled_account_is_excluded_from_runtime_without_secret_access(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    catalog.activate()
    catalog.disable_account("alice")
    store.fail_get = True

    settings = catalog.load_settings()

    assert settings.emails == []


def test_missing_or_unreadable_active_secret_fails_closed(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    catalog.activate()
    store.values.clear()

    with pytest.raises(ManagedCatalogError, match="missing"):
        catalog.load_settings()


def test_active_catalog_rejects_new_account_without_harming_existing_snapshot(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    catalog.activate()

    with pytest.raises(ManagedCatalogError, match="STAGING"):
        catalog.add_account(
            name="incomplete",
            full_name="Incomplete",
            email_address="incomplete@example.test",
            incoming=_server(),
            outgoing=None,
        )

    assert [account.account_name for account in catalog.load_settings().emails] == ["alice"]


def test_rotation_crash_before_cleanup_remains_doctor_visible(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "old-secret")

    class SimulatedCrash(BaseException):
        pass

    def crash_during_cleanup(_locator: str) -> bool:
        raise SimulatedCrash

    store.delete = crash_during_cleanup  # type: ignore[method-assign]
    with pytest.raises(SimulatedCrash):
        catalog.set_secret("alice", "incoming", "new-secret")

    assert catalog.load_settings(require_active=False).emails[0].incoming.password.get_secret_value() == "new-secret"
    assert catalog.doctor().cleanup_required_bindings == 1


def test_doctor_reports_unavailable_active_secret_without_locator(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    locator = next(iter(store.values))
    store.values.clear()

    report = catalog.doctor()

    assert report.problems == ("active_secret_unavailable:alice:incoming",)
    assert locator not in str(report)


def test_duplicate_account_name_is_rejected(tmp_path):
    catalog = _catalog(tmp_path, FakeSecretStore())
    _add_account(catalog)

    with pytest.raises(ManagedCatalogError, match="already exists"):
        _add_account(catalog)


def test_insecure_database_permissions_are_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    if os.name != "posix":
        pytest.skip("POSIX permission contract")
    catalog.path.chmod(0o644)

    with pytest.raises(ManagedCatalogSecurityError, match="owner-only"):
        catalog.lifecycle()


def test_database_symlink_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    link = catalog.path.parent / "link.sqlite3"
    try:
        link.symlink_to(catalog.path)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ManagedCatalogSecurityError, match="symlink"):
        ManagedCatalog(link).lifecycle()


def test_concurrent_readers_do_not_fail_on_sidecar_shutdown_races(tmp_path):
    catalog = _catalog(tmp_path)

    def read_lifecycle(_index: int) -> str:
        result = ""
        for _ in range(20):
            result = catalog.lifecycle()
        return result

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(read_lifecycle, range(8)))

    assert results == ["STAGING"] * 8


def test_wal_retry_uses_one_wall_clock_busy_deadline() -> None:
    clock = [0.0]
    calls: list[str] = []

    class LockedConnection:
        def execute(self, statement: str):
            calls.append(statement)
            if statement == "PRAGMA journal_mode":
                clock[0] += WAL_RETRY_BUSY_TIMEOUT_MS / 1_000
                raise sqlite3.OperationalError("database is locked")
            return self

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    with (
        patch("mcp_email_server.managed.time.monotonic", side_effect=monotonic),
        patch("mcp_email_server.managed.time.sleep", side_effect=sleep),
        pytest.raises(sqlite3.OperationalError, match="locked"),
    ):
        _enable_wal(LockedConnection())  # type: ignore[arg-type]

    assert SQLITE_BUSY_TIMEOUT_MS / 1_000 <= clock[0] <= SQLITE_BUSY_TIMEOUT_MS / 1_000 + 0.2
    assert calls[0] == f"PRAGMA busy_timeout = {WAL_RETRY_BUSY_TIMEOUT_MS}"
    assert calls[-1] == f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}"


def test_corrupt_database_is_rejected_with_bounded_error(tmp_path):
    parent = tmp_path / "managed"
    parent.mkdir(mode=0o700)
    path = parent / "catalog.sqlite3"
    path.write_bytes(b"not a sqlite database")
    path.chmod(0o600)

    with pytest.raises(ManagedCatalogError, match=r"corrupt|unavailable"):
        ManagedCatalog(path).lifecycle()


def test_unrelated_managed_database_is_rejected_before_enabling_wal(tmp_path: Path) -> None:
    parent = tmp_path / "managed"
    parent.mkdir(mode=0o700)
    path = parent / "unrelated.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    path.chmod(0o600)
    original_bytes = path.read_bytes()

    with pytest.raises(ManagedCatalogError, match="schema"):
        ManagedCatalog(path).lifecycle()

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert path.read_bytes() == original_bytes
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
