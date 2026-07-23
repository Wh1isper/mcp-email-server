from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mcp_email_server.adapters import authority as authority_module
from mcp_email_server.config import EmailSettings, ProviderSettings, Settings


def _use_legacy_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        authority_module,
        "process_bootstrap",
        lambda _config_path: SimpleNamespace(mode="legacy", db_path=None),
    )


def test_local_authority_rejects_mode_change_before_account_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_legacy_bootstrap(monkeypatch)
    get_settings = Mock()
    monkeypatch.setattr(authority_module, "get_settings", get_settings)

    with pytest.raises(RuntimeError, match=r"^Configuration mode changed; restart required$"):
        authority_module.resolve_local_account("primary", expected_mode="managed")

    get_settings.assert_not_called()


def test_local_authority_resolves_legacy_email_account(
    monkeypatch: pytest.MonkeyPatch,
    email_settings: EmailSettings,
) -> None:
    _use_legacy_bootstrap(monkeypatch)
    settings = Mock(spec=Settings)
    settings.get_account.return_value = email_settings
    monkeypatch.setattr(authority_module, "get_settings", Mock(return_value=settings))

    resolution = authority_module.resolve_local_account("primary", roles=("incoming",))

    assert resolution.mode == "legacy"
    assert resolution.settings is settings
    assert resolution.account is email_settings
    settings.get_account.assert_called_once_with("primary")


def test_local_authority_rejects_legacy_provider_account(
    monkeypatch: pytest.MonkeyPatch,
    provider_settings: ProviderSettings,
) -> None:
    _use_legacy_bootstrap(monkeypatch)
    settings = Mock(spec=Settings)
    settings.get_account.return_value = provider_settings
    monkeypatch.setattr(authority_module, "get_settings", Mock(return_value=settings))

    with pytest.raises(NotImplementedError):
        authority_module.resolve_local_account("provider")


def test_local_authority_reports_missing_legacy_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_legacy_bootstrap(monkeypatch)
    settings = Mock(spec=Settings)
    settings.get_account.return_value = None
    monkeypatch.setattr(authority_module, "get_settings", Mock(return_value=settings))

    with pytest.raises(ValueError, match=r"^Account missing was not found$"):
        authority_module.resolve_local_account("missing")
