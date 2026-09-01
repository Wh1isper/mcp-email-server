from __future__ import annotations

import ast
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from mcp_email_server import bootstrap as bootstrap_module
from mcp_email_server import config as config_module
from mcp_email_server import keyring_store
from mcp_email_server import managed as managed_module
from mcp_email_server.adapters.reads import LocalReadBackend
from mcp_email_server.bootstrap import BOOTSTRAP_VERSION, ManagedModeWriteError, freeze_process_bootstrap
from mcp_email_server.cli import _validate_managed_runtime, app
from mcp_email_server.config import (
    EmailServer,
    Settings,
    clear_settings_cache,
    delete_settings,
    get_settings,
)
from mcp_email_server.imap_keywords import ImapKeywordTag
from mcp_email_server.managed import ManagedCatalog
from mcp_email_server.windows_security import ensure_private_parent, harden_private_file


def test_transport_adapters_do_not_import_legacy_provider_or_catalog_bypasses() -> None:
    project_root = Path(__file__).parents[1]
    forbidden = {
        ("mcp_email_server.emails.classic", "ClassicEmailHandler"),
        ("mcp_email_server.managed", "ManagedCatalog"),
        ("mcp_email_server.emails.dispatcher", "dispatch_handler"),
    }
    discovered: set[tuple[str, str]] = set()
    cli_modules: set[str] = set()
    for relative_path in ("mcp_email_server/app.py", "mcp_email_server/cli.py"):
        tree = ast.parse((project_root / relative_path).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and isinstance(node.module, str):
                discovered.update((node.module, alias.name) for alias in node.names)
                if relative_path.endswith("cli.py"):
                    cli_modules.add(node.module)
            elif isinstance(node, ast.Import) and relative_path.endswith("cli.py"):
                cli_modules.update(alias.name for alias in node.names)

    assert discovered.isdisjoint(forbidden)
    assert cli_modules.isdisjoint({
        "mcp_email_server.bootstrap",
        "mcp_email_server.config",
        "mcp_email_server.keyring_store",
    })
    assert ("mcp_email_server", "keyring_store") not in discovered
    assert not (project_root / "mcp_email_server/emails/dispatcher.py").exists()


def _private_directory(path: Path) -> Path:
    if os.name == "nt":
        ensure_private_parent(path / "placeholder")
    else:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    return path


def _harden_private_test_file(path: Path) -> None:
    if os.name == "nt":
        harden_private_file(path)
    else:
        path.chmod(0o600)


def _select_managed(
    monkeypatch,
    tmp_path: Path,
    fake_keyring,
    *,
    complete: bool = True,
    tags: tuple[ImapKeywordTag, ...] = (),
    enable_attachment_content: bool = False,
):
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
        tags=tags,
    )
    if complete:
        catalog.set_secret("managed-alice", "incoming", "runtime-secret")
    if enable_attachment_content:
        policy = catalog.policy()
        catalog.update_policy(
            expected_revision=policy.revision,
            enable_attachment_download=policy.enable_attachment_download,
            enable_attachment_content=True,
            allowed_recipients=policy.allowed_recipients,
            allowed_senders=policy.allowed_senders,
            report_blocked_mutations=policy.report_blocked_mutations,
        )
    config_path.write_text(
        f"bootstrap_version = {BOOTSTRAP_VERSION}\n"
        'mode = "managed"\n'
        f'db_location = "{database.as_posix()}"\n'
        'credential_storage = "plaintext"\n'
        '[[emails]]\naccount_name = "legacy-tripwire"\n'
    )
    _harden_private_test_file(config_path)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    clear_settings_cache()
    return config_path, catalog


def test_managed_get_settings_uses_non_secret_catalog_not_legacy_or_environment(monkeypatch, tmp_path, fake_keyring):
    tag = ImapKeywordTag(name="important", keyword="$Important", writable=True)
    _config_path, catalog = _select_managed(
        monkeypatch,
        tmp_path,
        fake_keyring,
        tags=(tag,),
        enable_attachment_content=True,
    )
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "env@example.test")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "environment-secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "env-imap.example.test")
    monkeypatch.setenv("MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_CONTENT", "false")

    settings = get_settings(reload=True)

    assert [account.account_name for account in settings.emails] == ["managed-alice"]
    assert settings.emails[0].tags == (tag,)
    assert settings.enable_attachment_content is True
    assert settings.emails[0].incoming.password.get_secret_value() == ""
    assert (
        catalog.load_account("managed-alice", roles=("incoming",)).incoming.password.get_secret_value()
        == "runtime-secret"
    )
    assert all(account.account_name not in {"legacy-tripwire", "default"} for account in settings.emails)


def test_linux_managed_store_is_independent_of_unavailable_keyring(monkeypatch, tmp_path, broken_keyring):
    monkeypatch.setattr(managed_module.sys, "platform", "linux")
    parent = _private_directory(tmp_path / "managed-sqlite")
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

    result = catalog.set_secret("alice", "incoming", "managed-sqlite-secret")

    assert result.status == "active"
    assert result.cleanup_required == 0
    with closing(sqlite3.connect(catalog.path)) as connection:
        assert connection.execute("SELECT secret_value FROM managed_secret").fetchone()[0] == "managed-sqlite-secret"
    assert catalog.doctor().cleanup_required_bindings == 0


def test_transport_preflight_freezes_managed_authority_until_restart(monkeypatch, tmp_path, fake_keyring) -> None:
    config_path, _catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    monkeypatch.setattr(bootstrap_module, "_PROCESS_BOOTSTRAP", None)

    _validate_managed_runtime()
    content = config_path.read_text().replace('mode = "managed"', 'mode = "legacy"')
    config_path.write_text(content)
    config_path.chmod(0o600)

    assert freeze_process_bootstrap(config_path).mode == "managed"
    assert get_settings(reload=True).emails[0].account_name == "managed-alice"


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


def test_disable_is_revalidated_before_next_read_provider_open(monkeypatch, tmp_path, fake_keyring):
    _config_path, catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    monkeypatch.setattr(bootstrap_module, "_PROCESS_BOOTSTRAP", None)
    freeze_process_bootstrap(_config_path)
    get_settings(reload=True)
    backend = LocalReadBackend()
    assert backend.resolve("managed-alice").account_name == "managed-alice"

    catalog.disable_account("managed-alice", expected_revision=2)

    with pytest.raises(ValueError, match="not found"):
        backend.open("managed-alice", expected_mode="managed")


def test_managed_missing_database_does_not_fall_back_to_legacy(monkeypatch, tmp_path):
    parent = _private_directory(tmp_path / "managed")
    config_path = parent / "config.toml"
    config_path.write_text(
        f"bootstrap_version = {BOOTSTRAP_VERSION}\n"
        'mode = "managed"\n'
        f'db_location = "{(parent / "missing.sqlite3").as_posix()}"\n'
        '[[emails]]\naccount_name = "legacy-tripwire"\n'
    )
    _harden_private_test_file(config_path)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    clear_settings_cache()

    with pytest.raises(Exception, match="missing"):
        get_settings(reload=True)


def test_managed_incomplete_account_does_not_fall_back_to_legacy(monkeypatch, tmp_path, fake_keyring):
    _select_managed(monkeypatch, tmp_path, fake_keyring, complete=False)

    assert get_settings(reload=True).emails == []


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


def test_cli_reset_and_migrate_are_fenced_with_guidance(monkeypatch, tmp_path, fake_keyring):
    config_path, _catalog = _select_managed(monkeypatch, tmp_path, fake_keyring)
    before = config_path.read_bytes()
    fake_keyring.calls.clear()
    runner = CliRunner()

    reset_result = runner.invoke(app, ["reset", "--confirm", "RESET"])
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
    _harden_private_test_file(legacy)
    monkeypatch.delenv("MCP_EMAIL_SERVER_CONFIG_PATH", raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(current))
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", str(legacy))

    resolved = config_module._resolve_config_path()

    assert resolved == legacy
    assert not current.exists()
    assert not current.parent.exists()
