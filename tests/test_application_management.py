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
    CatalogService,
    CreateAccountCommand,
    DoctorReport,
    EndpointPatch,
    EndpointSummary,
    IndexHealth,
    LegacyAccountSnapshot,
    LegacyCredentialMigrationResult,
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
from mcp_email_server.config import EmailServer, EmailSettings


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


def _empty_legacy_source() -> LegacySourceSnapshot:
    return LegacySourceSnapshot((), (), False, (), (), False)


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


def test_catalog_initialization_selects_managed_and_records_database() -> None:
    database = Path.cwd() / "private" / "catalog.sqlite3"
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=None)
    backend.load_legacy_source.return_value = _empty_legacy_source()
    catalog = Mock(path=database)
    catalog.catalog_revision.return_value = 1
    backend.initialize_catalog.return_value = catalog
    service = CatalogService(backend)

    result = service.initialize(database)

    assert result.mode == "managed"
    assert result.bootstrap_revision == 1
    assert result.restart_required is True
    assert result.catalog_revision == 1
    backend.initialize_catalog.assert_called_once_with(database)
    catalog.validate_ready.assert_called_once_with()
    backend.write_selection.assert_called_once_with(
        "managed", catalog.path, expected_revision=0, expected_source=_empty_legacy_source()
    )


def test_catalog_initialization_preserves_legacy_until_existing_settings_are_imported() -> None:
    database = Path("/private/catalog.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy",
        db_path=None,
        revision=4,
        running_mode="legacy",
        running_db_path=None,
    )
    backend.load_legacy_source.return_value = _legacy_source()
    catalog = Mock(path=database)
    catalog.catalog_revision.return_value = 1
    backend.initialize_catalog.return_value = catalog

    result = CatalogService(backend).initialize(database)

    assert result.mode == "legacy"
    assert result.restart_required is False
    assert result.bootstrap_revision == 5
    backend.write_selection.assert_called_once_with(
        "legacy", database, expected_revision=4, expected_source=_legacy_source()
    )


def test_catalog_initialization_can_retry_after_bootstrap_cas_loss() -> None:
    database = Path.cwd() / "private" / "catalog.sqlite3"
    backend = Mock()
    backend.read_bootstrap.side_effect = [
        BootstrapSnapshot(mode="legacy", db_path=None, revision=3),
        BootstrapSnapshot(mode="legacy", db_path=Path.cwd() / "private" / "other.sqlite3", revision=4),
    ]
    backend.load_legacy_source.return_value = _empty_legacy_source()
    catalog = Mock(path=database)
    backend.initialize_catalog.return_value = catalog
    backend.write_selection.side_effect = [RevisionConflictError("bootstrap"), None]
    service = CatalogService(backend)

    with pytest.raises(RevisionConflictError):
        service.initialize(database)
    service.initialize(database)

    assert backend.initialize_catalog.call_args_list == [call(database), call(database)]
    assert backend.write_selection.call_args_list == [
        call("managed", database, expected_revision=3, expected_source=_empty_legacy_source()),
        call("managed", database, expected_revision=4, expected_source=_empty_legacy_source()),
    ]


def test_managed_selection_validates_effective_catalog_before_write() -> None:
    database = Path("/private/catalog.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=database)
    catalog = Mock()
    catalog.catalog_revision.return_value = 7
    backend.open_catalog.return_value = catalog

    result = CatalogService(backend).select(
        "managed",
        expected_bootstrap_revision=0,
        expected_catalog_revision=7,
    )

    assert result.mode == "managed"
    assert result.bootstrap_revision == 1
    assert result.restart_required is True
    catalog.validate_ready.assert_called_once_with()
    catalog.load_settings.assert_not_called()
    backend.write_selection.assert_called_once_with("managed", database, expected_revision=0)


def test_bound_account_mutation_rejects_selection_drift_even_when_revisions_match() -> None:
    backend = Mock()
    catalog_a = Mock()
    path_a = Path("/private/a.sqlite3")
    path_b = Path("/private/b.sqlite3")
    backend.read_bootstrap.side_effect = [
        BootstrapSnapshot(mode="legacy", db_path=path_a, revision=2, exists=True),
        BootstrapSnapshot(mode="legacy", db_path=path_b, revision=3, exists=True),
    ]
    backend.open_catalog.return_value = catalog_a

    accounts = ManagedAccountService(backend).bind(
        expected_bootstrap_revision=2,
        expected_catalog=path_a.as_posix(),
    )
    with pytest.raises(RevisionConflictError, match="bootstrap"):
        accounts.enable("alice", expected_revision=1)

    backend.open_catalog.assert_called_once_with(path_a)
    catalog_a.set_account_enabled.assert_not_called()


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


def test_repeated_exact_legacy_apply_verifies_secret_and_performs_guarded_cutover() -> None:
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
    matching_secret = "unicode-secret-密碼甲"  # noqa: S105 - synthetic Unicode regression value
    different_secret = "unicode-secret-密碼乙"  # noqa: S105 - synthetic Unicode regression value
    backend.resolve_legacy_secret.return_value = matching_secret
    catalog.load_account.return_value = EmailSettings(
        account_name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=_endpoint().model_copy(update={"password": SecretStr(matching_secret)}),
    )

    service = LegacyImportService(backend)
    catalog.catalog_revision.return_value = 5
    preview = service.preview()
    report = service.apply(
        preview_token=preview.preview_token,
        expected_revision=preview.target_revision,
        confirmation="",
    )

    assert report.plan.accounts[0].action == "unchanged"
    assert report.created == report.resumed == ()
    assert report.mode == "managed"
    assert report.bootstrap_revision == 1
    assert report.restart_required is True
    backend.guarded_import_cutover.assert_called_once_with(
        target_path=Path("catalog.sqlite3"),
        expected_bootstrap_revision=0,
        expected_source=source,
        expected_resolved_secrets=(("alice", "incoming", matching_secret),),
        expected_catalog_revision=5,
        expected_account_revisions=(("alice", 2, True),),
    )
    backend.write_selection.assert_not_called()
    backend.resolve_legacy_secret.assert_called_once_with("alice", "incoming", source_account)
    catalog.load_account.assert_called_once_with("alice", roles=("incoming",))
    catalog.add_account.assert_not_called()
    catalog.set_secret.assert_not_called()
    catalog.update_policy.assert_not_called()

    catalog.load_account.return_value = EmailSettings(
        account_name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=_endpoint().model_copy(update={"password": SecretStr(different_secret)}),
    )
    backend.guarded_import_cutover.reset_mock()
    conflicting_preview = service.preview()

    with pytest.raises(ManagementError, match="credentials differ") as caught:
        service.apply(
            preview_token=conflicting_preview.preview_token,
            expected_revision=conflicting_preview.target_revision,
            confirmation="",
        )

    assert caught.value.reason == "import_credential_conflict"
    backend.guarded_import_cutover.assert_not_called()


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


def test_legacy_apply_rechecks_selected_path_after_secret_resolution() -> None:
    backend = Mock()
    selected = Path("/private/a.sqlite3")
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=selected, revision=2, exists=True)
    backend.load_legacy_source.return_value = _legacy_source()
    catalog = Mock()
    catalog.catalog_revision.return_value = 5
    catalog.list_accounts.return_value = [
        AccountSummary("alice", "alice@example.test", True, 4, False, "MISSING", None)
    ]
    catalog.show_account.return_value = replace(_details(), incoming_binding="MISSING")
    catalog.policy.return_value = ManagedPolicy(5, False, (), (), False)
    backend.open_catalog.return_value = catalog
    service = LegacyImportService(backend)
    preview = service.preview()

    def switch_selection(*_args: object) -> str:
        backend.read_bootstrap.return_value = BootstrapSnapshot(
            mode="legacy",
            db_path=Path("/private/b.sqlite3"),
            revision=3,
            exists=True,
        )
        return "legacy-secret"

    backend.resolve_legacy_secret.side_effect = switch_selection
    with pytest.raises(ManagementError, match="target changed"):
        service.apply(
            preview_token=preview.preview_token,
            expected_revision=preview.target_revision,
            confirmation="IMPORT",
        )

    catalog.set_secret.assert_not_called()


def test_legacy_apply_rechecks_selected_path_immediately_before_policy_write() -> None:
    selected = Path("/private/a.sqlite3")
    source = LegacySourceSnapshot(
        accounts=(),
        unsupported_provider_names=(),
        enable_attachment_download=True,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy",
        db_path=selected,
        revision=2,
        exists=True,
    )
    source_reads = 0

    def load_source_and_switch_selection() -> LegacySourceSnapshot:
        nonlocal source_reads
        source_reads += 1
        if source_reads == 3:
            backend.read_bootstrap.return_value = BootstrapSnapshot(
                mode="legacy",
                db_path=Path("/private/b.sqlite3"),
                revision=3,
                exists=True,
            )
        return source

    backend.load_legacy_source.side_effect = load_source_and_switch_selection
    catalog = Mock()
    catalog.catalog_revision.return_value = 1
    catalog.list_accounts.return_value = []
    catalog.policy.return_value = ManagedPolicy(1, False, (), (), False)
    backend.open_catalog.return_value = catalog
    service = LegacyImportService(backend)
    preview = service.preview()

    with pytest.raises(ManagementError, match="target changed"):
        service.apply(
            preview_token=preview.preview_token,
            expected_revision=preview.target_revision,
            confirmation="IMPORT",
        )

    assert source_reads == 3
    catalog.update_policy.assert_not_called()


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
    catalog.list_accounts.return_value = []
    catalog.has_removed_account.return_value = False
    catalog.policy.return_value = ManagedPolicy(1, False, (), (), False)
    backend.open_catalog.return_value = catalog
    return backend, catalog


def test_import_with_unsupported_provider_keeps_legacy_selected() -> None:
    backend, catalog = _import_backend()
    backend.load_legacy_source.return_value = replace(
        _legacy_source(),
        unsupported_provider_names=("unsupported-provider",),
    )
    catalog.list_accounts.return_value = [AccountSummary("alice", "alice@example.test", True, 4, False, "ACTIVE", None)]
    catalog.show_account.return_value = _details()
    service = LegacyImportService(backend)
    preview = service.preview()

    report = service.apply(
        preview_token=preview.preview_token,
        expected_revision=preview.target_revision,
        confirmation="IMPORT",
    )

    assert report.mode == "legacy"
    assert report.restart_required is False
    backend.write_selection.assert_not_called()


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
    report = DoctorReport(2, 7, 1, 1, 0, ())
    backend.open_catalog.return_value.doctor.return_value = report

    status = CatalogService(backend).status()

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

    status = CatalogService(backend).status()

    assert status.mode == "managed"
    assert status.bootstrap_revision == 9
    assert status.report is None
    assert status.catalog_problem == "selected_catalog_unavailable"
    assert "sensitive" not in repr(status)


def test_selecting_legacy_uses_bootstrap_revision_without_opening_failed_catalog() -> None:
    database = Path("/private/catalog.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="managed", db_path=database, revision=4)

    CatalogService(backend).select("legacy", expected_bootstrap_revision=4)

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
        CatalogService(backend).select("legacy", expected_bootstrap_revision=4)

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
    assert result.category == "credential_unavailable"
    assert result.message == "Connection test failed before provider access; inspect account and credential state"
    assert "secret" not in result.message


@pytest.mark.parametrize(("entry_count", "accepted"), [(1_000, True), (1_001, False)])
def test_managed_policy_update_enforces_policy_entry_limit(entry_count: int, accepted: bool) -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy",
        db_path=Path("catalog.sqlite3"),
    )
    catalog = Mock()
    catalog.update_policy.return_value = 2
    backend.open_catalog.return_value = catalog
    policy = ManagedPolicy(
        revision=1,
        enable_attachment_download=False,
        allowed_recipients=tuple(f"user-{index}@example.test" for index in range(entry_count)),
        allowed_senders=(),
        report_blocked_mutations=False,
    )

    if accepted:
        result = PolicyManagementService(backend).update(policy)
        assert len(result.allowed_recipients) == 1_000
        catalog.update_policy.assert_called_once()
    else:
        with pytest.raises(ManagementError, match="recipient policy has too many entries"):
            PolicyManagementService(backend).update(policy)
        catalog.update_policy.assert_not_called()


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


def test_legacy_compatibility_freezes_and_preflights_only_managed_running_authority() -> None:
    backend = Mock()
    service = ManagementServices.compose(backend).legacy_compatibility
    backend.freeze_runtime_authority.return_value = "legacy"

    service.validate_runtime()

    backend.freeze_runtime_authority.assert_called_once_with()
    backend.validate_managed_runtime.assert_not_called()

    backend.freeze_runtime_authority.return_value = "managed"
    service.validate_runtime()

    assert backend.freeze_runtime_authority.call_count == 2
    backend.validate_managed_runtime.assert_called_once_with()


@pytest.mark.parametrize("operation", ["reset", "migrate"])
def test_legacy_compatibility_rejects_writes_in_managed_runtime(operation: str) -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy",
        db_path=Path("catalog.sqlite3"),
        running_mode="managed",
    )
    service = ManagementServices.compose(backend).legacy_compatibility

    with pytest.raises(ManagementError, match="config select legacy"):
        if operation == "reset":
            service.reset()
        else:
            service.migrate_credentials("plaintext")

    backend.reset_legacy_settings.assert_not_called()
    backend.migrate_legacy_credentials.assert_not_called()


def test_legacy_compatibility_delegates_migration_and_returns_bounded_report() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=None)
    expected = LegacyCredentialMigrationResult(2, ("alice:incoming",), (), "mcp-email-server")
    backend.migrate_legacy_credentials.return_value = expected

    result = ManagementServices.compose(backend).legacy_compatibility.migrate_credentials("plaintext")

    assert result == expected
    backend.migrate_legacy_credentials.assert_called_once_with("plaintext")


def test_default_initialization_uses_backend_path_and_bootstrap_revision() -> None:
    database = Path("/private/managed.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=None, revision=4, exists=True)
    backend.load_legacy_source.return_value = _empty_legacy_source()
    backend.default_catalog_path.return_value = database
    backend.initialize_catalog.return_value = Mock(path=database)

    result = CatalogService(backend).initialize_default(expected_bootstrap_revision=4)

    assert result.mode == "managed"
    assert result.database == database.as_posix()
    assert result.bootstrap_revision == 5
    backend.read_bootstrap.assert_called_once_with()
    backend.initialize_catalog.assert_called_once_with(database)
    backend.write_selection.assert_called_once_with(
        "managed",
        database,
        expected_revision=4,
        expected_exists=None,
        expected_source=_empty_legacy_source(),
    )


def test_default_initialization_reads_catalog_revision_before_committing_selection() -> None:
    database = Path("/private/managed.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=None, revision=4, exists=True)
    backend.load_legacy_source.return_value = _empty_legacy_source()
    backend.default_catalog_path.return_value = database
    catalog = Mock(path=database)
    selection_committed = False

    def read_catalog_revision() -> int:
        if selection_committed:
            raise ManagementError("post-commit read must not run")
        return 7

    def commit_selection(*_args: object, **_kwargs: object) -> None:
        nonlocal selection_committed
        selection_committed = True

    catalog.catalog_revision.side_effect = read_catalog_revision
    backend.initialize_catalog.return_value = catalog
    backend.write_selection.side_effect = commit_selection

    result = CatalogService(backend).initialize_default(expected_bootstrap_revision=4)

    assert selection_committed is True
    assert result.catalog_revision == 7
    catalog.catalog_revision.assert_called_once_with()


def test_automatic_default_initialization_binds_absent_bootstrap_proof() -> None:
    database = Path("/private/managed.sqlite3")
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=None, revision=0, exists=False)
    backend.default_catalog_path.return_value = database
    backend.load_legacy_source.return_value = _empty_legacy_source()
    backend.initialize_catalog.return_value = Mock(path=database)

    CatalogService(backend).initialize_default(
        expected_bootstrap_revision=0,
        require_empty_install=True,
    )

    backend.write_selection.assert_called_once_with(
        "managed",
        database,
        expected_revision=0,
        expected_exists=False,
        expected_source=_empty_legacy_source(),
    )


def test_automatic_default_initialization_rejects_new_effective_legacy_content() -> None:
    backend = Mock()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=None, revision=0, exists=False)
    backend.load_legacy_source.return_value = _legacy_source()

    with pytest.raises(RevisionConflictError, match="bootstrap"):
        CatalogService(backend).initialize_default(
            expected_bootstrap_revision=0,
            require_empty_install=True,
        )

    backend.default_catalog_path.assert_not_called()
    backend.initialize_catalog.assert_not_called()
    backend.write_selection.assert_not_called()


def test_default_initialization_is_idempotent_only_for_the_selected_default() -> None:
    database = Path("/private/managed.sqlite3")
    backend = Mock()
    backend.default_catalog_path.return_value = database
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=database, revision=2, exists=True)
    backend.load_legacy_source.return_value = _empty_legacy_source()
    backend.open_catalog.return_value.path = database
    result = CatalogService(backend).initialize_default(expected_bootstrap_revision=2)
    assert result.mode == "managed"
    assert result.database == database.as_posix()
    backend.open_catalog.return_value.validate_ready.assert_called_once_with()
    backend.initialize_catalog.assert_not_called()
    backend.write_selection.assert_called_once_with(
        "managed",
        database,
        expected_revision=2,
        expected_exists=None,
        expected_source=_empty_legacy_source(),
    )

    backend.read_bootstrap.return_value = BootstrapSnapshot(
        mode="legacy", db_path=Path("/private/other.sqlite3"), revision=2, exists=True
    )
    with pytest.raises(ManagementError, match="different managed database"):
        CatalogService(backend).initialize_default(expected_bootstrap_revision=2)


def test_legacy_preview_binds_apply_to_the_selected_catalog_path() -> None:
    backend, catalog = _import_backend()
    service = LegacyImportService(backend)
    preview = service.preview()
    backend.read_bootstrap.return_value = BootstrapSnapshot(mode="legacy", db_path=Path("other.sqlite3"), revision=0)

    with pytest.raises(ManagementError, match="target changed"):
        service.apply(
            preview_token=preview.preview_token,
            expected_revision=preview.target_revision,
            confirmation="IMPORT",
        )

    backend.resolve_legacy_secret.assert_not_called()
    catalog.add_account.assert_not_called()


def test_legacy_preview_reports_normalized_name_collision_as_conflict() -> None:
    backend, catalog = _import_backend()
    source = _legacy_source()
    backend.load_legacy_source.return_value = replace(
        source,
        accounts=(replace(source.accounts[0], name="ALICE"),),
    )
    catalog.list_accounts.return_value = [AccountSummary("alice", "alice@example.test", True, 2, False, "ACTIVE", None)]
    catalog.show_account.return_value = _details()

    preview = LegacyImportService(backend).preview()

    assert preview.accounts[0].action == "conflict"
    assert preview.accounts[0].expected_target_revision == 4
    backend.resolve_legacy_secret.assert_not_called()


def test_legacy_preview_cache_is_strictly_bounded() -> None:
    backend, _catalog = _import_backend()
    service = LegacyImportService(backend)

    tokens = {service.preview().preview_token for _ in range(service._MAX_PREVIEWS + 5)}

    assert len(tokens) == service._MAX_PREVIEWS + 5
    assert len(service._previews) == service._MAX_PREVIEWS
