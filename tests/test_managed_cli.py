from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from mcp_email_server.bootstrap import read_bootstrap
from mcp_email_server.cli import app
from mcp_email_server.managed import ManagedCatalog


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def _base_account_args() -> list[str]:
    return [
        "account",
        "add",
        "alice",
        "--email",
        "alice@example.test",
        "--full-name",
        "Alice",
        "--imap-host",
        "imap.example.test",
        "--password-stdin",
    ]


def test_cli_managed_setup_activation_and_selection(monkeypatch, tmp_path, fake_keyring):
    parent = _private_directory(tmp_path / "app")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()

    initialized = runner.invoke(app, ["config", "init", "--database", str(database)])
    added = runner.invoke(app, _base_account_args(), input="incoming-secret\n")
    listed = runner.invoke(app, ["account", "list"])
    staging_select = runner.invoke(app, ["config", "select", "managed"])
    activated = runner.invoke(app, ["config", "activate"])
    selected = runner.invoke(app, ["config", "select", "managed"])

    assert initialized.exit_code == 0, initialized.output
    assert read_bootstrap(config_path).mode == "managed"
    # Selection is observed only after the final command; init itself was legacy.
    assert added.exit_code == 0, added.output
    assert "incoming-secret" not in added.output
    assert listed.exit_code == 0
    assert "alice" in listed.output
    assert "incoming=ACTIVE" in listed.output
    assert "incoming-secret" not in listed.output
    assert staging_select.exit_code == 1
    assert "must be ACTIVE" in staging_select.output
    assert activated.exit_code == 0, activated.output
    assert selected.exit_code == 0, selected.output
    assert "Restart" in selected.output
    assert read_bootstrap(config_path).mode == "managed"
    assert ManagedCatalog(database).lifecycle() == "ACTIVE"
    replacement = parent / "replacement.sqlite3"
    rejected_init = runner.invoke(app, ["config", "init", "--database", str(replacement)])
    assert rejected_init.exit_code == 1
    assert "Select legacy" in rejected_init.output
    assert not replacement.exists()
    assert read_bootstrap(config_path).db_path == database


def test_config_init_does_not_select_managed(monkeypatch, tmp_path):
    parent = _private_directory(tmp_path / "app")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))

    result = CliRunner().invoke(app, ["config", "init", "--database", str(database)])

    assert result.exit_code == 0
    assert read_bootstrap(config_path).mode == "legacy"
    assert ManagedCatalog(database).lifecycle() == "STAGING"


def test_select_managed_rejects_missing_active_secret(monkeypatch, tmp_path, fake_keyring):
    parent = _private_directory(tmp_path / "app")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0
    assert runner.invoke(app, _base_account_args(), input="incoming-secret\n").exit_code == 0
    assert runner.invoke(app, ["config", "activate"]).exit_code == 0
    fake_keyring._store.clear()

    selected = runner.invoke(app, ["config", "select", "managed"])

    assert selected.exit_code == 1
    assert "missing" in selected.output.lower()
    assert "incoming-secret" not in selected.output
    assert read_bootstrap(config_path).mode == "legacy"


def test_account_add_has_no_secret_argv_option():
    runner = CliRunner()

    help_result = runner.invoke(app, ["account", "add", "--help"])
    rejected = runner.invoke(app, [*_base_account_args()[:-1], "--password", "secret"])

    assert help_result.exit_code == 0
    assert "--password-stdin" in help_result.output
    assert "--password " not in help_result.output
    assert rejected.exit_code != 0
    assert "No such option" in rejected.output


def test_cli_account_add_with_smtp_reads_two_stdin_lines(monkeypatch, tmp_path, fake_keyring):
    parent = _private_directory(tmp_path / "app")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            *_base_account_args(),
            "--smtp-host",
            "smtp.example.test",
            "--smtp-user",
            "alice@example.test",
        ],
        input="incoming-secret\noutgoing-secret\n",
    )

    assert result.exit_code == 0, result.output
    summary = ManagedCatalog(database).list_accounts()[0]
    assert summary.incoming_binding == "ACTIVE"
    assert summary.outgoing_binding == "ACTIVE"
    assert "incoming-secret" not in result.output
    assert "outgoing-secret" not in result.output


def test_cli_account_lifecycle_uses_revisions_and_explicit_removal_confirmation(
    monkeypatch,
    tmp_path,
    fake_keyring,
):
    parent = _private_directory(tmp_path / "app-lifecycle")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0
    assert runner.invoke(app, _base_account_args(), input="incoming-secret\n").exit_code == 0

    updated = runner.invoke(
        app,
        [
            "account",
            "update",
            "alice",
            "--expected-revision",
            "2",
            "--name",
            "alice-renamed",
            "--full-name",
            "Alice Updated",
            "--imap-host",
            "imap-new.example.test",
        ],
    )
    stale = runner.invoke(
        app,
        ["account", "disable", "alice-renamed", "--expected-revision", "2"],
    )
    disabled = runner.invoke(
        app,
        ["account", "disable", "alice-renamed", "--expected-revision", "3"],
    )
    detached = runner.invoke(
        app,
        ["account", "remove-secret", "alice-renamed", "incoming", "--expected-revision", "4"],
    )
    reset_secret = runner.invoke(
        app,
        ["account", "set-secret", "alice-renamed", "incoming", "--password-stdin"],
        input="replacement-secret\n",
    )
    enabled = runner.invoke(
        app,
        ["account", "enable", "alice-renamed", "--expected-revision", "6"],
    )
    disabled_again = runner.invoke(
        app,
        ["account", "disable", "alice-renamed", "--expected-revision", "7"],
    )
    wrong_confirmation = runner.invoke(
        app,
        [
            "account",
            "remove",
            "alice-renamed",
            "--expected-revision",
            "8",
            "--confirm",
            "alice",
        ],
    )
    removed = runner.invoke(
        app,
        [
            "account",
            "remove",
            "alice-renamed",
            "--expected-revision",
            "8",
            "--confirm",
            "alice-renamed",
        ],
    )

    assert updated.exit_code == 0, updated.output
    assert stale.exit_code == 1
    assert "revision changed" in stale.output
    assert disabled.exit_code == 0, disabled.output
    assert detached.exit_code == 0, detached.output
    assert reset_secret.exit_code == 0, reset_secret.output
    assert "replacement-secret" not in reset_secret.output
    assert enabled.exit_code == 0, enabled.output
    assert disabled_again.exit_code == 0, disabled_again.output
    assert wrong_confirmation.exit_code == 1
    assert "exactly match" in wrong_confirmation.output
    assert removed.exit_code == 0, removed.output
    assert ManagedCatalog(database).list_accounts() == []


def test_cli_update_can_add_complete_outgoing_endpoint_to_disabled_account(monkeypatch, tmp_path, fake_keyring):
    parent = _private_directory(tmp_path / "app-outgoing")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0
    assert runner.invoke(app, _base_account_args(), input="incoming-secret\n").exit_code == 0
    assert (
        runner.invoke(
            app,
            ["account", "disable", "alice", "--expected-revision", "2"],
        ).exit_code
        == 0
    )

    updated = runner.invoke(
        app,
        [
            "account",
            "update",
            "alice",
            "--expected-revision",
            "3",
            "--smtp-host",
            "smtp.example.test",
            "--smtp-port",
            "465",
            "--smtp-user",
            "alice@example.test",
            "--smtp-ssl",
            "--no-smtp-starttls",
            "--smtp-verify-ssl",
        ],
    )

    assert updated.exit_code == 0, updated.output
    details = ManagedCatalog(database).show_account("alice")
    assert details.outgoing is not None
    assert details.outgoing.host == "smtp.example.test"
    assert details.outgoing_binding is None


def test_cli_legacy_import_preview_apply_and_repeat_are_deterministic(
    monkeypatch,
    tmp_path,
    fake_keyring,
):
    parent = _private_directory(tmp_path / "app-import")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    config_path.write_text(
        """credential_storage = "plaintext"
enable_attachment_download = true
allowed_recipients = ["BOB@EXAMPLE.TEST"]
allowed_senders = ["*@example.test"]
report_blocked_mutations = true

[[emails]]
account_name = "stored"
full_name = "Stored User"
email_address = "stored@example.test"
save_to_sent = false
sent_folder_name = "Sent Items"

[emails.incoming]
user_name = "stored@example.test"
password = "stored-secret"
host = "imap.stored.example.test"
port = 993
use_ssl = true
start_ssl = false
verify_ssl = true

[[providers]]
account_name = "unsupported-provider"
provider_name = "example"
api_key = "provider-secret"
"""
    )
    config_path.chmod(0o600)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "environment-only")
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "environment@example.test")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "environment-secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.environment.example.test")
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0
    source_after_init = config_path.read_bytes()
    keyring_before_preview = dict(fake_keyring._store)

    preview = runner.invoke(app, ["config", "import-legacy"])

    assert preview.exit_code == 0, preview.output
    assert "mode=preview" in preview.output
    assert "account=stored action=create" in preview.output
    assert "provider=unsupported-provider action=unsupported" in preview.output
    assert "environment-only" not in preview.output
    assert "stored-secret" not in preview.output
    assert "provider-secret" not in preview.output
    assert "environment-secret" not in preview.output
    assert fake_keyring._store == keyring_before_preview
    assert config_path.read_bytes() == source_after_init

    missing_confirmation = runner.invoke(app, ["config", "import-legacy", "--apply"])
    applied = runner.invoke(
        app,
        ["config", "import-legacy", "--apply", "--confirm", "IMPORT"],
    )
    keyring_after_apply = dict(fake_keyring._store)
    repeated = runner.invoke(
        app,
        ["config", "import-legacy", "--apply", "--confirm", "IMPORT"],
    )

    assert missing_confirmation.exit_code == 1
    assert "exactly IMPORT" in missing_confirmation.output
    assert applied.exit_code == 0, applied.output
    assert "created=stored" in applied.output
    assert repeated.exit_code == 0, repeated.output
    assert "account=stored action=unchanged" in repeated.output
    assert "created=none" in repeated.output
    assert fake_keyring._store == keyring_after_apply
    assert config_path.read_bytes() == source_after_init
    catalog = ManagedCatalog(database)
    account = catalog.load_account("stored")
    assert account.incoming.password.get_secret_value() == "stored-secret"
    assert [item.account_name for item in catalog.load_settings(require_active=False).emails] == ["stored"]
    policy = catalog.policy()
    assert policy.enable_attachment_download is True
    assert policy.allowed_recipients == ("bob@example.test",)
    assert policy.allowed_senders == ("*@example.test",)
    assert policy.report_blocked_mutations is True


def test_cli_legacy_import_preview_does_not_resolve_missing_keyring_secret(
    monkeypatch,
    tmp_path,
    fake_keyring,
):
    parent = _private_directory(tmp_path / "app-import-keyring")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    config_path.write_text(
        """credential_storage = "keyring"

[[emails]]
account_name = "stored"
full_name = "Stored User"
email_address = "stored@example.test"

[emails.incoming]
user_name = "stored@example.test"
password = "__KEYRING__"
host = "imap.stored.example.test"
port = 993
use_ssl = true
start_ssl = false
verify_ssl = true
"""
    )
    config_path.chmod(0o600)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0

    preview = runner.invoke(app, ["config", "import-legacy"])
    applied = runner.invoke(
        app,
        ["config", "import-legacy", "--apply", "--confirm", "IMPORT"],
    )

    assert preview.exit_code == 0, preview.output
    assert "action=create" in preview.output
    assert applied.exit_code == 1
    assert "credential is unavailable" in applied.output
    assert ManagedCatalog(database).list_accounts() == []
