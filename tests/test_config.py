import pytest
from pydantic import SecretStr, ValidationError

from mcp_email_server.config import (
    EmailServer,
    EmailSettings,
    ProviderSettings,
    get_settings,
    normalize_address,
    sender_allowed,
    store_settings,
)


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
