import pytest
from pydantic import SecretStr, ValidationError

from mcp_email_server.config import (
    EmailServer,
    EmailSettings,
    ProviderSettings,
    get_settings,
    store_settings,
)


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


def test_api_key_is_secret_type():
    """API key field must be SecretStr."""
    provider = ProviderSettings(
        account_name="test",
        provider_name="test",
        api_key="sk-123",
    )
    assert isinstance(provider.api_key, SecretStr)
    assert provider.api_key.get_secret_value() == "sk-123"


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


def test_allowed_recipients_defaults_to_empty(tmp_path, monkeypatch):
    """allowed_recipients is empty by default (allow-all)."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    config_file = tmp_path / "config.toml"
    config_file.write_text("")
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_recipients == []
    finally:
        config_module._settings = None


def test_allowed_recipients_toml_path_normalised(tmp_path, monkeypatch):
    """allowed_recipients loaded from TOML are lowercased and deduplicated by __init__."""
    import tomli_w

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    toml_data = {"allowed_recipients": ["Alice@Example.COM", "BOB@example.com", "alice@example.com"]}
    config_file = tmp_path / "config.toml"
    config_file.write_bytes(tomli_w.dumps(toml_data).encode())
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_recipients == ["alice@example.com", "bob@example.com"]
    finally:
        config_module._settings = None


def test_allowed_senders_defaults_to_empty(tmp_path, monkeypatch):
    """allowed_senders is empty by default (allow-all)."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    config_file = tmp_path / "config.toml"
    config_file.write_text("")
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_senders == []
    finally:
        config_module._settings = None


def test_allowed_senders_toml_normalised(tmp_path, monkeypatch):
    """Patterns from TOML are lowercased and deduplicated (globs preserved)."""
    import tomli_w

    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    toml_data = {"allowed_senders": ["*@GLEZ.DE", "*@glez.de", "Alice@Example.COM"]}
    config_file = tmp_path / "config.toml"
    config_file.write_bytes(tomli_w.dumps(toml_data).encode())
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_senders == ["*@glez.de", "alice@example.com"]
    finally:
        config_module._settings = None
