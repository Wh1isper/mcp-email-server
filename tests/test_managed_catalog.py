from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
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
    def __init__(
        self,
        on_put: Callable[[str, str], None] | None = None,
        on_get: Callable[[str], None] | None = None,
        on_delete: Callable[[str], None] | None = None,
    ) -> None:
        self.values: dict[str, str] = {}
        self.on_put = on_put
        self.on_get = on_get
        self.on_delete = on_delete
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
        if self.on_get is not None:
            self.on_get(locator)
        if self.fail_get:
            raise ManagedCatalogError("backend unavailable")
        try:
            return self.values[locator]
        except KeyError as exc:
            raise ManagedCatalogError("missing") from exc

    def delete(self, locator: str) -> bool:
        self.delete_calls.append(locator)
        if self.on_delete is not None:
            self.on_delete(locator)
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


def test_successful_retry_claims_pending_binding_before_external_cleanup(tmp_path):
    observed_statuses: list[str] = []
    catalog_holder: list[ManagedCatalog] = []

    def observe_delete(locator: str) -> None:
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            status = connection.execute(
                "SELECT status FROM secret_binding WHERE opaque_locator = ?",
                (locator,),
            ).fetchone()[0]
        observed_statuses.append(status)

    store = FakeSecretStore(on_delete=observe_delete)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)
    store.fail_put = True
    with pytest.raises(ManagedCatalogError):
        catalog.set_secret("alice", "incoming", "first-secret")
    store.fail_put = False

    catalog.set_secret("alice", "incoming", "replacement-secret")

    assert observed_statuses == ["CLEANUP_REQUIRED"]
    assert catalog.doctor().pending_bindings == 0
    assert catalog.list_accounts()[0].incoming_binding == "ACTIVE"


def test_concurrent_rotation_cannot_activate_a_candidate_claimed_for_cleanup(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "initial-secret")
    candidate_started = Event()
    release_candidate = Event()

    def block_candidate(_locator: str, value: str) -> None:
        if value != "candidate-b":
            return
        candidate_started.set()
        assert release_candidate.wait(timeout=5)

    store.on_put = block_candidate
    with ThreadPoolExecutor(max_workers=2) as executor:
        candidate_b = executor.submit(catalog.set_secret, "alice", "incoming", "candidate-b")
        assert candidate_started.wait(timeout=5)
        catalog.set_secret("alice", "incoming", "candidate-a")
        release_candidate.set()
        with pytest.raises(ManagedCatalogError, match="changed"):
            candidate_b.result(timeout=5)

    settings = catalog.load_settings(require_active=False)
    assert settings.emails[0].incoming.password.get_secret_value() == "candidate-a"
    assert catalog.doctor().pending_bindings == 0
    assert catalog.doctor().cleanup_required_bindings == 0


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
    catalog.disable_account("alice", expected_revision=2)
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


def test_active_account_update_is_revision_guarded_and_invalidates_remote_projection(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    catalog.activate()
    account_id, revision = catalog.account_revision("alice")
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.execute(
            "INSERT INTO operational_account VALUES (?, 'managed', ?, '2026-07-22T00:00:00+00:00')",
            ("operational-alice", f"managed:{account_id}"),
        )
        connection.execute(
            """INSERT INTO mailbox_projection
               VALUES ('mailbox-inbox', 'operational-alice', 'INBOX', '/', '[]', 1, '2026-07-22T00:00:00+00:00')"""
        )
        connection.commit()

    updated_revision = catalog.update_account(
        "alice",
        expected_revision=revision,
        new_name="alice-renamed",
        full_name="Alice Updated",
        email_address="updated@example.test",
        incoming=_server(host="imap-new.example.test"),
        save_to_sent=False,
        sent_folder_name="Sent Items",
        update_sent_folder=True,
    )

    details = catalog.show_account("alice-renamed")
    assert updated_revision == revision + 1
    assert details.revision == updated_revision
    assert details.full_name == "Alice Updated"
    assert details.email_address == "updated@example.test"
    assert details.incoming.host == "imap-new.example.test"
    assert details.save_to_sent is False
    assert details.sent_folder_name == "Sent Items"
    assert (
        catalog.load_account("alice-renamed", require_active_catalog=True).incoming.password.get_secret_value()
        == "secret"
    )
    with closing(sqlite3.connect(catalog.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM mailbox_projection").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM operational_account").fetchone()[0] == 1
    with pytest.raises(ManagedCatalogError, match="revision changed"):
        catalog.update_account("alice-renamed", expected_revision=revision, full_name="Stale")


def test_enabled_update_cannot_add_unbound_outgoing_endpoint(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    catalog.activate()
    revision = catalog.show_account("alice").revision

    with pytest.raises(ManagedCatalogError, match="incomplete"):
        catalog.update_account(
            "alice",
            expected_revision=revision,
            outgoing=_server(host="smtp.example.test"),
        )

    details = catalog.show_account("alice")
    assert details.revision == revision
    assert details.outgoing is None


def test_disable_and_reenable_validate_secrets_outside_write_transaction(tmp_path: Path) -> None:
    catalog_holder: list[ManagedCatalog] = []
    transaction_states: list[bool] = []

    def observe_get(_locator: str) -> None:
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            transaction_states.append(connection.in_transaction)

    store = FakeSecretStore(on_get=observe_get)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog, outgoing=True)
    catalog.set_secret("alice", "incoming", "incoming-secret")
    catalog.set_secret("alice", "outgoing", "outgoing-secret")
    revision = catalog.show_account("alice").revision
    disabled_revision = catalog.disable_account("alice", expected_revision=revision)

    enabled_revision = catalog.enable_account("alice", expected_revision=disabled_revision)

    assert enabled_revision == disabled_revision + 1
    assert catalog.show_account("alice").enabled is True
    assert transaction_states == [False, False]


def test_reenable_rechecks_revision_after_secret_resolution(tmp_path: Path) -> None:
    catalog_holder: list[ManagedCatalog] = []
    raced = False

    def race(_locator: str) -> None:
        nonlocal raced
        if raced:
            return
        raced = True
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            connection.execute("UPDATE managed_account SET revision = revision + 1 WHERE name = 'alice'")
            connection.commit()

    store = FakeSecretStore(on_get=race)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    revision = catalog.show_account("alice").revision
    disabled_revision = catalog.disable_account("alice", expected_revision=revision)

    with pytest.raises(ManagedCatalogError, match="changed while credentials"):
        catalog.enable_account("alice", expected_revision=disabled_revision)

    assert catalog.show_account("alice").enabled is False


def test_soft_remove_retains_identity_endpoints_bindings_and_projection(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    account_id, revision = catalog.account_revision("alice")
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.execute(
            "INSERT INTO operational_account VALUES (?, 'managed', ?, '2026-07-22T00:00:00+00:00')",
            ("operational-alice", f"managed:{account_id}"),
        )
        connection.execute(
            """INSERT INTO mailbox_projection
               VALUES ('mailbox-inbox', 'operational-alice', 'INBOX', '/', '[]', 1, '2026-07-22T00:00:00+00:00')"""
        )
        connection.commit()

    catalog.soft_remove_account("alice", expected_revision=revision)

    assert catalog.list_accounts() == []
    with pytest.raises(ManagedCatalogError, match="not found"):
        catalog.load_account("alice")
    with closing(sqlite3.connect(catalog.path)) as connection:
        row = connection.execute(
            """SELECT a.enabled, a.removed_at,
                      (SELECT COUNT(*) FROM endpoint WHERE account_id = a.id),
                      (SELECT COUNT(*) FROM secret_binding WHERE account_id = a.id)
               FROM managed_account a WHERE a.id = ?""",
            (account_id,),
        ).fetchone()
        projection_count = connection.execute("SELECT COUNT(*) FROM mailbox_projection").fetchone()[0]
    assert row[0] == 0
    assert row[1] is not None
    assert row[2:] == (1, 1)
    assert projection_count == 1
    assert store.values


def test_secret_removal_detaches_binding_before_external_delete(tmp_path: Path) -> None:
    observed_statuses: list[str] = []
    catalog_holder: list[ManagedCatalog] = []

    def observe_delete(locator: str) -> None:
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            status = connection.execute(
                "SELECT status FROM secret_binding WHERE opaque_locator = ?", (locator,)
            ).fetchone()[0]
        observed_statuses.append(status)

    store = FakeSecretStore(on_delete=observe_delete)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    revision = catalog.show_account("alice").revision
    disabled_revision = catalog.disable_account("alice", expected_revision=revision)

    cleaned = catalog.remove_secret("alice", "incoming", expected_revision=disabled_revision)

    assert cleaned is True
    assert observed_statuses == ["CLEANUP_REQUIRED"]
    assert catalog.show_account("alice").incoming_binding == "SUPERSEDED"
    assert catalog.show_account("alice").revision == disabled_revision + 1


def test_secret_removal_rejects_enabled_account_without_external_delete(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")

    with pytest.raises(ManagedCatalogError, match="Disable"):
        catalog.remove_secret("alice", "incoming", expected_revision=2)

    assert store.delete_calls == []
    assert catalog.show_account("alice").incoming_binding == "ACTIVE"


def test_doctor_cleanup_claims_candidates_before_delete_and_never_deletes_active_binding(tmp_path: Path) -> None:
    observed_statuses: list[str] = []
    catalog_holder: list[ManagedCatalog] = []

    def observe_delete(locator: str) -> None:
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            status = connection.execute(
                "SELECT status FROM secret_binding WHERE opaque_locator = ?", (locator,)
            ).fetchone()[0]
        observed_statuses.append(status)

    store = FakeSecretStore(on_delete=observe_delete)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "active-secret")
    active_locator = next(iter(store.values))
    stale_locators = ["stale-pending", "cleanup-required"]
    for locator in stale_locators:
        store.values[locator] = "candidate"
    account_id, _revision = catalog.account_revision("alice")
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.execute(
            """INSERT INTO secret_binding(id, account_id, role, status, opaque_locator, created_at)
               VALUES ('stale-pending-id', ?, 'incoming', 'PENDING', ?, '2000-01-01 00:00:00')""",
            (account_id, stale_locators[0]),
        )
        connection.execute(
            """INSERT INTO secret_binding(id, account_id, role, status, opaque_locator, created_at)
               VALUES ('cleanup-id', ?, 'incoming', 'CLEANUP_REQUIRED', ?, CURRENT_TIMESTAMP)""",
            (account_id, stale_locators[1]),
        )
        connection.commit()

    first = catalog.cleanup_credentials(limit=1)
    second = catalog.cleanup_credentials(limit=1)

    assert first.examined == first.cleaned == 1
    assert second.examined == second.cleaned == 1
    assert active_locator not in store.delete_calls
    assert set(stale_locators) <= set(store.delete_calls)
    assert observed_statuses == ["CLEANUP_REQUIRED", "CLEANUP_REQUIRED"]
    assert store.values == {active_locator: "active-secret"}


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
