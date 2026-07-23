from __future__ import annotations

import asyncio
import traceback
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, Mock

import aiosmtplib
import pytest

from mcp_email_server import keyring_store
from mcp_email_server.adapters import management as management_module
from mcp_email_server.adapters.management import LocalManagementBackend
from mcp_email_server.application.management import LegacyAccountSnapshot, ManagementError
from mcp_email_server.config import EmailSettings


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


def test_legacy_secret_resolution_rejects_changed_source_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalManagementBackend()
    original = _legacy_raw()
    account = backend._parse_legacy_accounts(original)[0]
    expected = backend._legacy_account_snapshot(account)
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
    return backend._legacy_account_snapshot(backend._parse_legacy_accounts(raw)[0])


def test_legacy_snapshot_exposes_only_credential_source_class() -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    account = backend._parse_legacy_accounts(raw)[0]
    plaintext = backend._legacy_account_snapshot(account)
    incoming = raw["emails"]
    assert isinstance(incoming, list)
    account_raw = incoming[0]
    assert isinstance(account_raw, dict)
    endpoint = account_raw["incoming"]
    assert isinstance(endpoint, dict)
    endpoint["password"] = keyring_store.SENTINEL
    keyring_account = backend._parse_legacy_accounts(raw)[0]
    keyring = backend._legacy_account_snapshot(keyring_account)

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
@pytest.mark.parametrize("error", [asyncio.CancelledError(), RuntimeError("provider secret detail")])
async def test_connection_propagates_cancellation_and_sanitizes_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    email_settings: EmailSettings,
    error: BaseException,
) -> None:
    catalog = Mock()
    catalog.load_account.return_value = email_settings
    handler = MagicMock()
    handler.list_mailboxes = AsyncMock(side_effect=error)
    monkeypatch.setattr(management_module, "ClassicEmailHandler", Mock(return_value=handler))

    expected = type(error) if isinstance(error, asyncio.CancelledError) else ManagementError
    with pytest.raises(expected) as caught:
        await LocalManagementBackend().test_connection(catalog, "test_account", "incoming")

    if isinstance(error, asyncio.CancelledError):
        assert caught.value is error
    else:
        assert str(caught.value) == "incoming connectivity test failed: RuntimeError"
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

    with pytest.raises(ManagementError, match=r"^Managed account has no outgoing endpoint$"):
        await LocalManagementBackend().test_connection(catalog, "test_account", "outgoing")
