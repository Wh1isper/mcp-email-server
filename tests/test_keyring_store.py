"""Tests for OS keyring credential storage (plan: keyring-credential-storage).

Every test picks its credential_storage mode explicitly via monkeypatch.setenv —
the conftest.py import-time default is "plaintext" so nothing here runs vacuously
under that default (see conftest.py's guardrail comment).
"""

from __future__ import annotations

import pytest
import tomli_w
from keyring.backend import KeyringBackend
from loguru import logger as loguru_logger
from pydantic import SecretStr
from typer.testing import CliRunner

import mcp_email_server.config as config_module
from mcp_email_server import keyring_store
from mcp_email_server.cli import app as cli_app
from mcp_email_server.config import EmailServer, EmailSettings, ProviderSettings, Settings, delete_settings
from mcp_email_server.keyring_store import SENTINEL, SERVICE


def _bind(tmp_path, monkeypatch, *, also_config_path: bool = False):
    """Point Settings' toml_file (and optionally CONFIG_PATH) at a fresh temp file."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
    if also_config_path:
        monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)
    config_module._settings = None
    return cfg


def _raw_email_toml(account_name: str, password: str, *, host: str = "imap.example.com") -> dict:
    return {
        "emails": [
            {
                "account_name": account_name,
                "full_name": "Test",
                "email_address": f"{account_name}@example.com",
                "incoming": {
                    "user_name": account_name,
                    "password": password,
                    "host": host,
                    "port": 993,
                    "use_ssl": True,
                    "start_ssl": False,
                    "verify_ssl": True,
                },
            }
        ]
    }


# 1. Round-trip [mode=keyring, fake] — covers both EmailSettings (incoming+outgoing)
# and ProviderSettings (api_key) keyring paths.
def test_round_trip_keyring_mode(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "keyring")
    cfg = _bind(tmp_path, monkeypatch)

    settings = Settings()
    settings.add_email(
        EmailSettings.init(
            account_name="acct1",
            full_name="Test",
            email_address="a@example.com",
            user_name="a",
            password="hunter2",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            smtp_password="smtp-secret",
        )
    )
    settings.add_provider(ProviderSettings(account_name="prov1", provider_name="openai", api_key="sk-secret"))
    settings.store()

    content = cfg.read_text()
    assert SENTINEL in content
    assert "hunter2" not in content
    assert "smtp-secret" not in content
    assert "sk-secret" not in content

    config_module._settings = None
    reloaded = Settings()
    assert reloaded == settings
    assert isinstance(reloaded.emails[0].incoming.password, SecretStr)
    assert isinstance(reloaded.emails[0].outgoing.password, SecretStr)
    assert isinstance(reloaded.providers[0].api_key, SecretStr)
    assert reloaded.emails[0].incoming.password.get_secret_value() == "hunter2"
    assert reloaded.emails[0].outgoing.password.get_secret_value() == "smtp-secret"
    assert reloaded.providers[0].api_key.get_secret_value() == "sk-secret"


# Partial keyring failure in auto mode: the probe succeeds (so use_keyring starts
# True), but the actual account's set_password call fails — distinct from
# test_auto_mode_falls_back_to_plaintext_on_broken_backend, where the probe itself
# fails and use_keyring is False from the start.
class _ProbeOnlyKeyring(KeyringBackend):
    """Succeeds for the probe's own key, fails for every real account key."""

    priority = 1

    def __init__(self):
        super().__init__()
        self._probe_store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        if username.startswith("__probe__"):
            self._probe_store[(service, username)] = password
            return
        from keyring.errors import PasswordSetError

        raise PasswordSetError("simulated failure storing a real credential")

    def get_password(self, service, username):
        return self._probe_store.get((service, username))

    def delete_password(self, service, username):
        key = (service, username)
        if key in self._probe_store:
            del self._probe_store[key]


def test_auto_mode_falls_back_to_plaintext_on_partial_keyring_failure(tmp_path, monkeypatch):
    import keyring

    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "auto")
    cfg = _bind(tmp_path, monkeypatch)

    previous = keyring.get_keyring()
    keyring.set_keyring(_ProbeOnlyKeyring())
    keyring_store.keyring_usable.cache_clear()
    try:
        settings = Settings()
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
        # A provider account too, so the provider-side set_secret failure branch
        # (distinct from the email-side one) is also exercised.
        settings.add_provider(ProviderSettings(account_name="prov1", provider_name="openai", api_key="sk-secret"))

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda msg: messages.append(str(msg)), level="WARNING")
        try:
            settings.store()  # must not raise despite the probe reporting "usable"
        finally:
            loguru_logger.remove(sink_id)
    finally:
        keyring.set_keyring(previous)
        keyring_store.keyring_usable.cache_clear()

    content = cfg.read_text()
    assert "hunter2" in content
    assert "sk-secret" in content
    assert SENTINEL not in content
    assert any("falling back to plaintext" in m for m in messages)


# auto mode with a genuinely usable backend must actually use it (exercises the
# keyring_usable() probe's success path, not just its failure path).
def test_auto_mode_uses_keyring_when_backend_usable(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "auto")
    cfg = _bind(tmp_path, monkeypatch)

    settings = Settings()
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

    content = cfg.read_text()
    assert SENTINEL in content
    assert "hunter2" not in content
    assert fake_keyring._store[(SERVICE, "acct1:incoming")] == "hunter2"
    # The probe's own set/get/delete round-trip must have happened.
    assert any(call[0] == "delete" and call[2].startswith("__probe__") for call in fake_keyring.calls)


class _WrongValueProbeKeyring(KeyringBackend):
    """set/get/delete all succeed without raising, but get returns the wrong
    value for the probe key — usability must be decided by set/get, not just
    the absence of an exception.
    """

    priority = 1

    def set_password(self, service, username, password):
        pass

    def get_password(self, service, username):
        return "not-ok"

    def delete_password(self, service, username):
        pass


def test_keyring_usable_false_when_probe_value_mismatches_without_raising(monkeypatch):
    import keyring

    previous = keyring.get_keyring()
    keyring.set_keyring(_WrongValueProbeKeyring())
    keyring_store.keyring_usable.cache_clear()
    try:
        assert keyring_store.keyring_usable() is False
    finally:
        keyring.set_keyring(previous)
        keyring_store.keyring_usable.cache_clear()


class _DeleteFailsProbeKeyring(KeyringBackend):
    """set/get succeed normally, but delete (the probe's own cleanup) raises —
    a working backend must not be misclassified as unusable because of this.
    """

    priority = 1

    def __init__(self):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        raise PasswordDeleteError("simulated cleanup failure")


def test_keyring_usable_true_despite_probe_cleanup_failure(monkeypatch):
    import keyring

    previous = keyring.get_keyring()
    keyring.set_keyring(_DeleteFailsProbeKeyring())
    keyring_store.keyring_usable.cache_clear()
    try:
        assert keyring_store.keyring_usable() is True
    finally:
        keyring.set_keyring(previous)
        keyring_store.keyring_usable.cache_clear()


# 2. Auto fallback [mode=auto, broken]
def test_auto_mode_falls_back_to_plaintext_on_broken_backend(tmp_path, monkeypatch, broken_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "auto")
    cfg = _bind(tmp_path, monkeypatch)

    settings = Settings()
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

    messages: list[str] = []
    sink_id = loguru_logger.add(lambda msg: messages.append(str(msg)), level="WARNING")
    try:
        settings.store()  # must not raise
    finally:
        loguru_logger.remove(sink_id)

    content = cfg.read_text()
    assert "hunter2" in content
    assert SENTINEL not in content
    assert any("plaintext" in m and "keyring" in m for m in messages)


# 3. Explicit keyring, no backend [mode=keyring, broken]
def test_explicit_keyring_mode_raises_without_usable_backend(tmp_path, monkeypatch, broken_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "keyring")
    cfg = _bind(tmp_path, monkeypatch)

    settings = Settings()
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
    with pytest.raises(ValueError, match="keyring"):
        settings.store()
    assert not cfg.exists()


# 4. Missing entry [mode=keyring, fake with the entry deleted]
def test_missing_keyring_entry_raises_on_load(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "keyring")
    _bind(tmp_path, monkeypatch)

    settings = Settings()
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

    fake_keyring._store.clear()  # simulate the entry vanishing from the keyring

    config_module._settings = None
    with pytest.raises(ValueError, match="acct1"):
        Settings()


# 4b. get_secret() raising (not just returning None) must also raise on load.
def test_broken_backend_raises_on_load_when_sentinel_present(tmp_path, monkeypatch, broken_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "keyring")
    cfg = _bind(tmp_path, monkeypatch)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("acct1", SENTINEL)))

    config_module._settings = None
    with pytest.raises(ValueError, match="acct1"):
        Settings()


# 5. Mixed file [mode=auto, fake holding the sentinel account's secret]
def test_mixed_sentinel_and_cleartext_accounts_load(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "auto")
    cfg = _bind(tmp_path, monkeypatch)

    fake_keyring.set_password(SERVICE, "acct1:incoming", "secret1")
    raw = {
        "emails": [
            _raw_email_toml("acct1", SENTINEL, host="imap1.example.com")["emails"][0],
            _raw_email_toml("acct2", "cleartext2", host="imap2.example.com")["emails"][0],
        ]
    }
    cfg.write_text(tomli_w.dumps(raw))

    config_module._settings = None
    settings = Settings()
    by_name = {e.account_name: e for e in settings.emails}
    assert by_name["acct1"].incoming.password.get_secret_value() == "secret1"
    assert by_name["acct2"].incoming.password.get_secret_value() == "cleartext2"


# 6. Migration both directions [fake; env override unset]. Uses an account with
# both incoming+outgoing plus a provider, so the --to plaintext cleanup loop
# exercises both the outgoing-role branch and the provider branch.
def test_migration_round_trip_both_directions(tmp_path, monkeypatch, fake_keyring):
    cfg = _bind(tmp_path, monkeypatch, also_config_path=True)

    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "plaintext")
    settings = Settings()
    settings.add_email(
        EmailSettings.init(
            account_name="acct1",
            full_name="Test",
            email_address="a@example.com",
            user_name="a",
            password="hunter2",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            smtp_password="smtp-secret",
        )
    )
    settings.add_provider(ProviderSettings(account_name="prov1", provider_name="openai", api_key="sk-secret"))
    settings.store()
    assert "hunter2" in cfg.read_text()
    assert "sk-secret" in cfg.read_text()

    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["migrate-credentials", "--to", "keyring"])
    assert result.exit_code == 0, result.output

    content = cfg.read_text()
    assert SENTINEL in content
    assert "hunter2" not in content
    assert "smtp-secret" not in content
    assert "sk-secret" not in content
    assert 'credential_storage = "keyring"' in content
    assert fake_keyring._store[(SERVICE, "acct1:incoming")] == "hunter2"
    assert fake_keyring._store[(SERVICE, "acct1:outgoing")] == "smtp-secret"
    assert fake_keyring._store[(SERVICE, "prov1:api_key")] == "sk-secret"

    result2 = runner.invoke(cli_app, ["migrate-credentials", "--to", "plaintext"])
    assert result2.exit_code == 0, result2.output

    content2 = cfg.read_text()
    assert "hunter2" in content2
    assert "smtp-secret" in content2
    assert "sk-secret" in content2
    assert SENTINEL not in content2
    assert 'credential_storage = "plaintext"' in content2
    assert (SERVICE, "acct1:incoming") not in fake_keyring._store
    assert (SERVICE, "acct1:outgoing") not in fake_keyring._store
    assert (SERVICE, "prov1:api_key") not in fake_keyring._store


def test_migrate_credentials_warns_on_env_override_conflict(tmp_path, monkeypatch, fake_keyring):
    cfg = _bind(tmp_path, monkeypatch, also_config_path=True)

    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "plaintext")
    settings = Settings()
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
    assert "hunter2" in cfg.read_text()

    # Deliberately conflicting: env says "plaintext", --to says "keyring".
    runner = CliRunner()
    result = runner.invoke(cli_app, ["migrate-credentials", "--to", "keyring"])
    assert result.exit_code == 0, result.output
    assert "MCP_EMAIL_SERVER_CREDENTIAL_STORAGE" in result.output
    assert "differs" in result.output


def test_migrate_credentials_to_plaintext_skips_outgoing_cleanup_for_incoming_only_account(
    tmp_path, monkeypatch, fake_keyring
):
    """The --to plaintext cleanup loop's outgoing-role branch must be skipped
    (not attempted) for an account that never had outgoing configured.
    """
    cfg = _bind(tmp_path, monkeypatch, also_config_path=True)
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)

    settings = Settings()
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
    settings.add_provider(ProviderSettings(account_name="prov1", provider_name="openai", api_key="sk-secret"))
    settings.credential_storage = "keyring"
    settings._credential_storage_override = "keyring"
    settings.store()
    assert cfg.read_text().count(SENTINEL) == 2  # incoming password + api_key, no outgoing

    runner = CliRunner()
    result = runner.invoke(cli_app, ["migrate-credentials", "--to", "plaintext"])
    assert result.exit_code == 0, result.output

    content = cfg.read_text()
    assert "hunter2" in content
    assert "sk-secret" in content
    assert (SERVICE, "acct1:incoming") not in fake_keyring._store
    assert (SERVICE, "prov1:api_key") not in fake_keyring._store
    assert (SERVICE, "acct1:outgoing") not in fake_keyring._store  # never existed; delete is a no-op


def test_migrate_credentials_load_failure_exits_cleanly(tmp_path, monkeypatch, broken_keyring):
    cfg = _bind(tmp_path, monkeypatch, also_config_path=True)
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("acct1", SENTINEL)))

    runner = CliRunner()
    result = runner.invoke(cli_app, ["migrate-credentials", "--to", "plaintext"])
    assert result.exit_code == 1
    assert "could not load" in result.output
    assert not isinstance(result.exception, ValueError)  # typer.Exit, not a raw traceback


def test_migrate_credentials_store_failure_exits_cleanly(tmp_path, monkeypatch, broken_keyring):
    cfg = _bind(tmp_path, monkeypatch, also_config_path=True)
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("acct1", "cleartext")))

    runner = CliRunner()
    result = runner.invoke(cli_app, ["migrate-credentials", "--to", "keyring"])
    assert result.exit_code == 1
    assert "failed" in result.output


# 6b. Migration ignores env-account shadowing (flag (c), §7)
def test_migration_ignores_env_account_shadow(tmp_path, monkeypatch, fake_keyring):
    _bind(tmp_path, monkeypatch)

    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "plaintext")
    settings = Settings()
    settings.add_email(
        EmailSettings.init(
            account_name="default",
            full_name="Stored",
            email_address="stored@example.com",
            user_name="stored",
            password="stored-secret",
            imap_host="imap.stored.example.com",
        )
    )
    settings.store()
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)

    # These would normally inject/override an EmailSettings for account "default".
    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "default")
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "env@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "env-secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.env.example.com")

    migrated = Settings.load_for_migration()
    assert migrated.emails[0].incoming.password.get_secret_value() == "stored-secret"
    assert migrated.emails[0].incoming.host == "imap.stored.example.com"


# 7. Deletion cleanup. ui.py is excluded from this project's coverage/unit tests
# (pyproject.toml omits it), so the gating contract ("skip keyring entirely when
# effective mode is plaintext") is exercised here via the equally-gated
# delete_settings() reset path, plus direct tests of the shared keyring_store
# helper both flows call.
def test_delete_account_credentials_removes_entries(fake_keyring):
    fake_keyring.set_password(SERVICE, "acct1:incoming", "secret1")
    fake_keyring.set_password(SERVICE, "acct1:outgoing", "secret2")
    keyring_store.delete_account_credentials("acct1", ["incoming", "outgoing"])
    assert (SERVICE, "acct1:incoming") not in fake_keyring._store
    assert (SERVICE, "acct1:outgoing") not in fake_keyring._store


def test_delete_account_credentials_swallows_broken_backend(broken_keyring):
    keyring_store.delete_account_credentials("acct1", ["incoming"])  # must not raise


def test_reset_performs_zero_keyring_calls_in_plaintext_mode(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "plaintext")
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("acct1", "cleartext")))

    delete_settings()
    assert not cfg.exists()
    assert fake_keyring.calls == []


def test_reset_mode_gate_falls_back_to_raw_toml_key_when_env_unset(tmp_path, monkeypatch, fake_keyring):
    """With no env override, the reset gate must read credential_storage from the
    TOML file itself — the real-world default path (no test here previously
    exercised it, since every other reset test sets the env var explicitly).
    """
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)

    raw = _raw_email_toml("acct1", "cleartext")
    raw["credential_storage"] = "plaintext"
    cfg.write_text(tomli_w.dumps(raw))

    delete_settings()
    assert not cfg.exists()
    assert fake_keyring.calls == []


def test_reset_mode_gate_attempts_cleanup_when_toml_mode_is_not_plaintext(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)

    fake_keyring.set_password(SERVICE, "acct1:incoming", "secret1")
    raw = _raw_email_toml("acct1", SENTINEL)
    raw["credential_storage"] = "keyring"
    cfg.write_text(tomli_w.dumps(raw))

    delete_settings()
    assert not cfg.exists()
    assert (SERVICE, "acct1:incoming") not in fake_keyring._store
    assert any(call[0] == "delete" for call in fake_keyring.calls)


def test_reset_cleans_up_outgoing_role_provider_and_skips_malformed_entries(tmp_path, monkeypatch, fake_keyring):
    """Covers the reset cleanup's outgoing-role branch, provider loop, and the
    defensive skip for an email entry with no account_name.
    """
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)

    fake_keyring.set_password(SERVICE, "acct1:incoming", "secret1")
    fake_keyring.set_password(SERVICE, "acct1:outgoing", "secret2")
    fake_keyring.set_password(SERVICE, "prov1:api_key", "secret3")

    raw = _raw_email_toml("acct1", SENTINEL)
    raw["emails"][0]["outgoing"] = dict(raw["emails"][0]["incoming"], password=SENTINEL)
    raw["emails"].append({"full_name": "No account name", "email_address": "x@example.com"})  # malformed
    raw["providers"] = [
        {"account_name": "prov1", "provider_name": "openai", "api_key": SENTINEL},
        {"provider_name": "no-name-provider", "api_key": SENTINEL},  # malformed: no account_name
    ]
    raw["credential_storage"] = "keyring"
    cfg.write_text(tomli_w.dumps(raw))

    delete_settings()  # must not raise despite the malformed email/provider entries
    assert not cfg.exists()
    assert (SERVICE, "acct1:incoming") not in fake_keyring._store
    assert (SERVICE, "acct1:outgoing") not in fake_keyring._store
    assert (SERVICE, "prov1:api_key") not in fake_keyring._store


def test_reset_unparseable_toml_still_unlinks(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)
    cfg.write_text("this is not [ valid toml =::")

    delete_settings()  # must not raise; warns and unlinks anyway
    assert not cfg.exists()


# 8. Sentinel value rejected everywhere
def test_sentinel_value_rejected_at_creation_entry_points():
    with pytest.raises(ValueError, match="reserved"):
        EmailSettings.init(
            account_name="x",
            full_name="x",
            email_address="x@example.com",
            user_name="x",
            password=SENTINEL,
            imap_host="imap.example.com",
        )

    settings = Settings()
    email = EmailSettings(
        account_name="x",
        full_name="x",
        email_address="x@example.com",
        incoming=EmailServer(user_name="x", password=SENTINEL, host="imap.example.com", port=993),
    )
    with pytest.raises(ValueError, match="reserved"):
        settings.add_email(email)

    provider = ProviderSettings(account_name="p", provider_name="p", api_key=SENTINEL)
    with pytest.raises(ValueError, match="reserved"):
        settings.add_provider(provider)


def test_sentinel_value_rejected_at_store_pre_write_check():
    settings = Settings()
    email = EmailSettings(
        account_name="x",
        full_name="x",
        email_address="x@example.com",
        incoming=EmailServer(user_name="x", password=SENTINEL, host="imap.example.com", port=993),
    )
    settings.emails.append(email)  # bypasses add_email's guard, same as an existing test pattern
    with pytest.raises(ValueError, match="reserved"):
        settings.store()


# 10. Env preemption [mode=auto, broken, sentinel TOML + matching env account]
def test_env_account_preempts_sentinel_resolution(tmp_path, monkeypatch, broken_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "auto")
    cfg = _bind(tmp_path, monkeypatch)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("default", SENTINEL, host="imap.stored.example.com")))

    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "default")
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "env@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "env-secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.env.example.com")

    config_module._settings = None
    settings = Settings()  # would raise if it touched the (broken) keyring at all
    assert settings.emails[0].incoming.password.get_secret_value() == "env-secret"


# 11. Broken-keyring reset [mode=auto, broken, sentinel TOML]
def test_reset_unlinks_file_despite_broken_keyring(tmp_path, monkeypatch, broken_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "auto")
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("acct1", SENTINEL)))
    assert cfg.exists()

    delete_settings()  # must not raise despite broken backend + sentinel content
    assert not cfg.exists()


# 12. Plaintext + sentinel file [mode=plaintext, fake installed]
def test_plaintext_mode_with_sentinel_file_is_hard_error(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "plaintext")
    cfg = _bind(tmp_path, monkeypatch)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("acct1", SENTINEL)))

    with pytest.raises(ValueError, match="migrate-credentials"):
        Settings()
    assert fake_keyring.calls == []


# 13. Plaintext + sentinel + matching env account [mode=plaintext, fake installed]
def test_plaintext_mode_env_account_preempts_sentinel_error(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "plaintext")
    cfg = _bind(tmp_path, monkeypatch)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("default", SENTINEL, host="imap.stored.example.com")))

    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "default")
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "env@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "env-secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.env.example.com")

    settings = Settings()  # must not raise
    assert settings.emails[0].incoming.password.get_secret_value() == "env-secret"
    assert fake_keyring.calls == []


# 14. Invalid MCP_EMAIL_SERVER_CREDENTIAL_STORAGE value
def test_invalid_credential_storage_env_value_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "bogus")
    _bind(tmp_path, monkeypatch)

    with pytest.raises(ValueError) as exc_info:
        Settings()
    msg = str(exc_info.value)
    assert "auto" in msg
    assert "keyring" in msg
    assert "plaintext" in msg


def test_invalid_credential_storage_env_value_reset_still_unlinks(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "bogus")
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg)
    cfg.write_text(tomli_w.dumps({"emails": []}))
    assert cfg.exists()

    delete_settings()  # must not raise; warns and proceeds as non-plaintext
    assert not cfg.exists()
