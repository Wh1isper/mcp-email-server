from __future__ import annotations

import asyncio
import contextlib
import os
import traceback
from collections.abc import Iterator, MutableMapping
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import aiosmtplib
import pytest

from mcp_email_server import keyring_store
from mcp_email_server.adapters import management as management_module
from mcp_email_server.adapters.management import LocalManagementBackend
from mcp_email_server.application.management import (
    BindingRole,
    ConnectivityCheckError,
    EndpointSummary,
    LegacyAccountSnapshot,
    LegacySourceSnapshot,
    ManagementError,
)
from mcp_email_server.bootstrap import BootstrapError, read_bootstrap, write_bootstrap
from mcp_email_server.config import EmailSettings, Settings
from mcp_email_server.emails.classic import ImapAuthenticationError
from mcp_email_server.managed import ManagedCatalog


def _legacy_raw() -> dict[str, object]:
    return {
        "emails": [
            {
                "account_name": "alice",
                "full_name": "Alice",
                "email_address": "alice@example.test",
                "incoming": {
                    "host": "imap.example.test",
                    "port": 993,
                    "user_name": "alice@example.test",
                    "password": "legacy-secret",
                    "use_ssl": True,
                },
            }
        ]
    }


def test_bootstrap_running_snapshot_failure_maps_to_bounded_management_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = MagicMock()
    bootstrap.path = Path("/private/bootstrap.toml")
    monkeypatch.setattr(management_module, "read_bootstrap", lambda: bootstrap)

    def fail_running_snapshot(_path: Path) -> None:
        raise BootstrapError("private bootstrap path")

    monkeypatch.setattr(management_module, "process_bootstrap", fail_running_snapshot)

    with pytest.raises(ManagementError) as caught:
        LocalManagementBackend().read_bootstrap()

    assert caught.value.reason == "bootstrap_unavailable"
    assert str(caught.value) == "Bootstrap configuration could not be read"


def test_legacy_secret_resolution_rejects_changed_source_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalManagementBackend()
    original = _legacy_raw()
    expected = backend._effective_legacy_accounts(original)[0]
    changed = deepcopy(original)
    changed_accounts = changed["emails"]
    assert isinstance(changed_accounts, list)
    changed_account = changed_accounts[0]
    assert isinstance(changed_account, dict)
    changed_incoming = changed_account["incoming"]
    assert isinstance(changed_incoming, dict)
    changed_incoming["host"] = "imap.changed.example.test"
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: changed)

    with pytest.raises(ManagementError, match=r"source changed.*preview and retry"):
        backend.resolve_legacy_secret("alice", "incoming", expected)


def _legacy_expected(backend: LocalManagementBackend, raw: dict[str, object]) -> LegacyAccountSnapshot:
    return backend._effective_legacy_accounts(raw)[0]


def test_legacy_snapshot_exposes_only_credential_source_class() -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    plaintext = backend._effective_legacy_accounts(raw)[0]
    incoming = raw["emails"]
    assert isinstance(incoming, list)
    account_raw = incoming[0]
    assert isinstance(account_raw, dict)
    endpoint = account_raw["incoming"]
    assert isinstance(endpoint, dict)
    endpoint["password"] = keyring_store.SENTINEL
    keyring = backend._effective_legacy_accounts(raw)[0]

    assert plaintext.incoming_secret_source == "plaintext"  # noqa: S105 - source class
    assert keyring.incoming_secret_source == "keyring"  # noqa: S105 - source class
    assert "legacy-secret" not in repr(plaintext)


def test_legacy_secret_resolution_reads_keyring_only_for_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    accounts = raw["emails"]
    assert isinstance(accounts, list)
    account = accounts[0]
    assert isinstance(account, dict)
    incoming = account["incoming"]
    assert isinstance(incoming, dict)
    incoming["password"] = keyring_store.SENTINEL
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: raw)
    get_secret = Mock(return_value="resolved-secret")
    monkeypatch.setattr(keyring_store, "get_secret", get_secret)

    assert backend.resolve_legacy_secret("alice", "incoming", _legacy_expected(backend, raw)) == "resolved-secret"
    get_secret.assert_called_once_with("alice", "incoming")


@pytest.mark.parametrize(
    ("keyring_result", "message"),
    [
        (None, "Stored legacy credential is unavailable"),
        ("", "Stored legacy credential is empty"),
        (RuntimeError("backend detail"), "Stored legacy credential backend is unavailable"),
    ],
)
def test_legacy_secret_resolution_sanitizes_unavailable_keyring(
    monkeypatch: pytest.MonkeyPatch,
    keyring_result: str | BaseException | None,
    message: str,
) -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    accounts = raw["emails"]
    assert isinstance(accounts, list)
    account = accounts[0]
    assert isinstance(account, dict)
    incoming = account["incoming"]
    assert isinstance(incoming, dict)
    incoming["password"] = keyring_store.SENTINEL
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: raw)
    get_secret = Mock(side_effect=keyring_result if isinstance(keyring_result, BaseException) else None)
    if not isinstance(keyring_result, BaseException):
        get_secret.return_value = keyring_result
    monkeypatch.setattr(keyring_store, "get_secret", get_secret)

    with pytest.raises(ManagementError, match=f"^{message}$") as caught:
        backend.resolve_legacy_secret("alice", "incoming", _legacy_expected(backend, raw))

    formatted = "".join(traceback.format_exception(caught.value))
    assert "backend detail" not in str(caught.value)
    assert "backend detail" not in formatted
    assert caught.value.__cause__ is None


def test_legacy_secret_resolution_rejects_absent_role(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: raw)

    with pytest.raises(ManagementError, match="no credential for that role"):
        backend.resolve_legacy_secret("alice", "outgoing", _legacy_expected(backend, raw))


@pytest.mark.asyncio
async def test_connection_checks_incoming_mailboxes(
    monkeypatch: pytest.MonkeyPatch,
    email_settings: EmailSettings,
) -> None:
    catalog = Mock()
    catalog.load_account.return_value = email_settings
    handler = MagicMock()
    handler.list_mailboxes = AsyncMock()
    monkeypatch.setattr(management_module, "ClassicEmailHandler", Mock(return_value=handler))

    await LocalManagementBackend().test_connection(catalog, "test_account", "incoming")

    catalog.load_account.assert_called_once_with("test_account", roles=("incoming",))
    handler.list_mailboxes.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "category"),
    [
        (asyncio.CancelledError(), None),
        (ImapAuthenticationError("provider secret detail"), "authentication_or_provider_rejected"),
        (TimeoutError("provider secret detail"), "timeout"),
        (RuntimeError("provider secret detail"), "tls_or_connection_failed"),
    ],
)
async def test_connection_propagates_cancellation_and_sanitizes_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    email_settings: EmailSettings,
    error: BaseException,
    category: str | None,
) -> None:
    catalog = Mock()
    catalog.load_account.return_value = email_settings
    handler = MagicMock()
    handler.list_mailboxes = AsyncMock(side_effect=error)
    monkeypatch.setattr(management_module, "ClassicEmailHandler", Mock(return_value=handler))

    expected = type(error) if isinstance(error, asyncio.CancelledError) else ConnectivityCheckError
    with pytest.raises(expected) as caught:
        await LocalManagementBackend().test_connection(catalog, "test_account", "incoming")

    if isinstance(error, asyncio.CancelledError):
        assert caught.value is error
    else:
        assert isinstance(caught.value, ConnectivityCheckError)
        assert caught.value.category == category
        assert "provider secret detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_connection_logs_in_to_outgoing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    email_settings: EmailSettings,
) -> None:
    catalog = Mock()
    catalog.load_account.return_value = email_settings
    tls_context = object()
    handler = MagicMock()
    handler.outgoing_client.smtp_start_tls = False
    handler.outgoing_client.smtp_use_tls = True
    handler.outgoing_client._get_smtp_ssl_context.return_value = tls_context
    monkeypatch.setattr(management_module, "ClassicEmailHandler", Mock(return_value=handler))
    smtp = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=smtp)
    context.__aexit__ = AsyncMock(return_value=None)
    smtp_factory = Mock(return_value=context)
    monkeypatch.setattr(aiosmtplib, "SMTP", smtp_factory)

    await LocalManagementBackend().test_connection(catalog, "test_account", "outgoing")

    assert email_settings.outgoing is not None
    smtp_factory.assert_called_once_with(
        hostname=email_settings.outgoing.host,
        port=email_settings.outgoing.port,
        start_tls=False,
        use_tls=True,
        tls_context=tls_context,
    )
    smtp.login.assert_awaited_once_with(
        email_settings.outgoing.user_name,
        email_settings.outgoing.password.get_secret_value(),
    )
    context.__aexit__.assert_awaited_once_with(None, None, None)


@pytest.mark.asyncio
async def test_connection_classifies_real_catalog_without_smtp_before_secret_resolution(tmp_path: Path) -> None:
    parent = tmp_path / "managed"
    parent.mkdir(mode=0o700)
    if os.name == "posix":
        parent.chmod(0o700)
    catalog = ManagedCatalog.initialize(parent / "catalog.sqlite3")
    catalog.add_account(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=EndpointSummary(
            host="imap.example.test",
            port=993,
            use_ssl=True,
            start_ssl=False,
            verify_ssl=True,
            user_name="alice@example.test",
        ),
        outgoing=None,
    )

    with pytest.raises(ConnectivityCheckError, match=r"^Outgoing endpoint is unavailable") as caught:
        await LocalManagementBackend().test_connection(catalog, "alice", "outgoing")

    assert caught.value.category == "endpoint_unavailable"


@pytest.mark.asyncio
async def test_connection_preserves_missing_outgoing_capability(
    monkeypatch: pytest.MonkeyPatch,
    email_settings: EmailSettings,
) -> None:
    account = email_settings.model_copy(update={"outgoing": None})
    catalog = Mock()
    catalog.load_account.return_value = account
    handler = MagicMock()
    handler.outgoing_client = None
    monkeypatch.setattr(management_module, "ClassicEmailHandler", Mock(return_value=handler))

    with pytest.raises(ConnectivityCheckError, match=r"^Outgoing endpoint is unavailable") as caught:
        await LocalManagementBackend().test_connection(catalog, "test_account", "outgoing")
    assert caught.value.category == "endpoint_unavailable"


def _set_environment_account(monkeypatch: pytest.MonkeyPatch, *, name: str = "environment") -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", name)
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", f"{name}@example.test")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "environment-secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.environment.example.test")


def test_effective_legacy_source_supports_environment_only_without_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalManagementBackend()
    _set_environment_account(monkeypatch)
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: {})
    monkeypatch.setenv("MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD", "true")
    monkeypatch.setenv("MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS", " BOB@EXAMPLE.TEST, bob@example.test ")
    monkeypatch.setenv("MCP_EMAIL_SERVER_ALLOWED_SENDERS", " *@Example.Test ")
    monkeypatch.setenv("MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS", "true")

    source = backend.load_legacy_source()

    assert [account.name for account in source.accounts] == ["environment"]
    assert source.accounts[0].incoming_secret_source == "environment"  # noqa: S105 - source class
    assert "environment-secret" not in repr(source)
    assert source.enable_attachment_download is True
    assert source.allowed_recipients == ("bob@example.test",)
    assert source.allowed_senders == ("*@example.test",)
    assert source.report_blocked_mutations is True
    assert backend.resolve_legacy_secret("environment", "incoming", source.accounts[0]) == "environment-secret"


def test_environment_empty_role_passwords_fall_back_to_base_like_legacy_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalManagementBackend()
    _set_environment_account(monkeypatch)
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_PASSWORD", "")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.environment.example.test")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_PASSWORD", "")
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: {})

    account = backend.load_legacy_source().accounts[0]

    assert backend.resolve_legacy_secret("environment", "incoming", account) == "environment-secret"
    assert backend.resolve_legacy_secret("environment", "outgoing", account) == "environment-secret"


def test_environment_preview_does_not_read_password_values(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalManagementBackend()
    _set_environment_account(monkeypatch)
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: {})
    original = os.environ

    class GuardedEnvironment(MutableMapping[str, str]):
        def __getitem__(self, name: str) -> str:
            if "PASSWORD" in name:
                raise AssertionError("preview read an environment password value")
            return original[name]

        def __iter__(self) -> Iterator[str]:
            return iter(original)

        def __len__(self) -> int:
            return len(original)

        def __setitem__(self, name: str, value: str) -> None:
            original[name] = value

        def __delitem__(self, name: str) -> None:
            del original[name]

    monkeypatch.setattr(management_module.os, "environ", GuardedEnvironment())

    source = backend.load_legacy_source()

    assert source.accounts[0].incoming_secret_source == "environment"  # noqa: S105 - source class


def test_legacy_preview_rejects_raw_policy_cardinality_before_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    raw["allowed_recipients"] = ["duplicate@example.test"] * 1001
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: raw)

    with pytest.raises(ManagementError, match="Stored legacy policy is invalid"):
        backend.load_legacy_source()


def test_legacy_preview_rejects_oversized_non_secret_fields() -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    accounts = raw["emails"]
    assert isinstance(accounts, list)
    account = accounts[0]
    assert isinstance(account, dict)
    incoming = account["incoming"]
    assert isinstance(incoming, dict)
    incoming["host"] = "x" * 100_000

    with pytest.raises(ManagementError, match="legacy email accounts are invalid"):
        backend._effective_legacy_accounts(raw)


def test_effective_legacy_environment_account_replaces_exact_name_and_preserves_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    _set_environment_account(monkeypatch, name="alice")
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: raw)

    replaced = backend.load_legacy_source()

    assert [account.name for account in replaced.accounts] == ["alice"]
    assert replaced.accounts[0].incoming.host == "imap.environment.example.test"
    assert replaced.accounts[0].incoming_secret_source == "environment"  # noqa: S105 - source class

    _set_environment_account(monkeypatch, name="environment")
    added = backend.load_legacy_source()
    assert [account.name for account in added.accounts] == ["environment", "alice"]
    assert added.accounts[0].incoming_secret_source == "environment"  # noqa: S105 - source class
    assert added.accounts[1].incoming_secret_source == "plaintext"  # noqa: S105 - source class


def test_environment_credential_resolution_rejects_changed_effective_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalManagementBackend()
    _set_environment_account(monkeypatch)
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: {})
    expected = backend.load_legacy_source().accounts[0]
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.changed.example.test")

    with pytest.raises(ManagementError, match=r"source changed.*preview and retry"):
        backend.resolve_legacy_secret("environment", "incoming", expected)


def test_legacy_migration_maps_bootstrap_lock_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = BootstrapError("private path detail")
    monkeypatch.setattr(Settings, "migrate_credentials", Mock(side_effect=failure))

    with pytest.raises(ManagementError, match="bootstrap authority is invalid or busy") as caught:
        LocalManagementBackend().migrate_legacy_credentials("plaintext")

    assert "private path detail" not in str(caught.value)
    assert caught.value.__cause__ is failure


def test_legacy_migration_maps_filesystem_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = OSError("private path detail")
    monkeypatch.setattr(Settings, "migrate_credentials", Mock(side_effect=failure))

    with pytest.raises(ManagementError, match="migration storage is unavailable") as caught:
        LocalManagementBackend().migrate_legacy_credentials("plaintext")

    assert "private path detail" not in str(caught.value)
    assert caught.value.__cause__ is failure


def test_absent_legacy_file_is_an_empty_import_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(management_module, "configured_path", lambda: tmp_path / "missing.toml")

    assert LocalManagementBackend()._read_legacy_raw() == {}


def _guarded_cutover_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[LocalManagementBackend, LegacySourceSnapshot, ManagedCatalog, Path]:
    parent = tmp_path / "guarded-cutover"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    config_path = parent / "config.toml"
    database = parent / "managed.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: raw)
    account = backend._effective_legacy_accounts(raw)[0]
    source = LegacySourceSnapshot(
        accounts=(account,),
        unsupported_provider_names=(),
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    catalog = ManagedCatalog.initialize(database)
    write_bootstrap(mode="legacy", db_path=database, path=config_path, expected_revision=0)
    return backend, source, catalog, config_path


def test_guarded_import_cutover_rejects_final_catalog_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend, source, catalog, config_path = _guarded_cutover_fixture(monkeypatch, tmp_path)
    catalog.add_account(
        name="concurrent",
        full_name="Concurrent",
        email_address="concurrent@example.test",
        incoming=EndpointSummary(
            host="imap.example.test",
            port=993,
            use_ssl=True,
            start_ssl=False,
            verify_ssl=True,
            user_name="concurrent@example.test",
        ),
        outgoing=None,
        expected_revision=1,
    )

    with pytest.raises(ManagementError, match="revision"):
        backend.guarded_import_cutover(
            target_path=catalog.path,
            expected_bootstrap_revision=1,
            expected_source=source,
            expected_resolved_secrets=(),
            expected_catalog_revision=1,
            expected_account_revisions=(),
        )

    assert read_bootstrap(config_path).mode == "legacy"


def test_guarded_import_cutover_rechecks_private_credential_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend, source, catalog, config_path = _guarded_cutover_fixture(monkeypatch, tmp_path)

    with pytest.raises(ManagementError, match="credential source changed"):
        backend.guarded_import_cutover(
            target_path=catalog.path,
            expected_bootstrap_revision=1,
            expected_source=source,
            expected_resolved_secrets=(("alice", "incoming", "different-secret"),),
            expected_catalog_revision=1,
            expected_account_revisions=(),
        )

    assert read_bootstrap(config_path).mode == "legacy"


def test_guarded_import_cutover_resolves_private_values_before_catalog_writer_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend, source, catalog, config_path = _guarded_cutover_fixture(monkeypatch, tmp_path)
    original_guard = ManagedCatalog.import_cutover_guard
    original_resolve = backend.resolve_legacy_secret
    in_catalog_guard = False
    resolved = False

    @contextlib.contextmanager
    def observed_guard(
        self: ManagedCatalog,
        *,
        expected_catalog_revision: int,
        expected_account_revisions: tuple[tuple[str, int, bool], ...],
    ) -> Iterator[None]:
        nonlocal in_catalog_guard
        in_catalog_guard = True
        try:
            with original_guard(
                self,
                expected_catalog_revision=expected_catalog_revision,
                expected_account_revisions=expected_account_revisions,
            ):
                yield
        finally:
            in_catalog_guard = False

    def observed_resolve(
        account_name: str,
        role: BindingRole,
        expected_account: LegacyAccountSnapshot,
    ) -> str:
        nonlocal resolved
        assert not in_catalog_guard
        resolved = True
        return original_resolve(account_name, role, expected_account)

    monkeypatch.setattr(ManagedCatalog, "import_cutover_guard", observed_guard)
    monkeypatch.setattr(backend, "resolve_legacy_secret", observed_resolve)

    backend.guarded_import_cutover(
        target_path=catalog.path,
        expected_bootstrap_revision=1,
        expected_source=source,
        expected_resolved_secrets=(("alice", "incoming", "legacy-secret"),),
        expected_catalog_revision=1,
        expected_account_revisions=(),
    )

    assert resolved is True
    assert read_bootstrap(config_path).mode == "managed"


def test_guarded_import_cutover_commits_only_after_final_source_and_target_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend, source, catalog, config_path = _guarded_cutover_fixture(monkeypatch, tmp_path)

    backend.guarded_import_cutover(
        target_path=catalog.path,
        expected_bootstrap_revision=1,
        expected_source=source,
        expected_resolved_secrets=(("alice", "incoming", "legacy-secret"),),
        expected_catalog_revision=1,
        expected_account_revisions=(),
    )

    selected = read_bootstrap(config_path)
    assert selected.mode == "managed"
    assert selected.revision == 2
    assert selected.db_path == catalog.path
