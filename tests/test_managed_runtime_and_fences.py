from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from mcp_email_server import bootstrap as bootstrap_module
from mcp_email_server import config as config_module
from mcp_email_server import keyring_store
from mcp_email_server.app import add_email_account
from mcp_email_server.bootstrap import BOOTSTRAP_VERSION, ManagedModeWriteError, freeze_process_bootstrap
from mcp_email_server.cli import app
from mcp_email_server.config import (
    EmailServer,
    EmailSettings,
    Settings,
    clear_settings_cache,
    delete_settings,
    get_settings,
)
from mcp_email_server.emails.dispatcher import dispatch_handler
from mcp_email_server.managed import ManagedCatalog


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def _select_managed(monkeypatch, tmp_path: Path, fake_keyring, *, activate: bool = True):
    parent = _private_directory(tmp_path / "managed")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    catalog = ManagedCatalog.initialize(database)
    catalog.add_account(
        name="managed-alice",
        full_name="Managed Alice",
        email_address="alice@example.test",
        incoming=EmailServer(
            user_name="alice@example.test",
            password=SecretStr("runtime-secret"),
            host="imap.example.test",
            port=993,
        ),
        outgoing=None,
    )
    catalog.set_secret("managed-alice", "incoming", "runtime-secret")
    if activate:
        catalog.activate()
    config_path.write_text(
        f"bootstrap_version = {BOOTSTRAP_VERSION}\n"
        'mode = "managed"\n'
        f'db_location = "{database.as_posix()}"\n'
        'credential_storage = "plaintext"\n'
        '[[emails]]\naccount_name = "legacy-tripwire"\n'
    )
    config_path.chmod(0o600)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    clear_settings_cache()
    return config_path, catalog


def test_managed_get_settings_uses_catalog_not_legacy_or_environment(monkeypatch, tmp_path, fake_keyring):
    _select_managed(monkeypatch, tmp_path, fake_keyring)
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "env@example.test")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "environment-secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "env-imap.example.test")

    settings = get_settings(reload=True)

    assert [account.account_name for account in settings.emails] == ["managed-alice"]
    assert settings.emails[0].incoming.password.get_secret_value() == "runtime-secret"
    assert all(account.account_name not in {"legacy-tripwire", "default"} for account in settings.emails)


def test_unavailable_managed_keyring_never_falls_back_to_plaintext(tmp_path, broken_keyring):
    parent = _private_directory(tmp_path / "managed-keyring")
    catalog = ManagedCatalog.initialize(parent / "catalog.sqlite3")
    catalog.add_account(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=EmailServer(
            user_name="alice@example.test",
            password=SecretStr("must-not-persist"),
            host="imap.example.test",
            port=993,
        ),
        outgoing=None,
    )

    with pytest.raises(Exception, match="backend rejected"):
        catalog.set_secret("alice", "incoming", "must-not-persist")

    assert b"must-not-persist" not in catalog.path.read_bytes()
    assert catalog.doctor().pending_bindings == 1


def test_running_managed_process_keeps_writer_fence_after_external_legacy_selection(
    monkeypatch, tmp_path, fake_keyring
):
    config_path, _catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    monkeypatch.setattr(bootstrap_module, "_PROCESS_BOOTSTRAP", None)
    freeze_process_bootstrap(config_path)
    content = config_path.read_text().replace('mode = "managed"', 'mode = "legacy"')
    config_path.write_text(content)
    config_path.chmod(0o600)
    fake_keyring.calls.clear()

    with pytest.raises(ManagedModeWriteError):
        Settings.model_construct(emails=[], providers=[]).store()

    assert fake_keyring.calls == []
    assert config_path.read_text() == content


def test_running_legacy_process_preserves_external_managed_selection(monkeypatch, tmp_path, fake_keyring):
    config_path, catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    legacy_content = config_path.read_text().replace('mode = "managed"', 'mode = "legacy"')
    config_path.write_text(legacy_content)
    config_path.chmod(0o600)
    monkeypatch.setattr(bootstrap_module, "_PROCESS_BOOTSTRAP", None)
    freeze_process_bootstrap(config_path)
    config_path.write_text(legacy_content.replace('mode = "legacy"', 'mode = "managed"'))
    config_path.chmod(0o600)
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)

    Settings.model_construct(emails=[], providers=[], credential_storage="plaintext").store()

    durable = bootstrap_module.read_bootstrap(config_path)
    assert durable.mode == "managed"
    assert durable.db_path == catalog.path


def test_disable_is_revalidated_before_next_provider_dispatch(monkeypatch, tmp_path, fake_keyring):
    _config_path, catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    monkeypatch.setattr(bootstrap_module, "_PROCESS_BOOTSTRAP", None)
    freeze_process_bootstrap(_config_path)
    get_settings(reload=True)
    assert dispatch_handler("managed-alice").email_settings.account_name == "managed-alice"

    catalog.disable_account("managed-alice")

    with pytest.raises(ValueError, match="not found"):
        dispatch_handler("managed-alice")


def test_managed_missing_database_does_not_fall_back_to_legacy(monkeypatch, tmp_path):
    parent = _private_directory(tmp_path / "managed")
    config_path = parent / "config.toml"
    config_path.write_text(
        f"bootstrap_version = {BOOTSTRAP_VERSION}\n"
        'mode = "managed"\n'
        f'db_location = "{(parent / "missing.sqlite3").as_posix()}"\n'
        '[[emails]]\naccount_name = "legacy-tripwire"\n'
    )
    config_path.chmod(0o600)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    clear_settings_cache()

    with pytest.raises(Exception, match="missing"):
        get_settings(reload=True)


def test_managed_staging_catalog_does_not_fall_back_to_legacy(monkeypatch, tmp_path, fake_keyring):
    _select_managed(monkeypatch, tmp_path, fake_keyring, activate=False)

    with pytest.raises(Exception, match="not active"):
        get_settings(reload=True)


def test_settings_store_and_delete_are_fenced_before_legacy_effects(monkeypatch, tmp_path, fake_keyring):
    config_path, _catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    before = config_path.read_bytes()
    fake_keyring.calls.clear()
    settings = Settings.model_construct(emails=[], providers=[])

    with pytest.raises(ManagedModeWriteError):
        settings.store()
    with pytest.raises(ManagedModeWriteError):
        delete_settings()

    assert config_path.read_bytes() == before
    assert fake_keyring.calls == []


@pytest.mark.parametrize(
    "operation",
    [
        lambda: keyring_store.keyring_usable(),
        lambda: keyring_store.set_secret("legacy", "incoming", "secret"),
        lambda: keyring_store.get_secret("legacy", "incoming"),
        lambda: keyring_store.delete_secret("legacy", "incoming"),
        lambda: keyring_store.delete_secret_checked("legacy", "incoming"),
        lambda: keyring_store.delete_account_credentials("legacy", ["incoming"]),
    ],
)
def test_legacy_keyring_operations_are_fenced_in_managed_mode(monkeypatch, tmp_path, fake_keyring, operation):
    _select_managed(monkeypatch, tmp_path, fake_keyring)
    fake_keyring.calls.clear()

    with pytest.raises(ManagedModeWriteError):
        operation()

    assert fake_keyring.calls == []


@pytest.mark.asyncio
async def test_mcp_legacy_account_add_is_fenced_in_managed_mode(monkeypatch, tmp_path, fake_keyring):
    config_path, _catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    before = config_path.read_bytes()
    candidate = EmailSettings(
        account_name="legacy-add",
        full_name="Legacy Add",
        email_address="legacy@example.test",
        incoming=EmailServer(
            user_name="legacy@example.test",
            password=SecretStr("must-not-write"),
            host="imap.example.test",
            port=993,
        ),
    )

    with pytest.raises(ManagedModeWriteError, match=r"account.*CLI"):
        await add_email_account(candidate)

    assert config_path.read_bytes() == before


def test_cli_reset_and_migrate_are_fenced_with_guidance(monkeypatch, tmp_path, fake_keyring):
    config_path, _catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    before = config_path.read_bytes()
    fake_keyring.calls.clear()
    runner = CliRunner()

    reset_result = runner.invoke(app, ["reset"])
    migrate_result = runner.invoke(app, ["migrate-credentials", "--to", "plaintext"])

    assert reset_result.exit_code == 1
    assert migrate_result.exit_code == 1
    assert "config select legacy" in reset_result.output
    assert "config select legacy" in migrate_result.output
    assert config_path.read_bytes() == before
    assert fake_keyring.calls == []


def test_managed_default_path_skips_legacy_migration(monkeypatch, tmp_path):
    current = tmp_path / "new" / "config.toml"
    old_parent = _private_directory(tmp_path / "old")
    legacy = old_parent / "config.toml"
    legacy.write_text(f'bootstrap_version = {BOOTSTRAP_VERSION}\nmode = "managed"\ndb_location = "catalog.sqlite3"\n')
    legacy.chmod(0o600)
    monkeypatch.delenv("MCP_EMAIL_SERVER_CONFIG_PATH", raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(current))
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", str(legacy))

    resolved = config_module._resolve_config_path()

    assert resolved == legacy
    assert not current.exists()
    assert not current.parent.exists()
