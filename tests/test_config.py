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
