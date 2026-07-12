import stat
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

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
    leftover = [p.name for p in tmp_path.iterdir() if p.name != "config.toml"]
    assert leftover == [], f"temp files left behind: {leftover}"


def test_store_non_posix_falls_back_to_plain_write(tmp_path, monkeypatch):
    """On non-POSIX platforms store() writes via write_text (no fd-level permissions)."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    cfg = tmp_path / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    config_module._settings = None
    try:
        settings = config_module.get_settings(reload=True)
        monkeypatch.setattr(config_module.os, "name", "nt")
        settings.store()
        assert cfg.read_text() == settings._to_toml()
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
