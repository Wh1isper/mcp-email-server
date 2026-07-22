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
