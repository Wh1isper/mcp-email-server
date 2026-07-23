from pathlib import Path
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


def test_local_authority_requires_managed_database_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        authority_module,
        "process_bootstrap",
        lambda _config_path: SimpleNamespace(mode="managed", db_path=None),
    )

    with pytest.raises(RuntimeError, match=r"^Managed mode requires a database path$"):
        authority_module.resolve_local_account("primary")


def test_local_authority_sanitizes_missing_managed_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(
        authority_module,
        "process_bootstrap",
        lambda _config_path: SimpleNamespace(mode="managed", db_path=database),
    )
    catalog = Mock()
    catalog.resolve_account.side_effect = authority_module.ManagedCatalogError("account not found or is disabled")
    monkeypatch.setattr(authority_module, "ManagedCatalog", Mock(return_value=catalog))

    with pytest.raises(ValueError, match=r"^Account missing was not found$"):
        authority_module.resolve_local_account("missing")


def test_local_authority_preserves_other_managed_catalog_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(
        authority_module,
        "process_bootstrap",
        lambda _config_path: SimpleNamespace(mode="managed", db_path=database),
    )
    catalog = Mock()
    error = authority_module.ManagedCatalogError("catalog unavailable")
    catalog.resolve_account.side_effect = error
    monkeypatch.setattr(authority_module, "ManagedCatalog", Mock(return_value=catalog))

    with pytest.raises(authority_module.ManagedCatalogError) as caught:
        authority_module.resolve_local_account("primary")

    assert caught.value is error
