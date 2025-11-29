"""Focused tests to achieve patch coverage for environment variable configuration."""

import os

from mcp_email_server.config import EmailSettings, Settings


def test_from_env_missing_email_and_password(monkeypatch):
    """Test return None when email/password missing - covers line 138."""
    # No environment variables set
    result = EmailSettings.from_env()
    assert result is None

    # Only email, no password
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
    result = EmailSettings.from_env()
    assert result is None

    # Only password, no email
    monkeypatch.delenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", raising=False)
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
    result = EmailSettings.from_env()
    assert result is None


def test_from_env_missing_hosts_warning(monkeypatch):
    """Test logger.warning for missing hosts - covers lines 154-156."""
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")

    # Missing both hosts
    result = EmailSettings.from_env()
    assert result is None

    # Missing SMTP host
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.test.com")
    result = EmailSettings.from_env()
    assert result is None

    # Missing IMAP host
    monkeypatch.delenv("MCP_EMAIL_SERVER_IMAP_HOST")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.test.com")
    result = EmailSettings.from_env()
    assert result is None


def test_from_env_exception_handling(monkeypatch):
    """Test exception handling in try/except - covers lines 177-179."""
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.test.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.test.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_PORT", "invalid")  # Will cause ValueError

    result = EmailSettings.from_env()
    assert result is None


def test_from_env_success_with_all_defaults(monkeypatch):
    """Test successful creation with defaults - covers lines 147-176."""
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "user@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.example.com")

    result = EmailSettings.from_env()
    assert result is not None
    assert result.account_name == "default"
    assert result.full_name == "user"
    assert result.email_address == "user@example.com"
    assert result.incoming.user_name == "user@example.com"
    assert result.incoming.port == 993
    assert result.outgoing.port == 465


def test_from_env_with_all_vars_set(monkeypatch):
    """Test with all environment variables set - covers parse_bool branches."""
    env_vars = {
        "MCP_EMAIL_SERVER_ACCOUNT_NAME": "myaccount",
        "MCP_EMAIL_SERVER_FULL_NAME": "John Doe",
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "john@example.com",
        "MCP_EMAIL_SERVER_USER_NAME": "johnuser",
        "MCP_EMAIL_SERVER_PASSWORD": "pass123",
        "MCP_EMAIL_SERVER_IMAP_HOST": "imap.example.com",
        "MCP_EMAIL_SERVER_IMAP_PORT": "143",
        "MCP_EMAIL_SERVER_IMAP_SSL": "false",
        "MCP_EMAIL_SERVER_SMTP_HOST": "smtp.example.com",
        "MCP_EMAIL_SERVER_SMTP_PORT": "587",
        "MCP_EMAIL_SERVER_SMTP_SSL": "no",
        "MCP_EMAIL_SERVER_SMTP_START_SSL": "yes",
        "MCP_EMAIL_SERVER_IMAP_USER_NAME": "imap_john",
        "MCP_EMAIL_SERVER_IMAP_PASSWORD": "imap_pass",
        "MCP_EMAIL_SERVER_SMTP_USER_NAME": "smtp_john",
        "MCP_EMAIL_SERVER_SMTP_PASSWORD": "smtp_pass",
    }

    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    result = EmailSettings.from_env()
    assert result is not None
    assert result.account_name == "myaccount"
    assert result.full_name == "John Doe"
    assert result.incoming.user_name == "imap_john"
    assert result.incoming.password == "imap_pass"  # noqa: S105
    assert result.incoming.port == 143
    assert result.incoming.use_ssl is False
    assert result.outgoing.user_name == "smtp_john"
    assert result.outgoing.password == "smtp_pass"  # noqa: S105
    assert result.outgoing.port == 587
    assert result.outgoing.use_ssl is False
    assert result.outgoing.start_ssl is True


def test_from_env_boolean_parsing_variations(monkeypatch):
    """Test various boolean value parsing - covers parse_bool function."""
    base_env = {
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "test@example.com",
        "MCP_EMAIL_SERVER_PASSWORD": "pass",
        "MCP_EMAIL_SERVER_IMAP_HOST": "imap.test.com",
        "MCP_EMAIL_SERVER_SMTP_HOST": "smtp.test.com",
    }

    # Test "1" = true
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SSL", "1")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_SSL", "0")
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)

    result = EmailSettings.from_env()
    assert result.incoming.use_ssl is True
    assert result.outgoing.use_ssl is False

    # Test "on"/"off"
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SSL", "on")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_START_SSL", "off")

    result = EmailSettings.from_env()
    assert result.incoming.use_ssl is True
    assert result.outgoing.start_ssl is False


def test_settings_init_no_env(monkeypatch, tmp_path):
    """Test Settings.__init__ when no env vars - covers line 211 false branch."""
    config_file = tmp_path / "empty.toml"
    config_file.write_text("")
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_file))

    # Clear any email env vars
    for key in list(os.environ.keys()):
        if key.startswith("MCP_EMAIL_SERVER_") and "CONFIG_PATH" not in key:
            monkeypatch.delenv(key, raising=False)

    settings = Settings()
    assert len(settings.emails) == 0


def test_settings_init_add_new_account(monkeypatch, tmp_path):
    """Test adding new account from env - covers lines 225-226."""
    config_file = tmp_path / "empty.toml"
    config_file.write_text("")
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_file))

    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "new@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "newpass")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.new.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.new.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "newaccount")

    settings = Settings()
    assert len(settings.emails) == 1
    assert settings.emails[0].account_name == "newaccount"


def test_settings_init_override_existing(monkeypatch, tmp_path):
    """Test overriding existing TOML account - covers lines 214-222."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[[emails]]
account_name = "existing"
full_name = "Old Name"
email_address = "old@example.com"
created_at = "2025-01-01T00:00:00"
updated_at = "2025-01-01T00:00:00"

[emails.incoming]
user_name = "olduser"
password = "oldpass"
host = "imap.old.com"
port = 993
use_ssl = true

[emails.outgoing]
user_name = "olduser"
password = "oldpass"
host = "smtp.old.com"
port = 465
use_ssl = true
""")

    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "existing")
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "new@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "newpass")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.new.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.new.com")

    settings = Settings()
    assert len(settings.emails) == 1
    assert settings.emails[0].account_name == "existing"
    assert settings.emails[0].email_address == "new@example.com"  # Overridden


def test_settings_init_loop_through_multiple_accounts(monkeypatch, tmp_path):
    """Test loop iteration with multiple accounts - covers lines 214-217."""
    config_file = tmp_path / "multi.toml"
    config_file.write_text("""
[[emails]]
account_name = "first"
full_name = "First"
email_address = "first@example.com"
created_at = "2025-01-01T00:00:00"
updated_at = "2025-01-01T00:00:00"

[emails.incoming]
user_name = "first"
password = "pass1"
host = "imap.first.com"
port = 993
use_ssl = true

[emails.outgoing]
user_name = "first"
password = "pass1"
host = "smtp.first.com"
port = 465
use_ssl = true

[[emails]]
account_name = "second"
full_name = "Second"
email_address = "second@example.com"
created_at = "2025-01-01T00:00:00"
updated_at = "2025-01-01T00:00:00"

[emails.incoming]
user_name = "second"
password = "pass2"
host = "imap.second.com"
port = 993
use_ssl = true

[emails.outgoing]
user_name = "second"
password = "pass2"
host = "smtp.second.com"
port = 465
use_ssl = true

[[emails]]
account_name = "third"
full_name = "Third"
email_address = "third@example.com"
created_at = "2025-01-01T00:00:00"
updated_at = "2025-01-01T00:00:00"

[emails.incoming]
user_name = "third"
password = "pass3"
host = "imap.third.com"
port = 993
use_ssl = true

[emails.outgoing]
user_name = "third"
password = "pass3"
host = "smtp.third.com"
port = 465
use_ssl = true
""")

    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_file))

    # Override the third account (forces loop to iterate through all)
    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "third")
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "env@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "envpass")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.env.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.env.com")

    settings = Settings()
    # Note: Our implementation replaces all TOML with env, so we only get 1 account
    assert len(settings.emails) == 1
    assert settings.emails[0].account_name == "third"
    assert settings.emails[0].email_address == "env@example.com"


def test_email_settings_masked(monkeypatch):
    """Test the masked() method - covers line 182."""
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "secret123")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.test.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.test.com")

    email = EmailSettings.from_env()
    assert email is not None

    masked = email.masked()
    assert masked.incoming.password == "********"  # noqa: S105
    assert masked.outgoing.password == "********"  # noqa: S105
    assert masked.email_address == "test@example.com"


def test_enable_attachment_download_from_env_true(monkeypatch, tmp_path):
    """Test enable_attachment_download can be set via environment variable."""
    config_file = tmp_path / "empty.toml"
    config_file.write_text("")
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_file))

    # Clear any email env vars
    for key in list(os.environ.keys()):
        if key.startswith("MCP_EMAIL_SERVER_") and "CONFIG_PATH" not in key:
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD", "true")

    settings = Settings()
    assert settings.enable_attachment_download is True


def test_enable_attachment_download_from_env_false(monkeypatch, tmp_path):
    """Test enable_attachment_download=false via environment variable."""
    config_file = tmp_path / "empty.toml"
    config_file.write_text("")
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_file))

    for key in list(os.environ.keys()):
        if key.startswith("MCP_EMAIL_SERVER_") and "CONFIG_PATH" not in key:
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD", "false")

    settings = Settings()
    assert settings.enable_attachment_download is False


def test_enable_attachment_download_env_overrides_toml(monkeypatch, tmp_path):
    """Test environment variable overrides TOML config for enable_attachment_download."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("enable_attachment_download = false\n")
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_file))

    for key in list(os.environ.keys()):
        if key.startswith("MCP_EMAIL_SERVER_") and "CONFIG_PATH" not in key:
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD", "1")

    settings = Settings()
    assert settings.enable_attachment_download is True
