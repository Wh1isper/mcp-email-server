from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call

import pytest
from pydantic import SecretStr

from mcp_email_server.application.management import (
    AccountDetails,
    AccountSummary,
    BootstrapSnapshot,
    CatalogLifecycleService,
    CreateAccountCommand,
    DoctorReport,
    EndpointPatch,
    EndpointSummary,
    IndexHealth,
    LegacyAccountSnapshot,
    LegacyImportService,
    LegacySourceSnapshot,
    ManagedAccountService,
    ManagedPolicy,
    ManagementError,
    ManagementServices,
    PolicyManagementService,
    RevisionConflictError,
    UpdateAccountCommand,
)
from mcp_email_server.config import EmailServer


def _endpoint(host: str = "imap.example.test") -> EmailServer:
    return EmailServer(
        host=host,
        port=993,
        user_name="alice@example.test",
        password=SecretStr("not-persisted"),
        use_ssl=True,
    )


def _summary(host: str = "imap.example.test") -> EndpointSummary:
    return EndpointSummary(
        host=host,
        port=993,
        user_name="alice@example.test",
        use_ssl=True,
        start_ssl=False,
        verify_ssl=True,
    )


def _details(*, outgoing: EndpointSummary | None = None) -> AccountDetails:
    return AccountDetails(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        enabled=False,
        revision=4,
        save_to_sent=True,
        sent_folder_name=None,
        incoming=_summary(),
        outgoing=outgoing,
        incoming_binding="ACTIVE",
        outgoing_binding="ACTIVE" if outgoing is not None else None,
    )


def test_lifecycle_initialization_preserves_mode_and_records_database() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=None)
    catalog = Mock(path=Path("/private/catalog.sqlite3"))
    backend.initialize_catalog.return_value = catalog
    service = CatalogLifecycleService(backend)

    service.initialize(Path("catalog.sqlite3"))

    backend.initialize_catalog.assert_called_once_with(Path("catalog.sqlite3"))
    backend.write_selection.assert_called_once_with("legacy", catalog.path, expected_revision=0)


def test_lifecycle_initialization_can_retry_after_bootstrap_cas_loss() -> None:
    database = Path("/private/catalog.sqlite3")
    backend = Mock()
    backend.read_bootstrap.side_effect = [
        BootstrapSnapshot(mode="legacy", db_path=None, revision=3),
        BootstrapSnapshot(mode="legacy", db_path=Path("/private/other.sqlite3"), revision=4),
    ]
    catalog = Mock(path=database)
    backend.initialize_catalog.return_value = catalog
    backend.write_selection.side_effect = [RevisionConflictError("bootstrap"), None]
    service = CatalogLifecycleService(backend)

    with pytest.raises(RevisionConflictError):
        service.initialize(database)
    service.initialize(database)

    assert backend.initialize_catalog.call_args_list == [call(database), call(database)]
    assert backend.write_selection.call_args_list == [
        call("legacy", database, expected_revision=3),
        call("legacy", database, expected_revision=4),
    ]


def test_managed_selection_validates_active_effective_snapshot_before_write() -> None:
    database = Path("/private/catalog.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=database)
    catalog = Mock()
    catalog.catalog_revision.return_value = 7
    catalog.lifecycle.return_value = "ACTIVE"
    backend.open_catalog.return_value = catalog

    CatalogLifecycleService(backend).select(
        "managed",
        expected_bootstrap_revision=0,
        expected_catalog_revision=7,
    )

    catalog.validate_ready.assert_called_once_with(require_active=True)
    catalog.load_settings.assert_not_called()
    backend.write_selection.assert_called_once_with("managed", database, expected_revision=0)


def test_account_creation_uses_one_service_for_rows_and_both_candidates() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    catalog = Mock()
    backend.open_catalog.return_value = catalog
    command = CreateAccountCommand(
        expected_catalog_revision=3,
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=_summary(),
        incoming_secret=SecretStr("incoming-secret"),
        outgoing=_summary("smtp.example.test"),
        outgoing_secret=SecretStr("outgoing-secret"),
    )

    ManagedAccountService(backend).create(command)

    catalog.add_account.assert_called_once()
    assert catalog.add_account.call_args.kwargs["expected_revision"] == 3
    assert catalog.set_secret.call_args_list[0].args == ("alice", "incoming", "incoming-secret")
    assert catalog.set_secret.call_args_list[0].kwargs == {"expected_revision": 1}
    assert catalog.set_secret.call_args_list[1].args == ("alice", "outgoing", "outgoing-secret")
    assert catalog.set_secret.call_args_list[1].kwargs == {
        "expected_revision": catalog.set_secret.return_value.revision
    }


def test_account_creation_command_excludes_secrets_from_repr_and_equality() -> None:
    first = CreateAccountCommand(
        expected_catalog_revision=3,
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=_summary(),
        incoming_secret=SecretStr("first-secret"),
    )
    second = replace(first, incoming_secret=SecretStr("second-secret"))

    assert first == second
    assert "first-secret" not in repr(first)
    assert "second-secret" not in repr(second)


def test_account_update_merges_endpoint_patch_before_catalog_write() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    catalog = Mock()
    catalog.show_account.return_value = _details()
    catalog.update_account.return_value = 5
    backend.open_catalog.return_value = catalog

    revision = ManagedAccountService(backend).update(
        UpdateAccountCommand(
            name="alice",
            expected_revision=4,
            incoming=EndpointPatch(host="imap-new.example.test", verify_ssl=False),
        )
    )

    assert revision == 5
    written = catalog.update_account.call_args.kwargs["incoming"]
    assert written == EndpointSummary(
        host="imap-new.example.test",
        port=993,
        user_name="alice@example.test",
        use_ssl=True,
        start_ssl=False,
        verify_ssl=False,
    )


def test_new_outgoing_endpoint_requires_complete_patch() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    catalog = Mock()
    catalog.show_account.return_value = _details()
    backend.open_catalog.return_value = catalog

    with pytest.raises(ManagementError, match="requires host"):
        ManagedAccountService(backend).update(
            UpdateAccountCommand(
                name="alice",
                expected_revision=4,
                outgoing=EndpointPatch(host="smtp.example.test"),
            )
        )

    catalog.update_account.assert_not_called()


def test_soft_remove_requires_exact_transport_independent_confirmation() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    catalog = Mock()
    backend.open_catalog.return_value = catalog
    service = ManagedAccountService(backend)

    with pytest.raises(ManagementError, match="exactly match"):
        service.soft_remove("alice", expected_revision=4, confirmation="Alice")

    catalog.soft_remove_account.assert_not_called()


def test_legacy_preview_never_resolves_credentials() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    backend.load_legacy_source.return_value = LegacySourceSnapshot(
        accounts=(
            LegacyAccountSnapshot(
                name="alice",
                full_name="Alice",
                email_address="alice@example.test",
                incoming=_summary(),
                incoming_secret_source="plaintext",
                outgoing=None,
                outgoing_secret_source=None,
                save_to_sent=True,
                sent_folder_name=None,
            ),
        ),
        unsupported_provider_names=("calendar-provider",),
        enable_attachment_download=True,
        allowed_recipients=("bob@example.test",),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    catalog = Mock()
    catalog.list_accounts.return_value = []
    catalog.has_removed_account.return_value = False
    catalog.policy.return_value = ManagedPolicy(
        revision=1,
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    backend.open_catalog.return_value = catalog

    plan = LegacyImportService(backend).preview()

    assert plan.accounts[0].action == "create"
    assert plan.accounts[0].source.incoming.host == "imap.example.test"
    assert plan.accounts[0].source.incoming_secret_source == "plaintext"  # noqa: S105
    assert plan.accounts[0].expected_target_revision is None
    assert plan.policy_action == "update"
    assert plan.source_policy.allowed_recipients == ("bob@example.test",)
    assert plan.target_policy_revision == 1
    assert plan.unsupported_provider_names == ("calendar-provider",)
    backend.resolve_legacy_secret.assert_not_called()


def test_repeated_exact_legacy_apply_is_noop_without_secret_resolution() -> None:
    source_account = LegacyAccountSnapshot(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=_summary(),
        incoming_secret_source="plaintext",
        outgoing=None,
        outgoing_secret_source=None,
        save_to_sent=True,
        sent_folder_name=None,
    )
    source = LegacySourceSnapshot(
        accounts=(source_account,),
        unsupported_provider_names=(),
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    backend.load_legacy_source.return_value = source
    catalog = Mock()
    catalog.lifecycle.return_value = "STAGING"
    catalog.list_accounts.return_value = [
        AccountSummary(
            name="alice",
            email_address="alice@example.test",
            enabled=True,
            revision=2,
            has_outgoing=False,
            incoming_binding="ACTIVE",
            outgoing_binding=None,
        )
    ]
    catalog.show_account.return_value = _details()
    catalog.policy.return_value = ManagedPolicy(
        revision=1,
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    backend.open_catalog.return_value = catalog

    service = LegacyImportService(backend)
    catalog.catalog_revision.return_value = 5
    preview = service.preview()
    report = service.apply(
        preview_token=preview.preview_token,
        expected_revision=preview.target_revision,
        confirmation="IMPORT",
    )

    assert report.plan.accounts[0].action == "unchanged"
    assert report.created == report.resumed == ()
    backend.resolve_legacy_secret.assert_not_called()
    catalog.add_account.assert_not_called()
    catalog.set_secret.assert_not_called()
    catalog.update_policy.assert_not_called()


def test_legacy_apply_rejects_all_conflicts_before_secret_resolution() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    backend.load_legacy_source.return_value = LegacySourceSnapshot(
        accounts=(
            LegacyAccountSnapshot(
                name="alice",
                full_name="Different Alice",
                email_address="alice@example.test",
                incoming=_summary(),
                incoming_secret_source="plaintext",
                outgoing=None,
                outgoing_secret_source=None,
                save_to_sent=True,
                sent_folder_name=None,
            ),
        ),
        unsupported_provider_names=(),
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    catalog = Mock()
    catalog.lifecycle.return_value = "STAGING"
    catalog.list_accounts.return_value = [AccountSummary("alice", "alice@example.test", True, 2, False, "ACTIVE", None)]
    catalog.show_account.return_value = _details()
    catalog.policy.return_value = ManagedPolicy(1, False, (), (), False)
    backend.open_catalog.return_value = catalog

    service = LegacyImportService(backend)
    catalog.catalog_revision.return_value = 5
    preview = service.preview()
    with pytest.raises(ManagementError, match="conflicts: alice"):
        service.apply(
            preview_token=preview.preview_token,
            expected_revision=preview.target_revision,
            confirmation="IMPORT",
        )

    backend.resolve_legacy_secret.assert_not_called()
    catalog.add_account.assert_not_called()


def test_legacy_apply_rejects_target_revision_drift_after_secret_resolution() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    backend.load_legacy_source.return_value = _legacy_source()
    catalog = Mock()
    catalog.catalog_revision.return_value = 5
    catalog.lifecycle.return_value = "STAGING"
    catalog.list_accounts.return_value = [
        AccountSummary("alice", "alice@example.test", True, 4, False, "MISSING", None)
    ]
    original = replace(_details(), incoming_binding="MISSING")
    catalog.show_account.return_value = original
    catalog.policy.return_value = ManagedPolicy(5, False, (), (), False)
    backend.open_catalog.return_value = catalog
    service = LegacyImportService(backend)
    preview = service.preview()

    def drift_target(*_args: object) -> str:
        catalog.show_account.return_value = replace(original, revision=5)
        return "legacy-secret"

    backend.resolve_legacy_secret.side_effect = drift_target
    with pytest.raises(ManagementError, match="preview is stale"):
        service.apply(
            preview_token=preview.preview_token,
            expected_revision=preview.target_revision,
            confirmation="IMPORT",
        )

    catalog.set_secret.assert_not_called()


@pytest.mark.asyncio
async def test_connectivity_service_delegates_to_provider_adapter() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    catalog = Mock()
    backend.open_catalog.return_value = catalog
    backend.test_connection = AsyncMock()
    services = ManagementServices.compose(backend)

    result = await services.connectivity.execute("alice", "incoming")

    assert result.status == "ok"
    backend.test_connection.assert_awaited_once_with(catalog, "alice", "incoming")


def _legacy_source(*, full_name: str = "Alice") -> LegacySourceSnapshot:
    return LegacySourceSnapshot(
        accounts=(
            LegacyAccountSnapshot(
                name="alice",
                full_name=full_name,
                email_address="alice@example.test",
                incoming=_summary(),
                incoming_secret_source="plaintext",
                outgoing=None,
                outgoing_secret_source=None,
                save_to_sent=True,
                sent_folder_name=None,
            ),
        ),
        unsupported_provider_names=(),
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
    )


def _import_backend() -> tuple[Mock, Mock]:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy",
        db_path=Path("catalog.sqlite3"),
    )
    backend.load_legacy_source.return_value = _legacy_source()
    catalog = Mock()
    catalog.catalog_revision.return_value = 5
    catalog.lifecycle.return_value = "STAGING"
    catalog.list_accounts.return_value = []
    catalog.has_removed_account.return_value = False
    catalog.policy.return_value = ManagedPolicy(1, False, (), (), False)
    backend.open_catalog.return_value = catalog
    return backend, catalog


def test_status_reports_durable_selection_restart_drift() -> None:
    database = Path("/private/catalog.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="managed",
        db_path=database,
        revision=3,
        running_mode="legacy",
        running_db_path=database,
    )
    report = DoctorReport("ACTIVE", 2, 7, 1, 1, 0, 0, 0, ())
    backend.open_catalog.return_value.doctor.return_value = report

    status = CatalogLifecycleService(backend).status()

    assert status.mode == "managed"
    assert status.selected_catalog == database.as_posix()
    assert status.bootstrap_revision == 3
    assert status.restart_required is True
    assert status.report == report
    assert status.catalog_problem is None


def test_status_reports_unavailable_catalog_without_blocking_recovery() -> None:
    database = Path("/private/missing.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="managed",
        db_path=database,
        revision=9,
    )
    backend.open_catalog.return_value.doctor.side_effect = ManagementError("sensitive provider detail")

    status = CatalogLifecycleService(backend).status()

    assert status.mode == "managed"
    assert status.bootstrap_revision == 9
    assert status.report is None
    assert status.catalog_problem == "selected_catalog_unavailable"
    assert "sensitive" not in repr(status)


def test_selecting_legacy_uses_bootstrap_revision_without_opening_failed_catalog() -> None:
    database = Path("/private/catalog.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="managed", db_path=database, revision=4)

    CatalogLifecycleService(backend).select("legacy", expected_bootstrap_revision=4)

    backend.open_catalog.assert_not_called()
    backend.write_selection.assert_called_once_with("legacy", database, expected_revision=4)


def test_selection_rejects_stale_bootstrap_revision_before_catalog_access() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="managed",
        db_path=Path("/private/catalog.sqlite3"),
        revision=5,
    )

    with pytest.raises(ManagementError, match="revision changed"):
        CatalogLifecycleService(backend).select("legacy", expected_bootstrap_revision=4)

    backend.open_catalog.assert_not_called()
    backend.write_selection.assert_not_called()


@pytest.mark.parametrize("drift", ["source", "target", "expired"])
def test_legacy_apply_rejects_preview_drift_or_expiry_before_writes(drift: str) -> None:
    backend, catalog = _import_backend()
    service = LegacyImportService(backend)
    preview = service.preview()
    if drift == "source":
        backend.load_legacy_source.return_value = _legacy_source(full_name="Changed")
    elif drift == "target":
        catalog.catalog_revision.return_value = 6
    else:
        stored = service._previews[preview.preview_token]
        service._previews[preview.preview_token] = replace(stored, expires_at=-1.0)

    with pytest.raises(ManagementError, match=r"stale|expired"):
        service.apply(
            preview_token=preview.preview_token,
            expected_revision=preview.target_revision,
            confirmation="IMPORT",
        )

    backend.resolve_legacy_secret.assert_not_called()
    catalog.add_account.assert_not_called()
    catalog.set_secret.assert_not_called()
    catalog.update_policy.assert_not_called()


@pytest.mark.asyncio
async def test_connectivity_failure_is_typed_and_sanitized() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy",
        db_path=Path("catalog.sqlite3"),
    )
    backend.open_catalog.return_value = Mock()
    backend.test_connection = AsyncMock(side_effect=ManagementError("provider host secret detail"))

    result = await ManagementServices.compose(backend).connectivity.execute("alice", "outgoing")

    assert result.role == "outgoing"
    assert result.status == "failed"
    assert result.message == "Connection test failed"
    assert "secret" not in result.message


def test_managed_policy_update_uses_legacy_canonicalization() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy",
        db_path=Path("catalog.sqlite3"),
    )
    catalog = Mock()
    catalog.update_policy.return_value = 8
    backend.open_catalog.return_value = catalog

    result = PolicyManagementService(backend).update(
        ManagedPolicy(
            revision=7,
            enable_attachment_download=True,
            allowed_recipients=(" Alice <ALICE@Example.Test> ", "", "alice@example.test"),
            allowed_senders=(" *@Example.Test ", "", "*@example.test"),
            report_blocked_mutations=True,
        )
    )

    assert result.allowed_recipients == ("alice@example.test",)
    assert result.allowed_senders == ("*@example.test",)
    catalog.update_policy.assert_called_once_with(
        expected_revision=7,
        enable_attachment_download=True,
        allowed_recipients=("alice@example.test",),
        allowed_senders=("*@example.test",),
        report_blocked_mutations=True,
    )


def test_index_health_service_delegates_bounded_adapter_result() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy",
        db_path=Path("catalog.sqlite3"),
    )
    catalog = Mock()
    backend.open_catalog.return_value = catalog
    expected = IndexHealth("degraded", 2, 1, ("refresh_pending",))
    backend.index_health.return_value = expected

    result = ManagementServices.compose(backend).index_health.get()

    assert result == expected
    backend.index_health.assert_called_once_with(catalog)
