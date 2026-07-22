from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from mcp_email_server.application.management import (
    AccountDetails,
    AccountSummary,
    BootstrapSnapshot,
    CatalogLifecycleService,
    CreateAccountCommand,
    EndpointPatch,
    EndpointSummary,
    LegacyAccountSnapshot,
    LegacyImportService,
    LegacySourceSnapshot,
    ManagedAccountService,
    ManagedPolicy,
    ManagementError,
    ManagementServices,
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
    backend.write_selection.assert_called_once_with("legacy", catalog.path)


def test_managed_selection_validates_active_effective_snapshot_before_write() -> None:
    database = Path("/private/catalog.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=database)
    catalog = Mock()
    catalog.lifecycle.return_value = "ACTIVE"
    backend.open_catalog.return_value = catalog

    CatalogLifecycleService(backend).select("managed")

    catalog.load_settings.assert_called_once_with(require_active=True)
    backend.write_selection.assert_called_once_with("managed", database)


def test_account_creation_uses_one_service_for_rows_and_both_candidates() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    catalog = Mock()
    backend.open_catalog.return_value = catalog
    command = CreateAccountCommand(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=_endpoint(),
        incoming_secret="incoming-secret",
        outgoing=_endpoint("smtp.example.test"),
        outgoing_secret="outgoing-secret",
    )

    ManagedAccountService(backend).create(command)

    catalog.add_account.assert_called_once()
    assert catalog.set_secret.call_args_list[0].args == ("alice", "incoming", "incoming-secret")
    assert catalog.set_secret.call_args_list[1].args == ("alice", "outgoing", "outgoing-secret")


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
                outgoing=None,
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
    assert plan.policy_action == "update"
    assert plan.unsupported_provider_names == ("calendar-provider",)
    backend.resolve_legacy_secret.assert_not_called()


def test_repeated_exact_legacy_apply_is_noop_without_secret_resolution() -> None:
    source_account = LegacyAccountSnapshot(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=_summary(),
        outgoing=None,
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

    report = LegacyImportService(backend).apply(confirmation="IMPORT")

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
                outgoing=None,
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

    with pytest.raises(ManagementError, match="conflicts: alice"):
        LegacyImportService(backend).apply(confirmation="IMPORT")

    backend.resolve_legacy_secret.assert_not_called()
    catalog.add_account.assert_not_called()


@pytest.mark.asyncio
async def test_connectivity_service_delegates_to_provider_adapter() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("catalog.sqlite3"))
    catalog = Mock()
    backend.open_catalog.return_value = catalog
    backend.test_connection = AsyncMock()
    services = ManagementServices.compose(backend)

    await services.connectivity.execute("alice", "incoming")

    backend.test_connection.assert_awaited_once_with(catalog, "alice", "incoming")
