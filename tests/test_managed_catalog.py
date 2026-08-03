from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from mcp_email_server import managed as managed_module
from mcp_email_server.config import EmailServer
from mcp_email_server.managed import (
    SQLITE_BUSY_TIMEOUT_MS,
    WAL_RETRY_BUSY_TIMEOUT_MS,
    ManagedCatalog,
    ManagedCatalogError,
    ManagedCatalogInitializationConflictError,
    ManagedCatalogSecurityError,
    ManagedKeyringSecretStore,
    ManagedSecretStoreUnavailableError,
    ManagedSqliteSecretStore,
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


def test_managed_catalog_enforces_account_and_policy_cardinality(monkeypatch, tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _add_account(catalog)
    monkeypatch.setattr(
        managed_module,
        "APPLICATION_LIMITS",
        replace(managed_module.APPLICATION_LIMITS, configured_accounts=1, policy_entries=1),
    )

    with pytest.raises(ManagedCatalogError, match="account count") as account_error:
        catalog.add_account(
            name="bob",
            full_name="Bob",
            email_address="bob@example.test",
            incoming=_server(),
            outgoing=None,
        )
    assert account_error.value.reason == "account_limit_reached"
    with pytest.raises(ManagedCatalogError, match="recipient policy"):
        catalog.update_policy(
            expected_revision=catalog.catalog_revision(),
            enable_attachment_download=False,
            allowed_recipients=("first@example.test", "second@example.test"),
            allowed_senders=(),
            report_blocked_mutations=False,
        )
    assert len(catalog.list_accounts()) == 1


def test_initialize_creates_minimal_private_catalog(tmp_path):
    catalog = _catalog(tmp_path)

    assert catalog.catalog_revision() == 1
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
        "managed_secret",
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


def test_initialize_adopts_existing_catalog_without_resetting_state(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    policy = catalog.policy()
    catalog.update_policy(
        expected_revision=policy.revision,
        enable_attachment_download=True,
        allowed_recipients=("alice@example.test",),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    revision = catalog.catalog_revision()

    adopted = ManagedCatalog.initialize(catalog.path)

    assert adopted.path == catalog.path
    assert adopted.catalog_revision() == revision
    assert adopted.policy().allowed_recipients == ("alice@example.test",)


def test_initialize_adopts_configured_catalog_without_modifying_it(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    revision = catalog.catalog_revision()

    adopted = ManagedCatalog.initialize(catalog.path)

    assert adopted.catalog_revision() == revision
    assert adopted.list_accounts()[0].name == "alice"


def test_initialize_rejects_foreign_existing_file_without_modifying_it(tmp_path: Path) -> None:
    parent = tmp_path / "foreign"
    parent.mkdir(mode=0o700)
    path = parent / "catalog.sqlite3"
    original = b"not a managed sqlite catalog"
    path.write_bytes(original)
    path.chmod(0o600)

    with pytest.raises(ManagedCatalogInitializationConflictError, match="not a compatible managed catalog"):
        ManagedCatalog.initialize(path)

    assert path.read_bytes() == original


def test_pre_release_schema_version_is_rejected_without_migration(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, FakeSecretStore())
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.execute("UPDATE schema_metadata SET version = 1 WHERE singleton = 1")
        connection.commit()

    with pytest.raises(ManagedCatalogError, match="version is unsupported"):
        catalog.catalog_revision()

    with closing(sqlite3.connect(catalog.path)) as connection:
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == 1


def test_v3_open_rejects_persistent_invariant_corruption(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, FakeSecretStore())
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """INSERT INTO endpoint
               VALUES ('missing-account', 'incoming', 'imap.example.test', 993, 1, 0, 1, 'alice@example.test')"""
        )
        connection.commit()

    with pytest.raises(ManagedCatalogError, match="invalid references"):
        catalog.catalog_revision()


def test_normalized_name_and_tombstone_prevent_reuse(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, FakeSecretStore())
    _add_account(catalog)
    revision = catalog.show_account("ALICE").revision
    catalog.soft_remove_account(" alice ", expected_revision=revision)

    with pytest.raises(ManagedCatalogError, match="already exists") as duplicate_error:
        catalog.add_account(
            name="\uff21\uff2c\uff29\uff23\uff25",
            full_name="Another Alice",
            email_address="other@example.test",
            incoming=_server(),
            outgoing=None,
        )
    assert duplicate_error.value.reason == "account_name_exists"


def test_add_and_resolve_round_trip_never_persists_secret(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)

    catalog.set_secret("alice", "incoming", "super-secret-password")
    settings = catalog.load_settings()

    account = settings.emails[0]
    assert account.account_name == "alice"
    assert account.incoming.password.get_secret_value() == ""
    resolved = catalog.load_account("alice", roles=("incoming",))
    assert resolved.incoming.password.get_secret_value() == "super-secret-password"
    assert b"super-secret-password" not in catalog.path.read_bytes()


def test_incomplete_enabled_account_is_excluded_from_effective_settings(tmp_path):
    catalog = _catalog(tmp_path, FakeSecretStore())
    _add_account(catalog)

    assert catalog.load_settings().emails == []
    assert catalog.doctor().problems == ("account_incomplete:alice:incoming",)


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

    assert catalog.load_settings().emails == []

    catalog.set_secret("alice", "outgoing", "outgoing-secret")
    account = catalog.load_account("alice", roles=("incoming", "outgoing"))
    assert account.outgoing is not None
    assert account.outgoing.password.get_secret_value() == "outgoing-secret"


def test_external_secret_is_stored_and_verified_before_binding_is_committed(tmp_path: Path) -> None:
    observed_binding_counts: list[int] = []
    catalog_holder: list[ManagedCatalog] = []

    def observe(_locator: str, _value: str) -> None:
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            observed_binding_counts.append(connection.execute("SELECT COUNT(*) FROM secret_binding").fetchone()[0])

    store = FakeSecretStore(on_put=observe)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)

    result = catalog.set_secret("alice", "incoming", "secret")

    assert observed_binding_counts == [0]
    assert result.status == "active"
    assert catalog.show_account("alice").incoming_binding == "ACTIVE"


def test_secret_store_failure_leaves_no_binding_or_revision_change(tmp_path: Path) -> None:
    store = FakeSecretStore()
    store.fail_put = True
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    initial_revision = catalog.show_account("alice").revision

    with pytest.raises(ManagedCatalogError, match="backend unavailable"):
        catalog.set_secret("alice", "incoming", "secret")

    assert catalog.show_account("alice").incoming_binding == "MISSING"
    assert catalog.show_account("alice").revision == initial_revision
    assert store.values == {}
    with closing(sqlite3.connect(catalog.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM secret_binding").fetchone()[0] == 0
    report = catalog.doctor()
    assert report.cleanup_required_bindings == 0
    assert report.problems == ("account_incomplete:alice:incoming",)


def test_revision_conflict_cleans_external_candidate_and_preserves_no_binding(tmp_path: Path) -> None:
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
    assert catalog.list_accounts()[0].incoming_binding == "MISSING"
    assert catalog.doctor().cleanup_required_bindings == 0


def test_post_commit_connection_failure_does_not_delete_active_external_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    original_connection = catalog._connection
    calls = 0

    @contextmanager
    def fail_after_activation_commit():
        nonlocal calls
        calls += 1
        with original_connection() as connection:
            yield connection
        if calls == 3:
            raise ManagedCatalogError("injected post-commit validation failure")

    monkeypatch.setattr(catalog, "_connection", fail_after_activation_commit)

    result = catalog.set_secret("alice", "incoming", "committed-secret")

    assert result.status == "active"
    assert store.delete_calls == []
    assert catalog.load_account("alice").incoming.password.get_secret_value() == "committed-secret"


def test_linux_secret_and_binding_roll_back_together_on_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(managed_module.sys, "platform", "linux")
    catalog = _catalog(tmp_path)
    _add_account(catalog)
    initial_revision = catalog.show_account("alice").revision
    original_put = ManagedSqliteSecretStore.put_in_transaction

    def insert_then_fail(connection: sqlite3.Connection, locator: str, value: str) -> None:
        original_put(connection, locator, value)
        raise ManagedCatalogError("injected activation failure")

    monkeypatch.setattr(ManagedSqliteSecretStore, "put_in_transaction", staticmethod(insert_then_fail))

    with pytest.raises(ManagedCatalogError, match="injected activation failure"):
        catalog.set_secret("alice", "incoming", "rolled-back-secret")

    with closing(sqlite3.connect(catalog.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM managed_secret").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM secret_binding").fetchone()[0] == 0
    assert catalog.show_account("alice").revision == initial_revision


def test_locked_keyring_write_is_typed_without_catalog_binding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import keyring
    from keyring.errors import KeyringLocked

    store = ManagedKeyringSecretStore()
    catalog = _catalog(tmp_path, store)  # type: ignore[arg-type]
    _add_account(catalog)

    def fail_get(_service: str, _locator: str) -> None:
        raise KeyringLocked("locked test collection")

    monkeypatch.setattr(keyring, "get_password", fail_get)

    with pytest.raises(ManagedSecretStoreUnavailableError) as caught:
        catalog.set_secret("alice", "incoming", "secret")

    assert caught.value.reason == "credential_store_unavailable"
    assert catalog.show_account("alice").incoming_binding == "MISSING"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_file_backed_platform_default_store_uses_catalog_sqlite_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
) -> None:
    monkeypatch.setattr(managed_module.sys, "platform", platform)
    catalog = _catalog(tmp_path)
    assert isinstance(catalog.secret_store, ManagedSqliteSecretStore)
    assert catalog.secret_store.path == catalog.path
    _add_account(catalog)

    result = catalog.set_secret("alice", "incoming", "sqlite-secret")

    assert result.status == "active"
    with closing(sqlite3.connect(catalog.path)) as connection:
        binding = connection.execute("SELECT status, opaque_locator FROM secret_binding").fetchone()
        stored = connection.execute(
            "SELECT secret_value FROM managed_secret WHERE locator = ?", (binding[1],)
        ).fetchone()
    assert binding[0] == "ACTIVE"
    assert stored[0] == "sqlite-secret"


@pytest.mark.parametrize("authority_change", ["disable", "policy"])
def test_selected_account_rejects_authority_change_while_secret_resolves(
    tmp_path: Path,
    authority_change: str,
) -> None:
    entered = Event()
    release = Event()
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    result = catalog.set_secret("alice", "incoming", "active-secret")

    def block_get(_locator: str) -> None:
        entered.set()
        assert release.wait(timeout=5)

    store.on_get = block_get
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            catalog.resolve_account,
            "alice",
            roles=("incoming",),
        )
        assert entered.wait(timeout=5)
        if authority_change == "disable":
            catalog.disable_account("alice", expected_revision=result.revision)
        else:
            policy = catalog.policy()
            catalog.update_policy(
                expected_revision=policy.revision,
                enable_attachment_download=True,
                allowed_recipients=(),
                allowed_senders=(),
                report_blocked_mutations=False,
            )
        release.set()
        with pytest.raises(ManagedCatalogError, match=r"changed|disabled"):
            future.result(timeout=5)


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

    resolved = catalog.load_account("alice", roles=("incoming",))
    assert resolved.incoming.password.get_secret_value() == "candidate-a"
    assert catalog.doctor().cleanup_required_bindings == 0


def test_rotation_activates_new_secret_before_old_cleanup_and_reports_failure(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "old-secret")
    old_locator = next(iter(store.values))
    store.fail_delete = True

    result = catalog.set_secret("alice", "incoming", "new-secret")

    assert result.status == "active_cleanup_required"
    resolved = catalog.load_account("alice", roles=("incoming",))
    assert resolved.incoming.password.get_secret_value() == "new-secret"
    assert old_locator in store.delete_calls
    assert catalog.doctor().cleanup_required_bindings == 1


def test_later_rotation_reports_existing_cleanup_residue(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    first = catalog.set_secret("alice", "incoming", "first-secret")
    store.fail_delete = True
    second = catalog.set_secret(
        "alice",
        "incoming",
        "second-secret",
        expected_revision=first.revision,
    )
    assert second.status == "active_cleanup_required"
    store.fail_delete = False

    third = catalog.set_secret(
        "alice",
        "incoming",
        "third-secret",
        expected_revision=second.revision,
    )

    assert third.status == "active_cleanup_required"
    assert third.cleanup_required == 1
    assert catalog.doctor().cleanup_required_bindings == 1
    assert "first-secret" in store.values.values()
    assert "third-secret" in store.values.values()


def test_rotation_returns_committed_result_when_cleanup_bookkeeping_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "old-secret")
    original_connection = catalog._connection
    calls = 0

    def fail_finalization():
        nonlocal calls
        calls += 1
        if calls > 3:
            raise ManagedCatalogError("injected finalization failure")
        return original_connection()

    monkeypatch.setattr(catalog, "_connection", fail_finalization)

    result = catalog.set_secret("alice", "incoming", "new-secret")

    assert result.status == "active_cleanup_required"
    assert result.cleanup_required == 1
    monkeypatch.setattr(catalog, "_connection", original_connection)
    resolved = catalog.load_account("alice", roles=("incoming",))
    assert resolved.incoming.password.get_secret_value() == "new-secret"


def test_disabled_account_is_excluded_from_runtime_without_secret_access(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    catalog.disable_account("alice", expected_revision=2)
    store.fail_get = True

    settings = catalog.load_settings()

    assert settings.emails == []


def test_missing_or_unreadable_active_secret_fails_closed(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    store.values.clear()

    with pytest.raises(ManagedCatalogError, match="missing"):
        catalog.load_account("alice", roles=("incoming",))


def test_new_incomplete_account_does_not_hide_existing_effective_account(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")

    catalog.add_account(
        name="incomplete",
        full_name="Incomplete",
        email_address="incomplete@example.test",
        incoming=_server(),
        outgoing=None,
    )

    assert [account.account_name for account in catalog.load_settings().emails] == ["alice"]
    assert "account_incomplete:incomplete:incoming" in catalog.doctor().problems


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

    assert catalog.load_account("alice", roles=("incoming",)).incoming.password.get_secret_value() == "new-secret"
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
    assert catalog.load_account("alice-renamed").incoming.password.get_secret_value() == "secret"
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
    transaction_states.clear()
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

    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    store.on_get = race
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

    result = catalog.soft_remove_account("alice", expected_revision=revision)

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
        binding_status = connection.execute(
            "SELECT status FROM secret_binding WHERE account_id = ?", (account_id,)
        ).fetchone()[0]
    assert row[0] == 0
    assert row[1] is not None
    assert row[2:] == (1, 1)
    assert projection_count == 1
    assert binding_status == "SUPERSEDED"
    assert store.values == {}
    assert result.revision == revision + 1
    assert result.credentials_examined == 1
    assert result.credentials_cleaned == 1
    assert result.cleanup_required == 0


def test_soft_remove_claims_active_and_cleanup_required_bindings_before_external_cleanup(tmp_path: Path) -> None:
    observed_statuses: list[str] = []
    catalog_holder: list[ManagedCatalog] = []

    def observe_delete(locator: str) -> None:
        with closing(sqlite3.connect(catalog_holder[0].path)) as connection:
            observed_statuses.append(
                connection.execute("SELECT status FROM secret_binding WHERE opaque_locator = ?", (locator,)).fetchone()[
                    0
                ]
            )

    store = FakeSecretStore(on_delete=observe_delete)
    catalog = _catalog(tmp_path, store)
    catalog_holder.append(catalog)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "active-secret")
    account_id, revision = catalog.account_revision("alice")
    with closing(sqlite3.connect(catalog.path)) as connection:
        locator = "cleanup-required-locator"
        connection.execute(
            """INSERT INTO secret_binding(id, account_id, role, status, opaque_locator)
               VALUES ('cleanup-required-binding', ?, 'outgoing', 'CLEANUP_REQUIRED', ?)""",
            (account_id, locator),
        )
        store.values[locator] = "cleanup-required-value"
        connection.commit()

    result = catalog.soft_remove_account("alice", expected_revision=revision)

    assert observed_statuses == ["CLEANUP_REQUIRED"] * 2
    assert result.credentials_examined == 2
    assert result.credentials_cleaned == 2
    assert result.cleanup_required == 0
    assert store.values == {}
    with closing(sqlite3.connect(catalog.path)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM secret_binding WHERE account_id = ? AND status != 'SUPERSEDED'",
                (account_id,),
            ).fetchone()[0]
            == 0
        )


def test_soft_remove_leaves_failed_deletes_globally_recoverable(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    _, revision = catalog.account_revision("alice")
    store.fail_delete = True

    result = catalog.soft_remove_account("alice", expected_revision=revision)

    assert result.credentials_cleaned == 0
    assert result.cleanup_required == 1
    with closing(sqlite3.connect(catalog.path)) as connection:
        assert (
            connection.execute("SELECT status FROM secret_binding WHERE status != 'SUPERSEDED'").fetchone()[0]
            == "CLEANUP_REQUIRED"
        )
    store.fail_delete = False
    report = catalog.cleanup_credentials()
    assert report.cleaned == 1
    assert report.remaining == 0
    assert store.values == {}


def test_soft_remove_returns_committed_result_when_cleanup_bookkeeping_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    _, revision = catalog.account_revision("alice")
    original_connection = catalog._connection
    calls = 0

    def fail_finalization():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ManagedCatalogError("injected finalization failure")
        return original_connection()

    monkeypatch.setattr(catalog, "_connection", fail_finalization)

    result = catalog.soft_remove_account("alice", expected_revision=revision)

    assert result.revision == revision + 1
    assert result.credentials_cleaned == 0
    assert result.cleanup_required == 1
    monkeypatch.setattr(catalog, "_connection", original_connection)
    assert catalog.list_accounts() == []


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

    result = catalog.remove_secret("alice", "incoming", expected_revision=disabled_revision)

    assert result.status == "removed"
    assert result.revision == disabled_revision + 1
    assert result.cleanup_required == 0
    assert observed_statuses == ["CLEANUP_REQUIRED"]
    assert catalog.show_account("alice").incoming_binding == "SUPERSEDED"
    assert catalog.show_account("alice").revision == disabled_revision + 1


def test_secret_removal_reports_committed_cleanup_reconciliation_on_finalization_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    revision = catalog.show_account("alice").revision
    disabled_revision = catalog.disable_account("alice", expected_revision=revision)
    original_connection = catalog._connection
    calls = 0

    def fail_finalization():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ManagedCatalogError("injected finalization failure")
        return original_connection()

    monkeypatch.setattr(catalog, "_connection", fail_finalization)

    result = catalog.remove_secret("alice", "incoming", expected_revision=disabled_revision)

    assert result.status == "removed_cleanup_required"
    assert result.revision == disabled_revision + 1
    assert result.cleanup_required == 1
    monkeypatch.setattr(catalog, "_connection", original_connection)
    assert catalog.show_account("alice").incoming_binding == "CLEANUP_REQUIRED"


def test_secret_removal_rejects_enabled_account_without_external_delete(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")

    with pytest.raises(ManagedCatalogError, match="Disable"):
        catalog.remove_secret("alice", "incoming", expected_revision=2)

    assert store.delete_calls == []
    assert catalog.show_account("alice").incoming_binding == "ACTIVE"


def test_doctor_cleanup_claims_cleanup_required_binding_and_never_deletes_active_binding(tmp_path: Path) -> None:
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
    stale_locator = "cleanup-required"
    store.values[stale_locator] = "candidate"
    account_id, _revision = catalog.account_revision("alice")
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.execute(
            """INSERT INTO secret_binding(id, account_id, role, status, opaque_locator, created_at)
               VALUES ('cleanup-id', ?, 'incoming', 'CLEANUP_REQUIRED', ?, CURRENT_TIMESTAMP)""",
            (account_id, stale_locator),
        )
        connection.commit()

    result = catalog.cleanup_credentials(limit=1)

    assert result.examined == result.cleaned == 1
    assert active_locator not in store.delete_calls
    assert stale_locator in store.delete_calls
    assert observed_statuses == ["CLEANUP_REQUIRED"]
    assert store.values == {active_locator: "active-secret"}


def test_managed_catalog_fails_closed_without_secure_filesystem_primitives(monkeypatch, tmp_path):
    monkeypatch.setattr("mcp_email_server.managed._SECURE_CATALOG_FILES_SUPPORTED", False)
    parent = tmp_path / "managed"

    with pytest.raises(ManagedCatalogSecurityError, match="platform cannot enforce"):
        ManagedCatalog.initialize(parent / "catalog.sqlite3")

    assert not parent.exists()


def test_managed_catalog_rejects_symlinked_and_writable_ancestor_chain(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ManagedCatalogSecurityError, match="parent chain"):
        ManagedCatalog.initialize(linked / "catalog.sqlite3")
    assert not (real / "catalog.sqlite3").exists()

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    private = unsafe / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(ManagedCatalogSecurityError, match="ancestor permissions"):
        ManagedCatalog.initialize(private / "catalog.sqlite3")
    assert not (private / "catalog.sqlite3").exists()


def test_insecure_database_permissions_are_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    if os.name != "posix":
        pytest.skip("POSIX permission contract")
    catalog.path.chmod(0o644)

    with pytest.raises(ManagedCatalogSecurityError, match="owner-only"):
        catalog.catalog_revision()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", ".lock"])
def test_catalog_sidecar_and_lock_symlinks_are_rejected(tmp_path: Path, suffix: str) -> None:
    catalog = _catalog(tmp_path)
    entry = Path(f"{catalog.path}{suffix}")
    entry.unlink(missing_ok=True)
    target = catalog.path.parent / f"target-{suffix.removeprefix('.').removeprefix('-')}"
    target.write_bytes(b"preserve")
    target.chmod(0o600)
    try:
        entry.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ManagedCatalogSecurityError, match="symlink"):
        catalog.catalog_revision()

    assert target.read_bytes() == b"preserve"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", ".lock"])
def test_catalog_sidecar_and_lock_permissions_are_rejected(tmp_path: Path, suffix: str) -> None:
    catalog = _catalog(tmp_path)
    entry = Path(f"{catalog.path}{suffix}")
    entry.unlink(missing_ok=True)
    entry.write_bytes(b"unsafe")
    entry.chmod(0o644)

    with pytest.raises(ManagedCatalogSecurityError, match="owner-only"):
        catalog.catalog_revision()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", ".lock"])
def test_catalog_sidecar_and_lock_hard_links_are_rejected(tmp_path: Path, suffix: str) -> None:
    catalog = _catalog(tmp_path)
    entry = Path(f"{catalog.path}{suffix}")
    entry.unlink(missing_ok=True)
    target = catalog.path.parent / f"target-{suffix.removeprefix('.').removeprefix('-')}"
    target.write_bytes(b"unsafe")
    target.chmod(0o600)
    try:
        os.link(target, entry)
    except OSError:
        pytest.skip("hard links unavailable")

    with pytest.raises(ManagedCatalogSecurityError, match="hard links"):
        catalog.catalog_revision()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", ".lock"])
def test_catalog_sidecar_and_lock_non_regular_entries_are_rejected(tmp_path: Path, suffix: str) -> None:
    catalog = _catalog(tmp_path)
    entry = Path(f"{catalog.path}{suffix}")
    entry.unlink(missing_ok=True)
    entry.mkdir()

    with pytest.raises(ManagedCatalogSecurityError, match="regular file"):
        catalog.catalog_revision()


def test_database_symlink_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    link = catalog.path.parent / "link.sqlite3"
    try:
        link.symlink_to(catalog.path)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ManagedCatalogSecurityError, match="symlink"):
        ManagedCatalog(link).catalog_revision()


def test_concurrent_readers_do_not_fail_on_sidecar_shutdown_races(tmp_path):
    catalog = _catalog(tmp_path)

    def read_revision(_index: int) -> int:
        result = 0
        for _ in range(20):
            result = catalog.catalog_revision()
        return result

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(read_revision, range(8)))

    assert results == [1] * 8


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
        ManagedCatalog(path).catalog_revision()


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
        ManagedCatalog(path).catalog_revision()

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert path.read_bytes() == original_bytes
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_import_cutover_guard_detects_account_only_credential_drift(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, FakeSecretStore())
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "first-secret", expected_revision=1)
    assert catalog.catalog_revision() == 2
    catalog.set_secret("alice", "incoming", "rotated-secret", expected_revision=2)
    assert catalog.catalog_revision() == 2

    with pytest.raises(ManagedCatalogError, match="changed"):
        with catalog.import_cutover_guard(
            expected_catalog_revision=2,
            expected_account_revisions=(("alice", 2, True),),
        ):
            pytest.fail("stale account authority must not enter the cutover critical section")
