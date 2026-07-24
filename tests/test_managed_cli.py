from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click import Command, Group, Option
from typer.main import get_command
from typer.testing import CliRunner

from mcp_email_server.adapters.management import LocalManagementBackend
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.management import (
    AccountCreationResult,
    AccountDetails,
    AccountRemovalResult,
    CatalogActivationResult,
    CatalogInitializationResult,
    ConnectivityResult,
    CredentialCleanupReport,
    CredentialMutationResult,
    CredentialRemovalResult,
    DoctorReport,
    EndpointSummary,
    IndexHealth,
    LegacyAccountSnapshot,
    LegacyCredentialMigrationResult,
    LegacyImportAccountPlan,
    LegacyImportPlan,
    LegacyPolicySnapshot,
    ManagedPolicy,
    ManagementServices,
    ModeSelectionResult,
)
from mcp_email_server.bootstrap import BootstrapError, read_bootstrap, write_bootstrap
from mcp_email_server.cli import app
from mcp_email_server.config import Settings
from mcp_email_server.managed import ManagedCatalog, ManagedCatalogError


def _configure_bound_management(management: MagicMock, *, catalog_revision: int = 7) -> None:
    management.lifecycle.status.return_value = SimpleNamespace(
        selected_catalog="/private/catalog.sqlite3",
        bootstrap_revision=2,
        report=SimpleNamespace(catalog_revision=catalog_revision),
    )
    for service_name in ("lifecycle", "accounts", "credentials", "policy", "connectivity"):
        service = getattr(management, service_name)
        service.bind.return_value = service


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


def test_cli_can_select_legacy_when_selected_managed_catalog_is_missing(monkeypatch, tmp_path) -> None:
    parent = _private_directory(tmp_path / "recovery")
    config_path = parent / "config.toml"
    missing_database = parent / "missing.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    write_bootstrap(mode="managed", db_path=missing_database, path=config_path)
    runner = CliRunner()

    status = runner.invoke(app, ["config", "status"])
    selected = runner.invoke(app, ["config", "select", "legacy"])

    assert status.exit_code == 0, status.output
    assert "catalog_status=unavailable" in status.output
    assert selected.exit_code == 0, selected.output
    assert read_bootstrap(config_path).mode == "legacy"
    assert not missing_database.exists()


def test_config_init_does_not_select_managed(monkeypatch, tmp_path):
    parent = _private_directory(tmp_path / "app")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))

    result = CliRunner().invoke(app, ["config", "init", "--database", str(database)])

    assert result.exit_code == 0
    assert read_bootstrap(config_path).mode == "legacy"
    assert ManagedCatalog(database).lifecycle() == "STAGING"


def test_cli_policy_update_and_index_health_use_managed_services(monkeypatch, tmp_path) -> None:
    parent = _private_directory(tmp_path / "policy-cli")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0

    initial = runner.invoke(app, ["config", "policy"])
    updated = runner.invoke(
        app,
        [
            "config",
            "update-policy",
            "--expected-revision",
            "1",
            "--enable-attachment-download",
            "--allowed-recipients",
            "BOB@EXAMPLE.TEST, Alice <ALICE@example.test>",
            "--allowed-senders",
            "*@EXAMPLE.TEST,*@example.test",
            "--report-blocked-mutations",
        ],
    )
    health = runner.invoke(app, ["config", "index-health"])

    assert initial.exit_code == 0, initial.output
    assert "revision=1" in initial.output
    assert "allowed_recipients=none" in initial.output
    assert updated.exit_code == 0, updated.output
    assert "revision 2" in updated.output
    policy = ManagedCatalog(database).policy()
    assert policy.allowed_recipients == ("bob@example.test", "alice@example.test")
    assert policy.allowed_senders == ("*@example.test",)
    assert health.exit_code == 0, health.output
    assert "status=" in health.output
    assert "pending_operations=" in health.output


def test_cli_failed_connectivity_has_typed_exit_semantics(monkeypatch) -> None:
    management = MagicMock()
    _configure_bound_management(management)
    management.connectivity.execute = AsyncMock(
        return_value=ConnectivityResult("incoming", "failed", "Connection test failed")
    )
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )

    failed = CliRunner().invoke(app, ["account", "test", "alice", "incoming"])

    assert failed.exit_code == 1
    assert "Error: Connection test failed" in failed.output
    assert "passed" not in failed.output


def test_cli_import_apply_exits_nonzero_for_conflict_only_plan(monkeypatch) -> None:
    management = MagicMock()
    _configure_bound_management(management)
    source = LegacyAccountSnapshot(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        incoming=EndpointSummary("imap.example.test", 993, True, False, True, "alice@example.test"),
        incoming_secret_source="plaintext",
        outgoing=None,
        outgoing_secret_source=None,
        save_to_sent=True,
        sent_folder_name=None,
    )
    management.legacy_import.preview.return_value = LegacyImportPlan(
        preview_token="private-token",
        source_fingerprint="fingerprint",
        target_revision=5,
        target_policy_revision=1,
        created_at="2026-01-01T00:00:00Z",
        accounts=(LegacyImportAccountPlan("alice", "conflict", source, 2),),
        source_policy=LegacyPolicySnapshot(False, (), (), False),
        policy_action="unchanged",
        unsupported_provider_names=(),
    )
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )

    result = CliRunner().invoke(app, ["config", "import-legacy", "--apply"])

    assert result.exit_code == 1
    assert "destination conflicts: alice" in result.output
    management.legacy_import.apply.assert_not_called()


def test_select_managed_validates_binding_metadata_without_resolving_secret(monkeypatch, tmp_path):
    parent = _private_directory(tmp_path / "app")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0
    assert runner.invoke(app, _base_account_args(), input="incoming-secret\n").exit_code == 0
    assert runner.invoke(app, ["config", "activate"]).exit_code == 0
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM managed_secret")
        connection.commit()

    selected = runner.invoke(app, ["config", "select", "managed"])

    assert selected.exit_code == 0
    assert "incoming-secret" not in selected.output
    assert read_bootstrap(config_path).mode == "managed"
    with pytest.raises(ManagedCatalogError, match="missing"):
        ManagedCatalog(database).load_account("alice", roles=("incoming",), require_active_catalog=True)


def test_account_add_has_no_secret_argv_option() -> None:
    root = get_command(app)
    assert isinstance(root, Group)
    account = root.commands["account"]
    assert isinstance(account, Group)
    add = account.commands["add"]
    config = root.commands["config"]
    assert isinstance(add, Command)
    assert "repair-secret" not in account.commands
    assert isinstance(config, Group)
    assert {"policy", "update-policy", "index-health"} <= set(config.commands)
    option_names = {option for parameter in add.params if isinstance(parameter, Option) for option in parameter.opts}
    rejected = CliRunner().invoke(app, [*_base_account_args()[:-1], "--password", "secret"])

    assert "--password-stdin" in option_names
    assert "--password" not in option_names
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
    assert "account=environment-only action=create" in preview.output
    assert "secret_source=environment" in preview.output
    assert "stored-secret" not in preview.output
    assert "provider-secret" not in preview.output
    assert "environment-secret" not in preview.output
    assert fake_keyring._store == keyring_before_preview
    assert config_path.read_bytes() == source_after_init

    missing_confirmation = runner.invoke(app, ["config", "import-legacy", "--apply"], input="NO\n")
    applied = runner.invoke(
        app,
        ["config", "import-legacy", "--apply"],
        input="IMPORT\n",
    )
    keyring_after_apply = dict(fake_keyring._store)
    repeated = runner.invoke(
        app,
        ["config", "import-legacy", "--apply"],
        input="IMPORT\n",
    )

    assert missing_confirmation.exit_code == 1
    assert "exactly IMPORT" in missing_confirmation.output
    assert applied.exit_code == 0, applied.output
    assert "created=environment-only,stored" in applied.output
    assert repeated.exit_code == 0, repeated.output
    assert "account=stored action=unchanged" in repeated.output
    assert "created=none" in repeated.output
    assert fake_keyring._store == keyring_after_apply
    assert config_path.read_bytes() == source_after_init
    catalog = ManagedCatalog(database)
    account = catalog.load_account("stored")
    assert account.incoming.password.get_secret_value() == "stored-secret"
    environment_account = catalog.load_account("environment-only")
    assert environment_account.incoming.password.get_secret_value() == "environment-secret"
    assert {item.account_name for item in catalog.load_settings(require_active=False).emails} == {
        "stored",
        "environment-only",
    }
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
        ["config", "import-legacy", "--apply"],
        input="IMPORT\n",
    )

    assert preview.exit_code == 0, preview.output
    assert "action=create" in preview.output
    assert applied.exit_code == 1
    assert "credential is unavailable" in applied.output
    assert ManagedCatalog(database).list_accounts() == []


def _json_document(result: object) -> dict[str, object]:
    output = result.output
    assert isinstance(output, str)
    return json.loads(output)


def test_cli_json_mode_exposes_stable_non_secret_management_results(monkeypatch, tmp_path, fake_keyring) -> None:
    parent = _private_directory(tmp_path / "json-cli")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()

    initialized = runner.invoke(app, ["config", "init", "--database", str(database), "--json"])
    added = runner.invoke(app, [*_base_account_args(), "--json"], input="incoming-secret\n")
    status = runner.invoke(app, ["config", "status", "--json"])
    doctor = runner.invoke(app, ["config", "doctor", "--json"])
    listed = runner.invoke(app, ["account", "list", "--json"])
    shown = runner.invoke(app, ["account", "show", "alice", "--json"])

    for result in (initialized, added, status, doctor, listed, shown):
        assert result.exit_code == 0, result.output
        document = _json_document(result)
        assert document["schema_version"] == 1
        assert document["ok"] is True
        assert document["warnings"] == []
        assert "incoming-secret" not in result.output

    assert _json_document(initialized)["command"] == "config.init"
    assert _json_document(status)["data"]["catalog_status"] == "available"  # type: ignore[index]
    assert _json_document(status)["data"]["restart_required"] is False  # type: ignore[index]
    assert _json_document(doctor)["data"]["catalog_revision"] == 2  # type: ignore[index]
    accounts = _json_document(listed)["data"]["accounts"]  # type: ignore[index]
    assert accounts == [
        {
            "name": "alice",
            "email_address": "alice@example.test",
            "enabled": True,
            "revision": 2,
            "has_outgoing": False,
            "incoming_binding": "ACTIVE",
            "outgoing_binding": None,
        }
    ]
    details = _json_document(shown)["data"]
    assert details["incoming"]["host"] == "imap.example.test"  # type: ignore[index]
    assert details["incoming"]["use_ssl"] is True  # type: ignore[index]
    assert details["outgoing"] is None  # type: ignore[index]


def test_cli_json_error_is_single_document_and_preflight_precedes_secret_read(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "missing" / "config.toml"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    read_secret = MagicMock(side_effect=AssertionError("secret must not be read"))
    monkeypatch.setattr("mcp_email_server.cli._read_secret", read_secret)

    result = CliRunner().invoke(app, [*_base_account_args(), "--json"], input="unused-secret\n")

    assert result.exit_code == 1
    document = _json_document(result)
    assert document == {
        "schema_version": 1,
        "ok": False,
        "command": "account.add",
        "error": {
            "code": "catalog_not_configured",
            "message": "No managed catalog is configured.",
            "details": {},
        },
        "warnings": [],
    }
    read_secret.assert_not_called()


def test_agent_readable_status_error_omits_bootstrap_path(monkeypatch, tmp_path: Path) -> None:
    parent = _private_directory(tmp_path / "private-owner-name")
    config_path = parent / "private-config.toml"
    config_path.write_text("[local_app\n", encoding="utf-8")
    if os.name == "posix":
        config_path.chmod(0o600)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))

    result = CliRunner().invoke(app, ["config", "status", "--json"])

    assert result.exit_code == 1
    document = _json_document(result)
    assert document["error"] == {  # type: ignore[index]
        "code": "bootstrap_unavailable",
        "message": "The bootstrap configuration is unavailable or busy.",
        "details": {},
    }
    assert str(config_path) not in result.output
    assert "private-owner-name" not in result.output


def test_json_mode_maps_account_validation_errors_to_single_documents(monkeypatch) -> None:
    management = MagicMock()
    _configure_bound_management(management)
    management.accounts.show.return_value = SimpleNamespace(revision=7, outgoing=None)
    management.credentials.set.side_effect = ValueError("account name must not contain control characters")
    management.accounts.update.side_effect = ValueError("full name must not contain control characters")
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )
    runner = CliRunner()

    set_secret = runner.invoke(
        app,
        ["account", "set-secret", "alice", "incoming", "--password-stdin", "--json"],
        input="private-value\n",
    )
    updated = runner.invoke(
        app,
        ["account", "update", "alice", "--expected-revision", "7", "--full-name", "Alice", "--json"],
    )

    for result, command in ((set_secret, "account.set-secret"), (updated, "account.update")):
        assert result.exit_code == 1
        document = _json_document(result)
        assert document["command"] == command
        assert document["error"]["code"] == "invalid_input"  # type: ignore[index]
        assert "private-value" not in result.output

    management.accounts.update.side_effect = ValueError("é" * APPLICATION_LIMITS.error_detail_bytes)
    bounded = runner.invoke(
        app,
        ["account", "update", "alice", "--expected-revision", "7", "--full-name", "Alice", "--json"],
    )
    message = _json_document(bounded)["error"]["message"]  # type: ignore[index]
    assert message == "The command input is invalid."


def test_lifecycle_json_mutations_use_committed_results_without_post_write_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    management = MagicMock()
    _configure_bound_management(management)
    management.lifecycle.initialize.return_value = CatalogInitializationResult(
        mode="legacy",
        bootstrap_revision=3,
        restart_required=True,
        catalog_revision=1,
        lifecycle="STAGING",
    )
    management.lifecycle.activate.return_value = CatalogActivationResult(catalog_revision=8, lifecycle="ACTIVE")
    management.lifecycle.select.return_value = ModeSelectionResult(
        mode="legacy",
        bootstrap_revision=3,
        restart_required=False,
    )
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )
    runner = CliRunner()

    management.lifecycle.status.side_effect = AssertionError("init must not observe after commit")
    initialized = runner.invoke(
        app,
        ["config", "init", "--database", str(tmp_path / "catalog.sqlite3"), "--json"],
    )
    assert initialized.exit_code == 0
    management.lifecycle.status.assert_not_called()

    management.lifecycle.status.reset_mock(side_effect=True)
    management.lifecycle.status.return_value = SimpleNamespace(
        selected_catalog="/private/catalog.sqlite3",
        bootstrap_revision=2,
        report=SimpleNamespace(catalog_revision=7),
    )
    activated = runner.invoke(app, ["config", "activate", "--json"])
    assert activated.exit_code == 0
    management.lifecycle.status.assert_called_once_with()

    management.lifecycle.status.reset_mock()
    selected = runner.invoke(app, ["config", "select", "legacy", "--json"])
    assert selected.exit_code == 0
    management.lifecycle.status.assert_called_once_with()


def test_every_finite_management_command_emits_one_json_success_document(monkeypatch, tmp_path: Path) -> None:
    endpoint = EndpointSummary(
        host="imap.example.test",
        port=993,
        use_ssl=True,
        start_ssl=False,
        verify_ssl=True,
        user_name="alice@example.test",
    )
    doctor = DoctorReport(
        lifecycle="STAGING",
        schema_version=1,
        catalog_revision=7,
        account_count=1,
        enabled_account_count=1,
        cleanup_required_bindings=0,
        problems=(),
    )
    policy = ManagedPolicy(
        revision=3,
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
    )
    account = AccountDetails(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        enabled=True,
        revision=7,
        save_to_sent=True,
        sent_folder_name=None,
        incoming=endpoint,
        outgoing=None,
        incoming_binding="ACTIVE",
        outgoing_binding=None,
    )
    credential = CredentialMutationResult(status="active", revision=8)
    management = MagicMock()
    _configure_bound_management(management)
    management.lifecycle.status.return_value = SimpleNamespace(
        mode="legacy",
        selected_catalog="configured",
        bootstrap_revision=2,
        restart_required=False,
        report=doctor,
        catalog_problem=None,
    )
    management.lifecycle.initialize.return_value = CatalogInitializationResult(
        mode="legacy",
        bootstrap_revision=3,
        restart_required=True,
        catalog_revision=1,
        lifecycle="STAGING",
    )
    management.lifecycle.activate.return_value = CatalogActivationResult(catalog_revision=8, lifecycle="ACTIVE")
    management.lifecycle.select.return_value = ModeSelectionResult(
        mode="legacy",
        bootstrap_revision=3,
        restart_required=False,
    )
    management.lifecycle.doctor.return_value = doctor
    management.index_health.get.return_value = IndexHealth("healthy", 1, 0, ())
    management.policy.get.return_value = policy
    management.policy.update.return_value = policy
    management.credentials.cleanup.return_value = CredentialCleanupReport(0, 0, 0)
    management.legacy_import.preview.return_value = SimpleNamespace(
        source_fingerprint="fingerprint",
        target_revision=7,
        target_policy_revision=3,
        has_conflicts=False,
        accounts=(),
        source_policy=policy,
        policy_action="unchanged",
        unsupported_provider_names=(),
    )
    management.accounts.create.return_value = AccountCreationResult(credential, None)
    management.accounts.list.return_value = []
    management.accounts.show.return_value = account
    management.accounts.update.return_value = 8
    management.accounts.disable.return_value = 8
    management.accounts.enable.return_value = 9
    management.accounts.soft_remove.return_value = AccountRemovalResult(8, 1, 1, 0)
    management.credentials.set.return_value = credential
    management.credentials.remove.return_value = CredentialRemovalResult("removed", 8)
    management.connectivity.execute = AsyncMock(
        return_value=ConnectivityResult("incoming", "ok", "Connection succeeded")
    )
    management.legacy_compatibility.migrate_credentials.return_value = LegacyCredentialMigrationResult(
        account_count=1,
        remaining_entries=(),
        unverifiable_entries=(),
        keyring_service="mcp-email-server",
    )
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )
    runner = CliRunner()
    invocations = [
        ("config.init", ["config", "init", "--database", str(tmp_path / "catalog.sqlite3"), "--json"], None),
        ("config.status", ["config", "status", "--json"], None),
        ("config.doctor", ["config", "doctor", "--json"], None),
        ("config.index-health", ["config", "index-health", "--json"], None),
        ("config.policy", ["config", "policy", "--json"], None),
        ("config.update-policy", ["config", "update-policy", "--expected-revision", "3", "--json"], None),
        ("config.cleanup-credentials", ["config", "cleanup-credentials", "--json"], None),
        ("config.import-legacy", ["config", "import-legacy", "--json"], None),
        ("config.activate", ["config", "activate", "--json"], None),
        ("config.select", ["config", "select", "legacy", "--json"], None),
        ("account.add", [*_base_account_args(), "--json"], "incoming-secret\n"),
        (
            "account.set-secret",
            ["account", "set-secret", "alice", "incoming", "--password-stdin", "--json"],
            "incoming-secret\n",
        ),
        ("account.list", ["account", "list", "--json"], None),
        ("account.show", ["account", "show", "alice", "--json"], None),
        (
            "account.update",
            ["account", "update", "alice", "--expected-revision", "7", "--full-name", "Alice", "--json"],
            None,
        ),
        ("account.disable", ["account", "disable", "alice", "--expected-revision", "7", "--json"], None),
        ("account.enable", ["account", "enable", "alice", "--expected-revision", "8", "--json"], None),
        (
            "account.remove",
            ["account", "remove", "alice", "--expected-revision", "7", "--confirm", "alice", "--json"],
            None,
        ),
        (
            "account.remove-secret",
            ["account", "remove-secret", "alice", "incoming", "--expected-revision", "7", "--json"],
            None,
        ),
        ("account.test", ["account", "test", "alice", "incoming", "--json"], None),
        ("reset", ["reset", "--confirm", "RESET", "--json"], None),
        ("migrate-credentials", ["migrate-credentials", "--json"], None),
    ]

    for command, arguments, stdin in invocations:
        result = runner.invoke(app, arguments, input=stdin)
        assert result.exit_code == 0, f"{command}: {result.output}"
        document = _json_document(result)
        assert document["schema_version"] == 1
        assert document["ok"] is True
        assert document["command"] == command
        assert isinstance(document["data"], dict)
        assert isinstance(document["warnings"], list)
        assert "incoming-secret" not in result.output


def test_migrate_credentials_json_redacts_bootstrap_path(monkeypatch, tmp_path: Path) -> None:
    sensitive_path = tmp_path / "private-config-name" / "config.toml"
    failure = BootstrapError(f"Could not inspect bootstrap lock: {sensitive_path}")
    monkeypatch.setattr(Settings, "migrate_credentials", MagicMock(side_effect=failure))
    management = ManagementServices.compose(LocalManagementBackend())
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )

    result = CliRunner().invoke(app, ["migrate-credentials", "--to", "plaintext", "--json"])

    assert result.exit_code == 1
    document = _json_document(result)
    assert document["ok"] is False
    assert sensitive_path.as_posix() not in result.output
    assert "private-config-name" not in result.output
    assert document["error"] == {
        "code": "bootstrap_unavailable",
        "message": "The bootstrap configuration is unavailable or busy.",
        "details": {},
    }


def test_migrate_credentials_json_redacts_filesystem_failure(monkeypatch, tmp_path: Path) -> None:
    sensitive_path = tmp_path / "private-config-name" / "config.toml"
    failure = OSError(f"Could not create configuration parent: {sensitive_path}")
    monkeypatch.setattr(Settings, "migrate_credentials", MagicMock(side_effect=failure))
    management = ManagementServices.compose(LocalManagementBackend())
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )

    result = CliRunner().invoke(app, ["migrate-credentials", "--to", "plaintext", "--json"])

    assert result.exit_code == 1
    document = _json_document(result)
    assert document["ok"] is False
    assert sensitive_path.as_posix() not in result.output
    assert "private-config-name" not in result.output
    assert document["error"] == {
        "code": "storage_unavailable",
        "message": "The required management storage is unavailable.",
        "details": {},
    }


def test_migrate_credentials_json_reports_cleanup_without_secret_locators(monkeypatch) -> None:
    management = MagicMock()
    _configure_bound_management(management)
    management.legacy_compatibility.migrate_credentials.return_value = LegacyCredentialMigrationResult(
        account_count=2,
        remaining_entries=("alice:incoming",),
        unverifiable_entries=("bob:outgoing",),
        keyring_service="mcp-email-server",
    )
    monkeypatch.delenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE", raising=False)
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )

    result = CliRunner().invoke(app, ["migrate-credentials", "--to", "plaintext", "--json"])

    assert result.exit_code == 0, result.output
    document = _json_document(result)
    assert document["data"] == {
        "target": "plaintext",
        "account_count": 2,
        "remaining_entry_count": 1,
        "unverifiable_entry_count": 1,
        "cleanup_complete": False,
    }
    assert [warning["code"] for warning in document["warnings"]] == [
        "keyring_entries_remaining",
        "keyring_cleanup_unverifiable",
    ]
    assert "alice:incoming" not in result.output
    assert "bob:outgoing" not in result.output
    assert "mcp-email-server" not in result.output


def test_all_finite_management_commands_advertise_json_mode() -> None:
    root = get_command(app)
    assert isinstance(root, Group)
    command_groups = {
        "config": {
            "init",
            "status",
            "doctor",
            "index-health",
            "policy",
            "update-policy",
            "cleanup-credentials",
            "import-legacy",
            "activate",
            "select",
        },
        "account": {
            "add",
            "set-secret",
            "list",
            "show",
            "update",
            "disable",
            "enable",
            "remove",
            "remove-secret",
            "test",
        },
    }
    for group_name, command_names in command_groups.items():
        group = root.commands[group_name]
        assert isinstance(group, Group)
        for command_name in command_names:
            command = group.commands[command_name]
            assert isinstance(command, Command)
            options = {
                option for parameter in command.params if isinstance(parameter, Option) for option in parameter.opts
            }
            assert "--json" in options, f"{group_name} {command_name}"

    for command_name in ("reset", "migrate-credentials"):
        command = root.commands[command_name]
        assert isinstance(command, Command)
        options = {option for parameter in command.params if isinstance(parameter, Option) for option in parameter.opts}
        assert "--json" in options


def test_reset_requires_exact_confirmation_and_supports_json(monkeypatch) -> None:
    management = MagicMock()
    _configure_bound_management(management)
    monkeypatch.setattr(
        "mcp_email_server.cli.get_application_runtime",
        lambda: SimpleNamespace(management=management),
    )
    runner = CliRunner()

    missing = runner.invoke(app, ["reset"])
    wrong = runner.invoke(app, ["reset", "--confirm", "reset"])
    confirmed = runner.invoke(app, ["reset", "--confirm", "RESET", "--json"])

    assert missing.exit_code == 2
    assert wrong.exit_code == 1
    assert "exactly RESET" in wrong.output
    assert confirmed.exit_code == 0, confirmed.output
    assert _json_document(confirmed)["data"] == {
        "reset": True,
        "scope": "persistent_legacy_configuration",
    }
    management.legacy_compatibility.reset.assert_called_once_with()


def test_account_add_validates_non_secret_endpoint_before_secret_read(monkeypatch, tmp_path) -> None:
    parent = _private_directory(tmp_path / "preflight")
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", str(config_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "init", "--database", str(database)]).exit_code == 0
    read_secret = MagicMock(side_effect=AssertionError("secret must not be read"))
    monkeypatch.setattr("mcp_email_server.cli._read_secret", read_secret)

    result = runner.invoke(app, [*_base_account_args(), "--imap-starttls", "--json"], input="unused\n")

    assert result.exit_code == 1
    document = _json_document(result)
    assert document["error"] == {  # type: ignore[index]
        "code": "invalid_input",
        "message": "The command input is invalid.",
        "details": {},
    }
    read_secret.assert_not_called()
