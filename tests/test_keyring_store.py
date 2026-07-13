"""Tests for OS keyring credential storage (plan: keyring-credential-storage).

Tests explicitly set or unset credential_storage as needed. The conftest.py
import-time default is "plaintext" so keyring paths never run vacuously under an
ambient backend (see conftest.py's guardrail comment).
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
def test_legacy_plaintext_config_loads_unchanged_then_migrates_on_save(tmp_path, monkeypatch, fake_keyring):
    """A config predating credential_storage remains untouched on load, then an
    auto-mode save migrates its secret when the keyring is usable.
    """
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    cfg = _bind(tmp_path, monkeypatch)
    cfg.write_text(tomli_w.dumps(_raw_email_toml("legacy", "legacy-secret")))
    before = cfg.read_bytes()

    settings = Settings()
    assert settings.credential_storage == "auto"
    assert settings.emails[0].incoming.password.get_secret_value() == "legacy-secret"
    assert cfg.read_bytes() == before
    assert fake_keyring.calls == []

    settings.store()
    content = cfg.read_text()
    assert 'credential_storage = "auto"' in content
    assert SENTINEL in content
    assert "legacy-secret" not in content

    config_module._settings = None
    reloaded = Settings()
    assert reloaded.emails[0].incoming.password.get_secret_value() == "legacy-secret"


def test_env_storage_override_is_persisted_with_credential_representation(tmp_path, monkeypatch, fake_keyring):
    """A keyring override must not leave plaintext mode beside keyring sentinels."""
    cfg = _bind(tmp_path, monkeypatch)
    raw = _raw_email_toml("acct1", "cleartext")
    raw["credential_storage"] = "plaintext"
    cfg.write_text(tomli_w.dumps(raw))
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "keyring")

    settings = Settings()
    settings.store()
    content = cfg.read_text()
    assert 'credential_storage = "keyring"' in content
    assert SENTINEL in content
    assert "cleartext" not in content

    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    config_module._settings = None
    reloaded = Settings()
    assert reloaded.credential_storage == "keyring"
    assert reloaded.emails[0].incoming.password.get_secret_value() == "cleartext"


def test_plaintext_env_override_persists_mode_with_cleartext_representation(tmp_path, monkeypatch, fake_keyring):
    cfg = _bind(tmp_path, monkeypatch)
    raw = _raw_email_toml("acct1", "cleartext")
    raw["credential_storage"] = "auto"
    cfg.write_text(tomli_w.dumps(raw))
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "plaintext")

    settings = Settings()
    settings.store()
    content = cfg.read_text()
    assert 'credential_storage = "plaintext"' in content
    assert "cleartext" in content
    assert SENTINEL not in content
    assert fake_keyring.calls == []


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


def test_migrate_credentials_to_plaintext_warns_on_undeletable_keyring_entry(tmp_path, monkeypatch, fake_keyring):
    """If a keyring entry can't be removed during --to plaintext, the leftover live
    secret must be reported, not hidden under the success message."""
    cfg = _bind(tmp_path, monkeypatch, also_config_path=True)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "keyring")

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
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)

    # Make keyring deletion a no-op so the entry survives (get still returns it).
    monkeypatch.setattr(fake_keyring, "delete_password", lambda service, username: None)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["migrate-credentials", "--to", "plaintext"])
    assert result.exit_code == 0, result.output
    assert "hunter2" in cfg.read_text()  # plaintext copy written
    assert "acct1:incoming" in result.output  # leftover secret reported
    assert fake_keyring._store[(SERVICE, "acct1:incoming")] == "hunter2"  # still in keyring


def test_migrate_credentials_to_plaintext_warns_when_removal_cannot_be_verified(tmp_path, monkeypatch, fake_keyring):
    from keyring.errors import KeyringError, PasswordDeleteError

    cfg = _bind(tmp_path, monkeypatch, also_config_path=True)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "keyring")
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
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)

    verification_started = False
    original_get = fake_keyring.get_password

    def fail_delete(service, username):
        nonlocal verification_started
        verification_started = True
        raise PasswordDeleteError("backend disconnected during delete")

    def fail_verification(service, username):
        if verification_started:
            raise KeyringError("backend unavailable during verification")
        return original_get(service, username)

    monkeypatch.setattr(fake_keyring, "delete_password", fail_delete)
    monkeypatch.setattr(fake_keyring, "get_password", fail_verification)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["migrate-credentials", "--to", "plaintext"])
    assert result.exit_code == 0, result.output
    assert "hunter2" in cfg.read_text()
    assert "could not be verified" in result.output
    assert "acct1:incoming" in result.output


def test_migrate_credentials_to_plaintext_no_backend_does_not_report_false_orphans(
    tmp_path, monkeypatch, broken_keyring
):
    """With no usable keyring backend, --to plaintext on an already-plaintext config
    is an idempotent no-op and must NOT falsely warn about orphaned secrets."""
    cfg = _bind(tmp_path, monkeypatch, also_config_path=True)
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    # Cleartext config (no sentinels) so load succeeds without touching the keyring.
    cfg.write_text(tomli_w.dumps(_raw_email_toml("acct1", "cleartext")))

    runner = CliRunner()
    result = runner.invoke(cli_app, ["migrate-credentials", "--to", "plaintext"])
    assert result.exit_code == 0, result.output
    assert "orphan" not in result.output.lower()
    assert "could not be confirmed removed" not in result.output
    assert "cleartext" in cfg.read_text()


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


# --- Recreate-on-owner-conflict: a keychain item created by a different install
# (uvx vs `uv tool install`) is foreign-owned; macOS blocks modifying it
# (errSecInvalidOwnerEdit, -25244). set_secret must delete + recreate it. ---
def _raise_owner_edit_error() -> None:
    from keyring.errors import PasswordSetError

    cause = OSError(-25244, "Unknown Error")
    raise PasswordSetError("Can't store password on keychain: (-25244, 'Unknown Error')") from cause


class _ForeignOwnedThenOkKeyring(KeyringBackend):
    """Modifying a pre-existing 'foreign-owned' key raises PasswordSetError; once
    the key is deleted, a fresh set succeeds. Records set/delete calls."""

    priority = 1

    def __init__(self, foreign_keys):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}
        self._foreign: set[tuple[str, str]] = set(foreign_keys)
        self.set_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def set_password(self, service, username, password):
        self.set_calls.append((service, username))
        key = (service, username)
        if key in self._foreign:
            _raise_owner_edit_error()
        self._store[key] = password

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username):
        self.delete_calls.append((service, username))
        key = (service, username)
        self._foreign.discard(key)  # deleting clears the foreign-owned lock
        self._store.pop(key, None)


def _install_backend(backend):
    import keyring

    previous = (keyring.get_keyring(), keyring_store.sys.platform)
    keyring.set_keyring(backend)
    keyring_store.sys.platform = "darwin"
    keyring_store.keyring_usable.cache_clear()
    return previous


def _restore_backend(previous):
    import keyring

    previous_backend, previous_platform = previous
    keyring.set_keyring(previous_backend)
    keyring_store.sys.platform = previous_platform
    keyring_store.keyring_usable.cache_clear()


def test_set_secret_skips_destructive_write_when_value_is_unchanged():
    backend = _ForeignOwnedThenOkKeyring(set())
    backend._store[(SERVICE, "acct1:incoming")] = "same-secret"
    previous = _install_backend(backend)
    try:
        keyring_store.set_secret("acct1", "incoming", "same-secret")
    finally:
        _restore_backend(previous)
    assert backend.set_calls == []
    assert backend.delete_calls == []


def test_set_secret_recreates_foreign_owned_entry():
    backend = _ForeignOwnedThenOkKeyring({(SERVICE, "acct1:incoming")})
    previous = _install_backend(backend)
    try:
        keyring_store.set_secret("acct1", "incoming", "hunter2")
    finally:
        _restore_backend(previous)
    assert backend.get_password(SERVICE, "acct1:incoming") == "hunter2"
    assert (SERVICE, "acct1:incoming") in backend.delete_calls
    assert len(backend.set_calls) == 2  # failed modify + successful recreate


def test_set_secret_recovers_even_when_delete_raises():
    from keyring.errors import PasswordDeleteError

    class _DeleteRaisesThenOk(_ForeignOwnedThenOkKeyring):
        def delete_password(self, service, username):
            self.delete_calls.append((service, username))
            self._foreign.discard((service, username))
            raise PasswordDeleteError("nothing to delete")

    backend = _DeleteRaisesThenOk({(SERVICE, "acct1:incoming")})
    previous = _install_backend(backend)
    try:
        keyring_store.set_secret("acct1", "incoming", "hunter2")
    finally:
        _restore_backend(previous)
    assert backend.get_password(SERVICE, "acct1:incoming") == "hunter2"


def test_set_secret_propagates_when_recreate_still_fails():
    from keyring.errors import PasswordSetError

    class _AlwaysForeign(_ForeignOwnedThenOkKeyring):
        def delete_password(self, service, username):  # never clears the foreign lock
            self.delete_calls.append((service, username))

    backend = _AlwaysForeign({(SERVICE, "acct1:incoming")})
    previous = _install_backend(backend)
    try:
        with pytest.raises(PasswordSetError):
            keyring_store.set_secret("acct1", "incoming", "hunter2")
    finally:
        _restore_backend(previous)
    assert len(backend.set_calls) == 2  # tried twice, still failed


# --- The delete-and-recreate recovery must be reserved for the -25244 owner
# conflict ONLY. An ordinary PasswordSetError (locked keychain, quota, an
# arbitrary backend failure) must never delete a still-valid credential. ---
class _ExistingThenSetError(KeyringBackend):
    """Holds an existing value; every set_password raises the given
    PasswordSetError. Records whether delete_password was ever called."""

    priority = 1

    def __init__(self, existing: dict[tuple[str, str], str], message: str):
        super().__init__()
        self._store = dict(existing)
        self._message = message
        self.delete_calls: list[tuple[str, str]] = []

    def set_password(self, service, username, password):
        from keyring.errors import PasswordSetError

        raise PasswordSetError(self._message)

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username):
        self.delete_calls.append((service, username))
        self._store.pop((service, username), None)


@pytest.mark.parametrize(
    ("platform", "status", "expected"),
    [
        ("darwin", -25244, True),
        ("darwin", -25300, False),
        ("linux", -25244, False),
    ],
)
def test_is_owner_edit_conflict_requires_darwin_and_chained_status(monkeypatch, platform, status, expected):
    from keyring.errors import PasswordSetError

    error = PasswordSetError("backend message may contain any text, including -25244")
    error.__cause__ = OSError(status, "backend status")
    monkeypatch.setattr(keyring_store.sys, "platform", platform)
    assert keyring_store._is_owner_edit_conflict(error) is expected


def test_is_owner_edit_conflict_ignores_message_without_status_cause(monkeypatch):
    from keyring.errors import PasswordSetError

    monkeypatch.setattr(keyring_store.sys, "platform", "darwin")
    error = PasswordSetError("failed for account -25244:incoming")
    assert keyring_store._is_owner_edit_conflict(error) is False


def test_set_secret_does_not_delete_on_ordinary_set_error():
    """An ordinary (non -25244) PasswordSetError must propagate without deleting
    the pre-existing credential, which stays readable."""
    from keyring.errors import PasswordSetError

    backend = _ExistingThenSetError(
        {(SERVICE, "acct1:incoming"): "old-secret"},
        message="Can't store password on keychain: (-25300, 'quota exceeded')",
    )
    previous = _install_backend(backend)
    try:
        with pytest.raises(PasswordSetError):
            keyring_store.set_secret("acct1", "incoming", "new-secret")
    finally:
        _restore_backend(previous)
    assert backend.delete_calls == []  # never deleted
    assert backend.get_password(SERVICE, "acct1:incoming") == "old-secret"  # still readable


def test_set_secret_failed_recovery_leaves_old_credential_readable():
    """When the -25244 recovery is attempted but the recreate keeps failing and
    the delete is blocked, the previous credential must remain readable."""
    from keyring.errors import PasswordDeleteError, PasswordSetError

    class _ForeignDeleteBlocked(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self._store = {(SERVICE, "acct1:incoming"): "old-secret"}
            self.delete_calls: list[tuple[str, str]] = []

        def set_password(self, service, username, password):
            # Foreign-owned: edits always blocked with the -25244 status.
            _raise_owner_edit_error()

        def get_password(self, service, username):
            return self._store.get((service, username))

        def delete_password(self, service, username):
            # Deleting a foreign-owned item is itself blocked, so the old value
            # is never removed.
            self.delete_calls.append((service, username))
            raise PasswordDeleteError("delete blocked for foreign-owned item")

    backend = _ForeignDeleteBlocked()
    previous = _install_backend(backend)
    try:
        with pytest.raises(PasswordSetError):
            keyring_store.set_secret("acct1", "incoming", "new-secret")
    finally:
        _restore_backend(previous)
    assert backend.delete_calls == [(SERVICE, "acct1:incoming")]  # recovery was attempted
    assert backend.get_password(SERVICE, "acct1:incoming") == "old-secret"  # but old value survives


def test_set_secret_rollback_restores_previous_value_when_recreate_fails():
    """The rollback path proper: delete succeeds (old value gone), the recreate of
    the NEW value fails transiently, and the previous value is restored so no
    credential is lost."""
    from keyring.errors import PasswordSetError

    class _RecreateFailsRollbackSucceeds(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self._store = {(SERVICE, "acct1:incoming"): "old-secret"}
            self._foreign = {(SERVICE, "acct1:incoming")}

        def set_password(self, service, username, password):
            key = (service, username)
            if key in self._foreign:
                _raise_owner_edit_error()
            if password == "new-secret":  # noqa: S105 (test literal, not a real credential)
                raise PasswordSetError("transient store failure recreating the new value")
            self._store[key] = password  # restoring the old value succeeds

        def get_password(self, service, username):
            return self._store.get((service, username))

        def delete_password(self, service, username):
            key = (service, username)
            self._foreign.discard(key)  # delete clears the foreign lock...
            self._store.pop(key, None)  # ...and removes the stored value

    backend = _RecreateFailsRollbackSucceeds()
    previous = _install_backend(backend)
    try:
        with pytest.raises(PasswordSetError, match="transient"):
            keyring_store.set_secret("acct1", "incoming", "new-secret")
    finally:
        _restore_backend(previous)
    # The new value never landed, but the previous value was restored by rollback.
    assert backend.get_password(SERVICE, "acct1:incoming") == "old-secret"


def test_set_secret_aborts_before_write_when_backup_read_fails():
    """A denied snapshot must fail closed before any destructive backend write."""
    from keyring.errors import KeyringError

    class _BackupReadRaises(_ForeignOwnedThenOkKeyring):
        def get_password(self, service, username):
            raise KeyringError("read blocked")

    backend = _BackupReadRaises({(SERVICE, "acct1:incoming")})
    previous = _install_backend(backend)
    try:
        with pytest.raises(KeyringError, match="read blocked"):
            keyring_store.set_secret("acct1", "incoming", "hunter2")
    finally:
        _restore_backend(previous)
    assert backend.set_calls == []
    assert backend.delete_calls == []


def test_set_secret_snapshots_before_mac_backend_delete_then_add_failure():
    """Model keyring's macOS order: the first set deletes the old item before
    SecItemAdd raises. The pre-write snapshot must still make rollback possible.
    """
    from keyring.errors import PasswordSetError

    class _MacDeleteThenAddFails(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self._store = {(SERVICE, "acct1:incoming"): "old-secret"}
            self.set_calls = 0

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self.set_calls += 1
            key = (service, username)
            self._store.pop(key, None)
            if self.set_calls == 1:
                _raise_owner_edit_error()
            if password == "new-secret":  # noqa: S105  # recreate fails, rollback succeeds
                raise PasswordSetError("recreate failed")
            self._store[key] = password

        def delete_password(self, service, username):
            self._store.pop((service, username), None)

    backend = _MacDeleteThenAddFails()
    previous = _install_backend(backend)
    try:
        with pytest.raises(PasswordSetError, match="recreate failed"):
            keyring_store.set_secret("acct1", "incoming", "new-secret")
    finally:
        _restore_backend(previous)
    assert backend.get_password(SERVICE, "acct1:incoming") == "old-secret"


def test_set_secret_rolls_back_when_recreate_raises_another_keyring_error():
    from keyring.errors import KeyringLocked

    class _RetryLocks(_ForeignOwnedThenOkKeyring):
        def set_password(self, service, username, password):
            key = (service, username)
            self.set_calls.append((service, username))
            if key in self._foreign:
                _raise_owner_edit_error()
            if password == "new-secret":  # noqa: S105
                raise KeyringLocked("keychain locked during recreate")
            self._store[key] = password

    backend = _RetryLocks({(SERVICE, "acct1:incoming")})
    backend._store[(SERVICE, "acct1:incoming")] = "old-secret"
    previous = _install_backend(backend)
    try:
        with pytest.raises(KeyringLocked, match="locked during recreate"):
            keyring_store.set_secret("acct1", "incoming", "new-secret")
    finally:
        _restore_backend(previous)
    assert backend.get_password(SERVICE, "acct1:incoming") == "old-secret"


def test_set_secret_logs_when_both_recreate_and_rollback_fail():
    """The genuine data-loss corner (delete succeeds, recreate fails, restore of
    the old value also fails) must be logged loudly rather than swallowed."""
    from keyring.errors import PasswordSetError

    class _EverySetFailsAfterDelete(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self._store = {(SERVICE, "acct1:incoming"): "old-secret"}
            self._foreign = {(SERVICE, "acct1:incoming")}

        def set_password(self, service, username, password):
            if (service, username) in self._foreign:
                _raise_owner_edit_error()
            raise PasswordSetError("every write fails after the delete")  # recreate AND rollback fail

        def get_password(self, service, username):
            return self._store.get((service, username))

        def delete_password(self, service, username):
            key = (service, username)
            self._foreign.discard(key)
            self._store.pop(key, None)

    backend = _EverySetFailsAfterDelete()
    previous = _install_backend(backend)
    messages: list[str] = []
    sink_id = loguru_logger.add(lambda msg: messages.append(str(msg)), level="ERROR")
    try:
        with pytest.raises(PasswordSetError):
            keyring_store.set_secret("acct1", "incoming", "new-secret")
    finally:
        loguru_logger.remove(sink_id)
        _restore_backend(previous)
    assert any("may no longer be stored" in m for m in messages)


def test_keyring_mode_error_message_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", "keyring")
    cfg = _bind(tmp_path, monkeypatch)

    class _AlwaysForeign(_ForeignOwnedThenOkKeyring):
        def delete_password(self, service, username):
            self.delete_calls.append((service, username))

    backend = _AlwaysForeign({(SERVICE, "acct1:incoming")})
    previous = _install_backend(backend)
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
        with pytest.raises(ValueError, match="security delete-generic-password"):
            settings.store()
        # Keyring mode raises before falling back, so no plaintext secret is written.
        assert not cfg.exists() or "hunter2" not in cfg.read_text()
    finally:
        _restore_backend(previous)
