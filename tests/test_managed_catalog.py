from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from mcp_email_server import managed as managed_module
from mcp_email_server.config import EmailServer
from mcp_email_server.managed import (
    _SCHEMA_V1,
    SCHEMA_VERSION,
    SQLITE_BUSY_TIMEOUT_MS,
    WAL_RETRY_BUSY_TIMEOUT_MS,
    ManagedCatalog,
    ManagedCatalogError,
    ManagedCatalogInitializationConflictError,
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


def test_managed_catalog_enforces_account_and_policy_cardinality(monkeypatch, tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _add_account(catalog)
    monkeypatch.setattr(
        managed_module,
        "APPLICATION_LIMITS",
        replace(managed_module.APPLICATION_LIMITS, configured_accounts=1, policy_entries=1),
    )

    with pytest.raises(ManagedCatalogError, match="account count"):
        catalog.add_account(
            name="bob",
            full_name="Bob",
            email_address="bob@example.test",
            incoming=_server(),
            outgoing=None,
        )
    with pytest.raises(ManagedCatalogError, match="recipient policy"):
        catalog.update_policy(
            expected_revision=catalog.catalog_revision(),
            enable_attachment_download=False,
            allowed_recipients=("first@example.test", "second@example.test"),
            allowed_senders=(),
            report_blocked_mutations=False,
        )
    assert len(catalog.list_accounts()) == 1


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


def test_initialize_adopts_existing_staging_catalog_without_resetting_state(tmp_path: Path) -> None:
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
    assert adopted.lifecycle() == "STAGING"
    assert adopted.catalog_revision() == revision
    assert adopted.policy().allowed_recipients == ("alice@example.test",)


def test_initialize_rejects_active_catalog_without_modifying_it(tmp_path: Path) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    catalog.set_secret("alice", "incoming", "secret")
    catalog.activate()
    revision = catalog.catalog_revision()

    with pytest.raises(ManagedCatalogInitializationConflictError, match="not in the staging lifecycle"):
        ManagedCatalog.initialize(catalog.path)

    assert catalog.lifecycle() == "ACTIVE"
    assert catalog.catalog_revision() == revision
    assert catalog.list_accounts()[0].name == "alice"


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


def test_v1_catalog_migrates_normalized_identity_and_repair_state_schema(tmp_path: Path) -> None:
    parent = tmp_path / "v1"
    parent.mkdir(mode=0o700)
    path = parent / "catalog.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_SCHEMA_V1)
        connection.execute("INSERT INTO schema_metadata VALUES (1, 1)")
        connection.execute("INSERT INTO catalog VALUES ('local', 'STAGING', 1, 0, '[]', '[]', 0)")
        connection.execute(
            """INSERT INTO managed_account
               VALUES ('account-1', 'local', ' Alice ', 'Alice', 'alice@example.test', 1, 1, 1, NULL, NULL)"""
        )
        connection.execute(
            """INSERT INTO endpoint
               VALUES ('account-1', 'incoming', 'imap.example.test', 993, 1, 0, 1, 'alice@example.test')"""
        )
        connection.commit()
    path.chmod(0o600)

    catalog = ManagedCatalog(path, secret_store=FakeSecretStore())
    assert catalog.show_account("alice").name == " Alice "

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT normalized_name FROM managed_account").fetchone()[0] == "alice"


def test_v1_migration_rolls_back_version_and_schema_when_invariants_fail(tmp_path: Path) -> None:
    parent = tmp_path / "invalid-v1"
    parent.mkdir(mode=0o700)
    path = parent / "catalog.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_SCHEMA_V1)
        connection.execute("INSERT INTO schema_metadata VALUES (1, 1)")
        connection.execute("INSERT INTO catalog VALUES ('local', 'STAGING', 1, 0, '[]', '[]', 0)")
        connection.execute(
            """INSERT INTO endpoint
               VALUES ('missing-account', 'incoming', 'imap.example.test', 993, 1, 0, 1, 'alice@example.test')"""
        )
        connection.commit()
    path.chmod(0o600)
    catalog = ManagedCatalog(path, secret_store=FakeSecretStore())

    for _attempt in range(2):
        with pytest.raises(ManagedCatalogError, match="invalid references"):
            catalog.lifecycle()
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == 1
            assert connection.execute("PRAGMA foreign_key_check").fetchone() is not None
            columns = {row[1] for row in connection.execute("PRAGMA table_info(managed_account)")}
            assert "normalized_name" not in columns


def test_v2_open_rejects_persistent_invariant_corruption(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, FakeSecretStore())
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """INSERT INTO endpoint
               VALUES ('missing-account', 'incoming', 'imap.example.test', 993, 1, 0, 1, 'alice@example.test')"""
        )
        connection.commit()

    with pytest.raises(ManagedCatalogError, match="invalid references"):
        catalog.lifecycle()


def test_normalized_name_and_tombstone_prevent_reuse(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, FakeSecretStore())
    _add_account(catalog)
    revision = catalog.show_account("ALICE").revision
    catalog.soft_remove_account(" alice ", expected_revision=revision)

    with pytest.raises(ManagedCatalogError, match="already exists"):
        catalog.add_account(
            name="\uff21\uff2c\uff29\uff23\uff25",
            full_name="Another Alice",
            email_address="other@example.test",
            incoming=_server(),
            outgoing=None,
        )


def test_add_and_activate_round_trip_never_persists_secret(tmp_path):
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)

    catalog.set_secret("alice", "incoming", "super-secret-password")
    catalog.activate()
    settings = catalog.load_settings()

    account = settings.emails[0]
    assert account.account_name == "alice"
    assert account.incoming.password.get_secret_value() == ""
    resolved = catalog.load_account("alice", roles=("incoming",), require_active_catalog=True)
    assert resolved.incoming.password.get_secret_value() == "super-secret-password"
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
    account = catalog.load_account(
        "alice",
        roles=("incoming", "outgoing"),
        require_active_catalog=True,
    )
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

    result = catalog.set_secret("alice", "incoming", "secret")

    assert result.status == "pending_repair_required"
    assert result.cleanup_required == 1
    report = catalog.doctor()
    assert report.pending_bindings == 0
    assert report.cleanup_required_bindings == 0
    assert report.repair_required_bindings == 1
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
    catalog.activate()

    def block_get(_locator: str) -> None:
        entered.set()
        assert release.wait(timeout=5)

    store.on_get = block_get
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            catalog.resolve_account,
            "alice",
            roles=("incoming",),
            require_active_catalog=True,
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


def test_explicit_repair_can_resume_an_ambiguous_candidate(tmp_path: Path) -> None:
    store = FakeSecretStore()
    store.fail_put = True
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    result = catalog.set_secret("alice", "incoming", "candidate-secret")
    assert result.status == "pending_repair_required"
    with closing(sqlite3.connect(catalog.path)) as connection:
        locator = connection.execute(
            "SELECT opaque_locator FROM secret_binding WHERE status = 'PENDING_REPAIR_REQUIRED'"
        ).fetchone()[0]
    store.values[locator] = "candidate-secret"
    store.fail_put = False

    repaired = catalog.repair_secret(
        "ALICE",
        "incoming",
        action="resume",
        expected_revision=result.revision,
    )

    assert repaired.status == "active"
    assert repaired.revision == result.revision + 1
    assert catalog.load_account("alice").incoming.password.get_secret_value() == "candidate-secret"
    assert catalog.doctor().repair_required_bindings == 0


def test_explicit_repair_rollback_detaches_before_external_cleanup(tmp_path: Path) -> None:
    observed: list[str] = []
    holder: list[ManagedCatalog] = []

    def observe_delete(locator: str) -> None:
        with closing(sqlite3.connect(holder[0].path)) as connection:
            observed.append(
                connection.execute(
                    "SELECT status FROM secret_binding WHERE opaque_locator = ?",
                    (locator,),
                ).fetchone()[0]
            )

    store = FakeSecretStore(on_delete=observe_delete)
    store.fail_put = True
    catalog = _catalog(tmp_path, store)
    holder.append(catalog)
    _add_account(catalog)
    result = catalog.set_secret("alice", "incoming", "candidate-secret")

    repaired = catalog.repair_secret(
        "alice",
        "incoming",
        action="rollback",
        expected_revision=result.revision,
    )

    assert repaired.status == "rolled_back"
    assert observed == ["CLEANUP_REQUIRED"]
    assert catalog.doctor().repair_required_bindings == 0


def test_repair_rollback_returns_committed_result_when_secret_cleanup_raises(tmp_path: Path) -> None:
    def fail_delete(_locator: str) -> None:
        raise ManagedCatalogError("injected secret cleanup failure")

    store = FakeSecretStore(on_delete=fail_delete)
    store.fail_put = True
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    pending = catalog.set_secret("alice", "incoming", "candidate-secret")

    repaired = catalog.repair_secret(
        "alice",
        "incoming",
        action="rollback",
        expected_revision=pending.revision,
    )

    assert repaired.status == "rolled_back_cleanup_required"
    assert repaired.revision == pending.revision + 1
    assert repaired.cleanup_required == 1
    assert catalog.show_account("alice").incoming_binding == "CLEANUP_REQUIRED"


def test_repair_resume_returns_committed_result_when_cleanup_bookkeeping_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = FakeSecretStore()
    catalog = _catalog(tmp_path, store)
    _add_account(catalog)
    active = catalog.set_secret("alice", "incoming", "old-secret")
    store.fail_put = True
    pending = catalog.set_secret(
        "alice",
        "incoming",
        "candidate-secret",
        expected_revision=active.revision,
    )
    with closing(sqlite3.connect(catalog.path)) as connection:
        locator = connection.execute(
            "SELECT opaque_locator FROM secret_binding WHERE status = 'PENDING_REPAIR_REQUIRED'"
        ).fetchone()[0]
    store.values[locator] = "candidate-secret"
    store.fail_put = False
    original_connection = catalog._connection
    calls = 0

    def fail_finalization():
        nonlocal calls
        calls += 1
        if calls > 3:
            raise ManagedCatalogError("injected finalization failure")
        return original_connection()

    monkeypatch.setattr(catalog, "_connection", fail_finalization)

    repaired = catalog.repair_secret(
        "alice",
        "incoming",
        action="resume",
        expected_revision=pending.revision,
    )

    assert repaired.status == "active_cleanup_required"
    assert repaired.revision == pending.revision + 1
    assert repaired.cleanup_required == 1
    monkeypatch.setattr(catalog, "_connection", original_connection)
    assert catalog.load_account("alice").incoming.password.get_secret_value() == "candidate-secret"
    assert catalog.doctor().cleanup_required_bindings == 1


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
    first = catalog.set_secret("alice", "incoming", "first-secret")
    assert first.status == "pending_repair_required"
    store.fail_put = False

    catalog.set_secret("alice", "incoming", "replacement-secret")

    # Ambiguous candidates require explicit repair and are never reclaimed by a
    # later successful writer as if they were ordinary cleanup work.
    assert observed_statuses == []
    report = catalog.doctor()
    assert report.pending_bindings == 0
    assert report.repair_required_bindings == 1
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

    resolved = catalog.load_account("alice", roles=("incoming",))
    assert resolved.incoming.password.get_secret_value() == "candidate-a"
    assert catalog.doctor().pending_bindings == 0
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
        catalog.load_account("alice", roles=("incoming",), require_active_catalog=True)


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


def test_soft_remove_claims_every_candidate_state_before_bounded_external_cleanup(tmp_path: Path) -> None:
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
        for status in ("PENDING", "CLEANUP_REQUIRED", "PENDING_REPAIR_REQUIRED"):
            binding_id = f"binding-{status.lower()}"
            locator = f"locator-{status.lower()}"
            connection.execute(
                """INSERT INTO secret_binding(id, account_id, role, status, opaque_locator)
                   VALUES (?, ?, 'outgoing', ?, ?)""",
                (binding_id, account_id, status, locator),
            )
            store.values[locator] = f"value-{status.lower()}"
        connection.commit()

    result = catalog.soft_remove_account("alice", expected_revision=revision)

    assert observed_statuses == ["CLEANUP_REQUIRED"] * 4
    assert result.credentials_examined == 4
    assert result.credentials_cleaned == 4
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
        catalog.lifecycle()


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
        catalog.lifecycle()

    assert target.read_bytes() == b"preserve"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", ".lock"])
def test_catalog_sidecar_and_lock_permissions_are_rejected(tmp_path: Path, suffix: str) -> None:
    catalog = _catalog(tmp_path)
    entry = Path(f"{catalog.path}{suffix}")
    entry.unlink(missing_ok=True)
    entry.write_bytes(b"unsafe")
    entry.chmod(0o644)

    with pytest.raises(ManagedCatalogSecurityError, match="owner-only"):
        catalog.lifecycle()


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
        catalog.lifecycle()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", ".lock"])
def test_catalog_sidecar_and_lock_non_regular_entries_are_rejected(tmp_path: Path, suffix: str) -> None:
    catalog = _catalog(tmp_path)
    entry = Path(f"{catalog.path}{suffix}")
    entry.unlink(missing_ok=True)
    entry.mkdir()

    with pytest.raises(ManagedCatalogSecurityError, match="regular file"):
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
