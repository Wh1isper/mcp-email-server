from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from mcp_email_server.adapters.metadata import LocalMetadataBackend
from mcp_email_server.application.accounts import EffectiveAccountQueryService
from mcp_email_server.config import EmailServer, EmailSettings


def _account() -> EmailSettings:
    return EmailSettings(
        account_name="work",
        full_name="Work User",
        email_address="user@example.test",
        incoming=EmailServer(
            user_name="user@example.test",
            password=SecretStr("secret"),
            host="imap.example.test",
            port=993,
        ),
    )


def test_effective_account_query_service_uses_injected_source() -> None:
    source = MagicMock()
    source.list_effective_accounts.return_value = [_account().masked()]

    accounts = EffectiveAccountQueryService(source).execute()

    assert accounts[0].account_name == "work"
    source.list_effective_accounts.assert_called_once_with()


@pytest.mark.parametrize("mode, reload", [("legacy", False), ("managed", True)])
def test_local_account_adapter_uses_selected_mode_authority_and_masks_secrets(mode: str, reload: bool) -> None:
    settings = MagicMock()
    settings.get_accounts.return_value = [_account()]
    with (
        patch("mcp_email_server.adapters.metadata.process_bootstrap", return_value=SimpleNamespace(mode=mode)),
        patch("mcp_email_server.adapters.metadata.get_settings", return_value=settings) as get_settings,
    ):
        accounts = LocalMetadataBackend().list_effective_accounts()

    assert isinstance(accounts[0], EmailSettings)
    assert accounts[0].account_name == "work"
    assert accounts[0].incoming.password.get_secret_value() == "********"
    get_settings.assert_called_once_with(reload=reload)
