import os
import stat
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import SecretStr, ValidationError

from mcp_email_server import config as config_module
from mcp_email_server.bootstrap import bootstrap_path, read_bootstrap
from mcp_email_server.config import (
    EmailServer,
    EmailSettings,
    ProviderSettings,
    Settings,
    get_settings,
    normalize_address,
    sender_allowed,
    store_settings,
)
from mcp_email_server.windows_security import ensure_private_parent, harden_private_file, validate_private_file


def _prepare_private_test_file(path: Path) -> None:
    if os.name == "nt":
        ensure_private_parent(path)
    else:
        path.parent.mkdir(mode=0o700)
        path.parent.chmod(0o700)


def _harden_private_test_file(path: Path) -> None:
    if os.name == "nt":
        harden_private_file(path)
    else:
        path.chmod(0o600)


def test_settings_rejects_policy_cardinality_above_application_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        config_module,
        "APPLICATION_LIMITS",
        replace(config_module.APPLICATION_LIMITS, policy_entries=1),
    )

    settings = Settings.model_construct(
        emails=[],
        providers=[],
        allowed_recipients=["first@example.test", "second@example.test"],
        allowed_senders=[],
    )
    with pytest.raises(ValueError, match="Allowed recipients exceed"):
        settings.check_unique_account_names()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice@example.com", "alice@example.com"),
        ("Alice@Example.COM", "alice@example.com"),
        ("Alice <Alice@Example.com>", "alice@example.com"),
        ("  bob@example.com  ", "bob@example.com"),
        ("", ""),
    ],
)
def test_normalize_address(raw, expected):
    assert normalize_address(raw) == expected


def test_resolve_config_path_migrates_legacy_default(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy" / "config.toml"
    config_path = tmp_path / "current" / "config.toml"
    legacy_path.parent.mkdir()
    legacy_path.write_text('credential_storage = "plaintext"\n')
    legacy_path.chmod(0o644)

    monkeypatch.delenv("MCP_EMAIL_SERVER_CONFIG_PATH", raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", str(legacy_path))
    original_copyfileobj = config_module.shutil.copyfileobj

    def assert_private_copy(source, destination):
        if os.name == "posix":
            assert stat.S_IMODE(os.fstat(destination.fileno()).st_mode) == 0o600
        original_copyfileobj(source, destination)

    monkeypatch.setattr(config_module.shutil, "copyfileobj", assert_private_copy)

    assert config_module._resolve_config_path() == config_path
    assert config_path.read_text() == legacy_path.read_text()
    assert legacy_path.exists()
    if sys.platform == "win32":
        validate_private_file(config_path, private_parent=False)
    else:
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_resolve_config_path_does_not_overwrite_current_config(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.toml"
    config_path = tmp_path / "current.toml"
    legacy_path.write_text('source = "legacy"\n')
    config_path.write_text('source = "current"\n')

    monkeypatch.delenv("MCP_EMAIL_SERVER_CONFIG_PATH", raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", str(legacy_path))

    assert config_module._resolve_config_path() == config_path
    assert config_path.read_text() == 'source = "current"\n'


def test_resolve_config_path_preserves_concurrent_current_config(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.toml"
    config_path = tmp_path / "current" / "config.toml"
    legacy_path.write_text('source = "legacy"\n')

    monkeypatch.delenv("MCP_EMAIL_SERVER_CONFIG_PATH", raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", str(legacy_path))
    original_link = config_module.os.link

    def create_current_then_link(source, destination):
        config_path.write_text('source = "current"\n')
        original_link(source, destination)

    def create_current_then_windows_write(_path, _content, *, private_parent, replace_existing):
        assert private_parent is False
        assert replace_existing is False
        config_path.parent.mkdir(parents=True)
        config_path.write_text('source = "current"\n')
        raise FileExistsError

    if os.name == "nt":
        monkeypatch.setattr(config_module, "atomic_write_private", create_current_then_windows_write)
    else:
        monkeypatch.setattr(config_module.os, "link", create_current_then_link)

    assert config_module._resolve_config_path() == config_path
    assert config_path.read_text() == 'source = "current"\n'
    assert list(config_path.parent.iterdir()) == [config_path]


def test_resolve_config_path_honors_explicit_override(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.toml"
    config_path = tmp_path / "current.toml"
    override_path = tmp_path / "override.toml"
    legacy_path.write_text('source = "legacy"\n')

    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(override_path))
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", str(legacy_path))

    assert config_module._resolve_config_path() == override_path
    assert not config_path.exists()


def test_resolve_config_path_falls_back_when_copy_fails(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.toml"
    config_path = tmp_path / "current" / "config.toml"
    legacy_path.write_text('source = "legacy"\n')

    monkeypatch.delenv("MCP_EMAIL_SERVER_CONFIG_PATH", raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", str(legacy_path))

    def fail_copy(_source, _destination):
        config_path.write_text('source = "current"\n')
        raise PermissionError

    def fail_windows_copy(_path, _content, *, private_parent, replace_existing):
        assert private_parent is False
        assert replace_existing is False
        config_path.parent.mkdir(parents=True)
        config_path.write_text('source = "current"\n')
        raise PermissionError

    if os.name == "nt":
        monkeypatch.setattr(config_module, "atomic_write_private", fail_windows_copy)
    else:
        monkeypatch.setattr(config_module.shutil, "copyfileobj", fail_copy)

    assert config_module._resolve_config_path() == config_path
    assert config_path.read_text() == 'source = "current"\n'
    assert list(config_path.parent.iterdir()) == [config_path]


def test_sensitive_fields_excluded_from_repr():
    """Verify password and api_key are not in repr or str output."""
    server = EmailServer(
        user_name="user",
        password="secret_pass",
        host="imap.example.com",
        port=993,
        use_ssl=True,
    )
    assert "secret_pass" not in repr(server)
    assert "secret_pass" not in str(server)

    provider = ProviderSettings(
        account_name="p",
        provider_name="test",
        api_key="secret_key",
    )
    assert "secret_key" not in repr(provider)
    assert "secret_key" not in str(provider)


def test_password_is_secret_type():
    """Password field must be SecretStr — explicit access required."""
    server = EmailServer(
        user_name="user",
        password="s3cret",
        host="imap.example.com",
        port=993,
    )
    assert isinstance(server.password, SecretStr)
    assert server.password.get_secret_value() == "s3cret"


def test_email_settings_init_validates_password_type():
    """EmailSettings.init must preserve Pydantic validation of raw passwords."""
    with pytest.raises(ValidationError, match="password"):
        EmailSettings.init(
            account_name="test",
            full_name="Test User",
            email_address="test@example.com",
            user_name="test@example.com",
            password=123,  # type: ignore[arg-type]
            imap_host="imap.example.com",
        )


def test_api_key_is_secret_type():
    """API key field must be SecretStr."""
    provider = ProviderSettings(
        account_name="test",
        provider_name="test",
        api_key="sk-123",
    )
    assert isinstance(provider.api_key, SecretStr)
    assert provider.api_key.get_secret_value() == "sk-123"


def test_email_settings_without_outgoing_is_read_only():
    """EmailSettings can represent read-only IMAP accounts."""
    settings = EmailSettings(
        account_name="read_only",
        full_name="Read Only",
        email_address="read-only@example.com",
        incoming=EmailServer(
            user_name="reader",
            password="secret",
            host="imap.example.com",
            port=993,
        ),
    )

    assert settings.outgoing is None
    assert settings.can_send is False

    masked = settings.masked()
    assert masked.outgoing is None
    assert masked.incoming.password.get_secret_value() == "********"


def test_config():
    settings = get_settings()
    assert settings.emails == []
    settings.emails.append(
        EmailSettings(
            account_name="email_test",
            full_name="Test User",
            email_address="1oBbE@example.com",
            incoming=EmailServer(
                user_name="test",
                password="test",
                host="imap.gmail.com",
                port=993,
                ssl=True,
            ),
            outgoing=EmailServer(
                user_name="test",
                password="test",
                host="smtp.gmail.com",
                port=587,
                ssl=True,
            ),
        )
    )
    settings.providers.append(ProviderSettings(account_name="provider_test", provider_name="test", api_key="test"))
    store_settings(settings)
    reloaded_settings = get_settings(reload=True)
    assert reloaded_settings == settings

    with pytest.raises(ValidationError):
        settings.add_email(
            EmailSettings(
                account_name="email_test",
                full_name="Test User",
                email_address="1oBbE@example.com",
                incoming=EmailServer(
                    user_name="test",
                    password="test",
                    host="imap.gmail.com",
                    port=993,
                    ssl=True,
                ),
                outgoing=EmailServer(
                    user_name="test",
                    password="test",
                    host="smtp.gmail.com",
                    port=587,
                    ssl=True,
                ),
            )
        )


def test_add_provider_appends_to_providers():
    # A bare, uncached Settings() instance — not get_settings() — so this test
    # can't be affected by, or leak state into, other tests via the module cache.
    settings = Settings()
    assert settings.providers == []
    settings.add_provider(ProviderSettings(account_name="new_provider", provider_name="openai", api_key="sk-test"))
    assert len(settings.providers) == 1
    assert settings.providers[0].account_name == "new_provider"


def test_duplicate_provider_account_name_rejected():
    settings = Settings()
    settings.add_provider(ProviderSettings(account_name="dup_provider", provider_name="a", api_key="k1"))
    with pytest.raises(ValidationError, match="Duplicate account name"):
        settings.add_provider(ProviderSettings(account_name="dup_provider", provider_name="b", api_key="k2"))


def test_delete_email_and_delete_provider():
    settings = Settings()
    settings.add_email(
        EmailSettings(
            account_name="to_delete_email",
            full_name="Test",
            email_address="del@example.com",
            incoming=EmailServer(user_name="u", password="p", host="imap.example.com", port=993),
        )
    )
    settings.add_provider(ProviderSettings(account_name="to_delete_provider", provider_name="p", api_key="k"))

    settings.delete_email("to_delete_email")
    settings.delete_provider("to_delete_provider")

    assert "to_delete_email" not in [e.account_name for e in settings.emails]
    assert "to_delete_provider" not in [p.account_name for p in settings.providers]


def test_get_account_and_get_accounts():
    settings = Settings()
    settings.add_email(
        EmailSettings(
            account_name="lookup_email",
            full_name="Test",
            email_address="lookup@example.com",
            incoming=EmailServer(user_name="u", password="secret_pw", host="imap.example.com", port=993),
        )
    )
    settings.add_provider(ProviderSettings(account_name="lookup_provider", provider_name="p", api_key="secret_key"))

    email = settings.get_account("lookup_email")
    assert email.incoming.password.get_secret_value() == "secret_pw"

    masked_email = settings.get_account("lookup_email", masked=True)
    assert masked_email.incoming.password.get_secret_value() == "********"

    provider = settings.get_account("lookup_provider")
    assert provider.api_key.get_secret_value() == "secret_key"

    assert settings.get_account("does_not_exist") is None

    all_accounts = settings.get_accounts()
    assert any(a.account_name == "lookup_email" for a in all_accounts)
    assert any(a.account_name == "lookup_provider" for a in all_accounts)

    masked_accounts = settings.get_accounts(masked=True)
    provider_masked = next(a for a in masked_accounts if a.account_name == "lookup_provider")
    assert provider_masked.api_key.get_secret_value() == "********"


def test_store_accepts_string_toml_file(tmp_path, monkeypatch):
    settings = Settings()
    config_path = tmp_path / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", str(config_path))

    settings.store()

    assert config_path.exists()


def test_store_rejects_invalid_toml_file_setting(monkeypatch):
    settings = Settings()
    monkeypatch.setitem(Settings.model_config, "toml_file", None)

    with pytest.raises(TypeError, match="toml_file must identify exactly one file"):
        settings.store()


def test_legacy_reset_preserves_recorded_managed_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    config_path = parent / "config.toml"
    _prepare_private_test_file(config_path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    config_path.write_text(
        'bootstrap_version = 1\nbootstrap_revision = 3\nmode = "legacy"\n'
        'managed_db_location = "/private/managed.sqlite3"\n'
        'credential_storage = "plaintext"\n'
        '[[emails]]\naccount_name = "alice"\nfull_name = "Alice"\n'
        'email_address = "alice@example.test"\n'
        '[emails.incoming]\nuser_name = "alice@example.test"\npassword = "secret"\n'
        'host = "imap.example.test"\nport = 993\nuse_ssl = true\n'
    )
    _harden_private_test_file(config_path)

    config_module.delete_settings()

    assert not config_path.exists()
    assert tomllib.loads(bootstrap_path(config_path).read_text()) == {
        "bootstrap_version": 1,
        "bootstrap_revision": 3,
        "mode": "legacy",
        "managed_selection": True,
        "managed_db_location": "/private/managed.sqlite3",
    }


def test_combined_bootstrap_fields_migrate_to_sidecar_on_legacy_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    config_path = parent / "config.toml"
    _prepare_private_test_file(config_path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    config_path.write_text(
        'bootstrap_version = 1\nbootstrap_revision = 3\nmode = "legacy"\n'
        'managed_db_location = "/private/managed.sqlite3"\n'
        'db_location = "/private/legacy-index.sqlite3"\n'
        'credential_storage = "plaintext"\n'
    )
    _harden_private_test_file(config_path)
    config_module._settings = None

    settings = config_module.get_settings(reload=True)
    assert settings.bootstrap_revision == 3
    assert settings.managed_selection is None
    assert settings.managed_db_location == "/private/managed.sqlite3"
    assert settings.db_location == "/private/legacy-index.sqlite3"

    store_settings(settings)
    reloaded = config_module.get_settings(reload=True)
    assert reloaded.bootstrap_revision is None
    assert reloaded.managed_selection is None
    assert reloaded.managed_db_location is None
    assert reloaded.db_location == "/private/legacy-index.sqlite3"
    durable = read_bootstrap(config_path)
    assert durable.revision == 3
    assert durable.db_path == Path("/private/managed.sqlite3")


def test_settings_store_preserves_explicit_revisioned_no_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    config_path = parent / "config.toml"
    legacy_index = parent / "legacy-index.sqlite3"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    config_path.write_text(
        f'bootstrap_version = 1\nbootstrap_revision = 3\nmode = "legacy"\n'
        f'managed_selection = false\ndb_location = "{legacy_index.as_posix()}"\n'
        'credential_storage = "plaintext"\n'
    )
    config_path.chmod(0o600)
    config_module._settings = None

    settings = config_module.get_settings(reload=True)
    settings.store()

    durable = read_bootstrap(config_path)
    assert durable.db_path is None
    assert durable.revision == 3
    assert tomllib.loads(bootstrap_path(config_path).read_text())["managed_selection"] is False
    stored = tomllib.loads(config_path.read_text())
    assert "managed_selection" not in stored
    assert stored["db_location"] == legacy_index.as_posix()


def test_store_settings_defaults_to_cached_instance(tmp_path, monkeypatch):
    """store_settings() with no argument must fetch and store the cached Settings."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        config_module.get_settings().add_email(
            EmailSettings(
                account_name="no_arg_store",
                full_name="Test",
                email_address="a@example.com",
                incoming=EmailServer(user_name="u", password="p", host="imap.example.com", port=993),
            )
        )
        store_settings()  # no argument
        assert "no_arg_store" in cfg.read_text()
    finally:
        config_module._settings = None


def test_env_account_replaces_second_of_multiple_toml_accounts(tmp_path, monkeypatch):
    """The env-account-injection loop must find a match beyond the first TOML entry."""
    import tomli_w

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    raw = {
        "emails": [
            {
                "account_name": "first",
                "full_name": "First",
                "email_address": "first@example.com",
                "incoming": {
                    "user_name": "first",
                    "password": "first_pw",
                    "host": "imap.first.com",
                    "port": 993,
                    "use_ssl": True,
                    "start_ssl": False,
                    "verify_ssl": True,
                },
            },
            {
                "account_name": "second",
                "full_name": "Second",
                "email_address": "second@example.com",
                "incoming": {
                    "user_name": "second",
                    "password": "second_pw",
                    "host": "imap.second.com",
                    "port": 993,
                    "use_ssl": True,
                    "start_ssl": False,
                    "verify_ssl": True,
                },
            },
        ]
    }
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(tomli_w.dumps(raw).encode())
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)

    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "second")
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "env@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "env_pw")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.env.com")

    config_module._settings = None
    try:
        settings = config_module.get_settings(reload=True)
        assert len(settings.emails) == 2
        second = next(e for e in settings.emails if e.account_name == "second")
        assert second.incoming.password.get_secret_value() == "env_pw"
        first = next(e for e in settings.emails if e.account_name == "first")
        assert first.incoming.password.get_secret_value() == "first_pw"
    finally:
        config_module._settings = None


@pytest.mark.parametrize(
    ("sender", "patterns", "expected"),
    [
        ("alice@example.com", [], True),  # empty allowlist => all allowed
        ("alice@example.com", ["alice@example.com"], True),  # exact
        ("Alice <Alice@Example.com>", ["alice@example.com"], True),  # display name + case-insensitive
        ("bob@example.com", ["*@example.com"], True),  # domain glob
        ("bob@other.com", ["*@example.com"], False),  # domain glob, no match
        ("mallory@evil.com", ["alice@example.com"], False),  # no match
        ("alice@example.com", ["*@other.com", "alice@example.com"], True),  # matches second pattern
        ("", ["*@example.com"], False),  # empty sender => blocked
        ("not an email", ["*@example.com"], False),  # unparseable => blocked
        # Multi-address From headers fail closed regardless of address order.
        ("blocked@evil.com, alice@example.com", ["alice@example.com"], False),
        ("alice@example.com, blocked@evil.com", ["alice@example.com"], False),
        ("Alice <alice@example.com>, Mallory <blocked@evil.com>", ["alice@example.com"], False),
        ("alice@example.com", ["*@Example.com"], True),  # mixed-case pattern still matches
    ],
)
def test_sender_allowed(sender, patterns, expected):
    assert sender_allowed(sender, patterns) is expected


def test_allowed_recipients_defaults_to_empty(tmp_path, monkeypatch):
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    blank = tmp_path / "config.toml"
    blank.write_text("")
    monkeypatch.setitem(Settings.model_config, "toml_file", blank)
    config_module._settings = None
    try:
        assert config_module.get_settings(reload=True).allowed_recipients == []
    finally:
        config_module._settings = None


def test_allowed_recipients_toml_normalised(tmp_path, monkeypatch):
    import tomli_w

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    toml_data = {"allowed_recipients": ["Alice <Alice@Example.com>", "BOB@example.com", "alice@example.com"]}
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(tomli_w.dumps(toml_data).encode())
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        assert config_module.get_settings(reload=True).allowed_recipients == ["alice@example.com", "bob@example.com"]
    finally:
        config_module._settings = None


def test_allowed_senders_defaults_to_empty(tmp_path, monkeypatch):
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    blank = tmp_path / "config.toml"
    blank.write_text("")
    monkeypatch.setitem(Settings.model_config, "toml_file", blank)
    config_module._settings = None
    try:
        assert config_module.get_settings(reload=True).allowed_senders == []
    finally:
        config_module._settings = None


def test_allowed_senders_toml_normalised_preserves_globs(tmp_path, monkeypatch):
    import tomli_w

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    toml_data = {"allowed_senders": ["*@Example.COM", "BOB@example.com", "*@example.com"]}
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(tomli_w.dumps(toml_data).encode())
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        # lowercased + de-duplicated, but glob characters preserved (NOT run through parseaddr)
        assert config_module.get_settings(reload=True).allowed_senders == ["*@example.com", "bob@example.com"]
    finally:
        config_module._settings = None


def test_report_blocked_mutations_defaults_to_false(tmp_path, monkeypatch):
    import tomli_w

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    cfg.write_bytes(tomli_w.dumps({}).encode())
    monkeypatch.delenv("MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS", raising=False)
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        assert config_module.get_settings(reload=True).report_blocked_mutations is False
    finally:
        config_module._settings = None


def test_report_blocked_mutations_from_toml(tmp_path, monkeypatch):
    import tomli_w

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    cfg.write_bytes(tomli_w.dumps({"report_blocked_mutations": True}).encode())
    monkeypatch.delenv("MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS", raising=False)
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        assert config_module.get_settings(reload=True).report_blocked_mutations is True
    finally:
        config_module._settings = None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
@pytest.mark.parametrize("storage_mode", ["plaintext", "keyring"])
def test_store_writes_owner_only_permissions(tmp_path, monkeypatch, request, storage_mode):
    """The stored config must not be world/group readable, whether it holds cleartext or sentinels."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import EmailSettings, Settings

    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", storage_mode)
    if storage_mode == "keyring":
        # The keyring branch needs a real backend, and an empty Settings has no
        # secrets to push to it — add an account so a sentinel is actually written.
        request.getfixturevalue("fake_keyring")

    cfg = tmp_path / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        settings = config_module.get_settings(reload=True)
        if storage_mode == "keyring":
            settings.add_email(
                EmailSettings.init(
                    account_name="acct1",
                    full_name="Test",
                    email_address="a@example.com",
                    user_name="a",
                    password="hunter2",
                    imap_host="imap.example.com",
                )
            )
        settings.store()
        mode = stat.S_IMODE(cfg.stat().st_mode)
        assert mode == 0o600
        if storage_mode == "keyring":
            assert "__KEYRING__" in cfg.read_text()
    finally:
        config_module._settings = None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_store_tightens_preexisting_permissions(tmp_path, monkeypatch):
    """A pre-existing world-readable file must be tightened, not left as-is."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    cfg.chmod(0o644)
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        settings = config_module.get_settings(reload=True)
        settings.store()
        mode = stat.S_IMODE(cfg.stat().st_mode)
        assert mode == 0o600
    finally:
        config_module._settings = None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_store_never_exposes_new_content_in_world_readable_file(tmp_path, monkeypatch):
    """Regression for the permission-window blocker: the *new* credentials must
    never exist in a world-readable file, not even transiently. Verifies the
    write ORDER (temp file is 0600 before the atomic swap; the destination still
    holds the OLD content at swap time) rather than only the final mode.
    """
    import os

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    cfg.write_text("report_blocked_mutations = false\n")  # pre-existing content...
    cfg.chmod(0o644)  # ...in a world-readable file

    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None

    observations = {}
    real_replace = os.replace

    def spy_replace(src, dst):
        # At swap time: the temp source must already be owner-only, and the
        # destination must still be the old (world-readable) file untouched.
        observations["src_mode"] = stat.S_IMODE(os.stat(src).st_mode)
        observations["dst_mode"] = stat.S_IMODE(os.stat(dst).st_mode)
        observations["dst_content"] = Path(dst).read_text()
        observations["src_content"] = Path(src).read_text()
        return real_replace(src, dst)

    monkeypatch.setattr(config_module.os, "replace", spy_replace)
    try:
        settings = config_module.get_settings(reload=True)
        settings.report_blocked_mutations = True  # make the new content differ from the old
        settings.store()
    finally:
        config_module._settings = None

    # The temp file the new content was written to was 0600 from the start...
    assert observations["src_mode"] == 0o600
    assert "report_blocked_mutations = true" in observations["src_content"]
    # ...while the destination still held the OLD 0644 content up to the swap.
    assert observations["dst_mode"] == 0o644
    assert observations["dst_content"] == "report_blocked_mutations = false\n"
    # Final state: atomically replaced, owner-only.
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_store_failed_write_leaves_original_intact(tmp_path, monkeypatch):
    """If the write fails mid-way, the atomic-replace approach must leave the
    previous config (and its permissions) untouched, with no temp file left behind.
    """

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    cfg.write_text("report_blocked_mutations = false\n")
    cfg.chmod(0o600)
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_module.os, "replace", boom)
    try:
        settings = config_module.get_settings(reload=True)
        with pytest.raises(OSError, match="simulated replace failure"):
            settings.store()
    finally:
        config_module._settings = None

    # Original file untouched; no stray temp files left in the directory.
    assert cfg.read_text() == "report_blocked_mutations = false\n"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    lock_path = Path(f"{bootstrap_path(cfg)}.lock")
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    leftover = [p.name for p in tmp_path.iterdir() if p.name not in {"config.toml", lock_path.name}]
    assert leftover == [], f"temp files left behind: {leftover}"


def test_store_non_posix_falls_back_to_plain_write(tmp_path, monkeypatch):
    """On non-POSIX platforms store() writes via write_text (no fd-level permissions)."""
    import mcp_email_server.bootstrap as bootstrap_module
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    monkeypatch.setattr(bootstrap_module, "_SECURE_BOOTSTRAP_FILES_SUPPORTED", False)
    config_module._settings = None
    try:
        settings = config_module.get_settings(reload=True)
        monkeypatch.setattr(config_module, "os", SimpleNamespace(name="nt"))
        settings.store()
        assert cfg.read_text() == settings._to_toml()
    finally:
        config_module._settings = None


def test_legacy_store_and_reset_remain_available_without_secure_bootstrap_primitives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_email_server.bootstrap as bootstrap_module

    config_path = tmp_path / "legacy" / "config.toml"
    monkeypatch.setattr(bootstrap_module, "_SECURE_BOOTSTRAP_FILES_SUPPORTED", False)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)

    settings = Settings(report_blocked_mutations=True)
    settings.store()

    assert config_path.exists()
    assert "report_blocked_mutations" in tomllib.loads(config_path.read_text())
    if os.name == "posix":
        assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
    assert not Path(f"{config_path}.lock").exists()

    config_module.delete_settings()

    assert not config_path.exists()
    assert not Path(f"{config_path}.lock").exists()


@pytest.mark.parametrize("target", ["keyring", "plaintext"])
def test_legacy_credential_migration_remains_available_without_secure_bootstrap_primitives(
    target: Literal["keyring", "plaintext"],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_email_server.bootstrap as bootstrap_module

    config_path = tmp_path / "legacy" / "config.toml"
    monkeypatch.setattr(bootstrap_module, "_SECURE_BOOTSTRAP_FILES_SUPPORTED", False)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)

    Settings.migrate_credentials(target)

    assert tomllib.loads(config_path.read_text())["credential_storage"] == target
    assert not Path(f"{config_path}.lock").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_legacy_store_does_not_chmod_existing_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    config_path = parent / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)

    Settings().store()

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_missing_reset_leaves_parent_ready_for_secure_managed_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_email_server.bootstrap import write_bootstrap
    from mcp_email_server.managed import ManagedCatalog

    parent = tmp_path / "fresh"
    config_path = parent / "config.toml"
    catalog_path = parent / "managed.sqlite3"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)

    config_module.delete_settings()

    assert not config_path.exists()
    assert not parent.exists()
    assert not Path(f"{config_path}.lock").exists()

    catalog = ManagedCatalog.initialize(catalog_path)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    bootstrap = write_bootstrap(
        mode="legacy",
        db_path=catalog_path,
        path=config_path,
        expected_revision=0,
        expected_exists=False,
    )

    assert catalog.catalog_revision() == 1
    assert bootstrap.db_path == catalog_path


def test_report_blocked_mutations_env_overrides_toml(tmp_path, monkeypatch):
    import tomli_w

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    cfg.write_bytes(tomli_w.dumps({"report_blocked_mutations": False}).encode())
    monkeypatch.setenv("MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS", "true")
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        assert config_module.get_settings(reload=True).report_blocked_mutations is True
    finally:
        config_module._settings = None
