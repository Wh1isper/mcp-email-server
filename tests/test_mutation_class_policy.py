from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from mcp_email_server.adapters.mutations import ClassicMutationProvider, LocalMutationBackend
from mcp_email_server.application.mutation_policy import DEFAULT_ALLOWED_MUTATIONS
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    ArchiveCommand,
    CreateMailboxCommand,
    DeleteCommand,
    DraftAppendCommand,
    MarkReadCommand,
    MoveCommand,
    MutationProviderAccess,
    SaveDraftCommand,
    SaveToMailboxCommand,
    SendCommand,
    SetEmailFlagsCommand,
    SetEmailTagsCommand,
)
from mcp_email_server.config import EmailSettings, Settings
from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.emails.models import MailboxInfo
from mcp_email_server.managed import ManagedCatalog
from tests.test_mutation_application import _account, _services
from tests.test_mutation_provider_outcomes import _imap


@pytest.mark.parametrize("values", [[], ["draft"], ["draft", "organize"], list(DEFAULT_ALLOWED_MUTATIONS)])
def test_config_grants_replace_not_union(email_settings, values):
    settings = Settings.model_construct(allowed_mutations=["send"])
    account = email_settings.model_copy(update={"allowed_mutations": values})
    resolved = MagicMock(account=account, settings=settings, mode="legacy")
    with patch("mcp_email_server.adapters.mutations.resolve_local_account", return_value=resolved):
        assert LocalMutationBackend().resolve(account.account_name).allowed_mutations == tuple(values)
    account.allowed_mutations = None
    with patch("mcp_email_server.adapters.mutations.resolve_local_account", return_value=resolved):
        assert LocalMutationBackend().resolve(account.account_name).allowed_mutations == ("send",)


def test_config_stable_defaults_and_invalid_grants(email_settings):
    assert Settings.model_construct().allowed_mutations == list(DEFAULT_ALLOWED_MUTATIONS)
    assert email_settings.allowed_mutations is None
    for grants in [["all"], ["send", "send"]]:
        with pytest.raises(ValidationError):
            EmailSettings.model_validate({**email_settings.model_dump(), "allowed_mutations": grants})


def test_toml_roundtrip_and_env_preserve_readonly(tmp_path, monkeypatch, email_settings):
    path = tmp_path / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", str(path))
    monkeypatch.setattr("mcp_email_server.config.CONFIG_PATH", path)
    settings = Settings.model_construct(credential_storage="plaintext", emails=[email_settings], allowed_mutations=[])
    settings.emails[0].allowed_mutations = ["draft"]
    settings.emails[0].drafts_mailbox = "My Drafts"
    settings.store()
    loaded = Settings.load_for_migration()
    assert loaded.allowed_mutations == []
    assert loaded.emails[0].allowed_mutations == ["draft"]
    assert loaded.emails[0].drafts_mailbox == "My Drafts"
    monkeypatch.setenv("MCP_EMAIL_SERVER_ALLOWED_MUTATIONS", "send,append")
    assert Settings().allowed_mutations == ["send", "append"]
    assert Settings.load_for_migration().allowed_mutations == []


@pytest.mark.parametrize(
    ("service", "command"),
    [
        ("set_flags", SetEmailFlagsCommand("primary", ("1",), "add", (r"\Seen",))),
        ("set_tags", SetEmailTagsCommand("primary", ("1",), "add", ("work",))),
        ("mark_read", MarkReadCommand("primary", ("1",))),
        ("delete", DeleteCommand("primary", ("1",))),
        ("move", MoveCommand("primary", ("1",), "INBOX", "Other")),
        ("archive", ArchiveCommand("primary", ("1",))),
        ("create_mailbox", CreateMailboxCommand("primary", "Other")),
        ("save_to_mailbox", SaveToMailboxCommand("primary", ("recipient@example.test",), "subject", "body")),
        ("send", SendCommand("primary", ("recipient@example.test",), "subject", "body")),
        ("save_draft", SaveDraftCommand("primary", (), "subject", "body")),
    ],
)
@pytest.mark.asyncio
async def test_readonly_denies_before_provider_open(service, command):
    services, _, factory, _ = _services(account=_account(allowed_mutations=()))
    with pytest.raises(PermissionError, match="not allowed"):
        await getattr(services, service).execute(command)
    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_recipientless_draft_only_uses_draft_and_fixed_target_flags():
    provider = MagicMock()
    provider.save_to_mailbox = AsyncMock(
        return_value=AppendMutationOutcome("succeeded", "draft-id", mailbox="My Drafts")
    )
    services, _, _, _ = _services(
        account=_account(allowed_mutations=("draft",), allowed_recipients=(), drafts_mailbox="My Drafts"),
        provider=provider,
    )
    result = await services.save_draft.execute(SaveDraftCommand("primary", (), "subject", "body"))
    assert result.status == "succeeded"
    command = provider.save_to_mailbox.await_args.args[0]
    assert isinstance(command, DraftAppendCommand)
    assert command.mailbox == "My Drafts"
    assert command.flags == (r"\Draft",)
    assert command.recipients == ()
    provider.find_drafts_mailbox.assert_not_called()
    with pytest.raises(PermissionError):
        await services.save_draft.execute(SaveDraftCommand("primary", ("blocked@example.test",), "subject", "body"))


@pytest.mark.asyncio
async def test_draft_rechecks_authority_after_discovery():
    provider = MagicMock()
    provider.find_drafts_mailbox = AsyncMock(return_value="Drafts")
    provider.save_to_mailbox = AsyncMock()
    account = _account(allowed_mutations=("draft",))
    services, _, factory, _ = _services(account=account, provider=provider)
    factory.open.side_effect = [
        MutationProviderAccess(account, provider),
        MutationProviderAccess(replace(account, allowed_mutations=()), provider),
    ]
    with pytest.raises(PermissionError):
        await services.save_draft.execute(SaveDraftCommand("primary", (), "subject", "body"))
    provider.save_to_mailbox.assert_not_awaited()


@pytest.mark.parametrize("flags", [[], [r"\Drafts"], [r"\Drafts", r"\NoSelect"]])
@pytest.mark.asyncio
async def test_draft_discovery_requires_unique_special_use(flags):
    handler = MagicMock()
    handler.incoming_client.list_mailboxes = AsyncMock(
        return_value=[MailboxInfo(name="Localized", delimiter="/", flags=flags)]
    )
    provider = ClassicMutationProvider(handler)
    if r"\Drafts" not in flags or r"\NoSelect" in flags:
        with pytest.raises(ValueError):
            await provider.find_drafts_mailbox()
    else:
        assert await provider.find_drafts_mailbox() == "Localized"
        handler.incoming_client.list_mailboxes.return_value *= 2
        with pytest.raises(ValueError):
            await provider.find_drafts_mailbox()


@pytest.mark.parametrize("flag", [r"\Deleted", r"\deleted"])
@pytest.mark.asyncio
async def test_full_grant_append_preserves_deleted_flag(flag):
    command = SaveToMailboxCommand("primary", ("recipient@example.test",), "s", "b", flags=(flag,))
    command.validate()
    provider = MagicMock()
    provider.save_to_mailbox = AsyncMock(
        return_value=AppendMutationOutcome("succeeded", "message-id", mailbox="Drafts")
    )
    services, _, _, _ = _services(provider=provider)
    result = await services.save_to_mailbox.execute(command)
    assert result.status == "succeeded"
    assert provider.save_to_mailbox.await_args.args[0].flags == (flag,)


@pytest.mark.parametrize("flag", [r"\Deleted", r"\deleted"])
@pytest.mark.parametrize("grants", [("append",), ("delete",), ()])
@pytest.mark.asyncio
async def test_deleted_append_requires_both_grants_before_provider_open(flag, grants):
    services, _, factory, _ = _services(account=_account(allowed_mutations=grants))
    with pytest.raises(PermissionError, match="not allowed"):
        await services.save_to_mailbox.execute(
            SaveToMailboxCommand("primary", ("recipient@example.test",), "s", "b", flags=(flag,))
        )
    factory.open.assert_not_called()


@pytest.mark.parametrize("flags", [None, (), (r"\Seen",)])
@pytest.mark.asyncio
async def test_ordinary_append_does_not_require_delete_grant(flags):
    provider = MagicMock()
    provider.save_to_mailbox = AsyncMock(
        return_value=AppendMutationOutcome("succeeded", "message-id", mailbox="Drafts")
    )
    services, _, _, _ = _services(account=_account(allowed_mutations=("append",)), provider=provider)
    result = await services.save_to_mailbox.execute(
        SaveToMailboxCommand("primary", ("recipient@example.test",), "s", "b", flags=flags)
    )
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_deleted_append_rechecks_opened_account_grants():
    account = _account(allowed_mutations=("append", "delete"))
    services, _, factory, _ = _services(account=account)
    provider = MagicMock()
    provider.save_to_mailbox = AsyncMock()
    factory.open.return_value = MutationProviderAccess(replace(account, allowed_mutations=("append",)), provider)
    with pytest.raises(PermissionError, match="delete"):
        await services.save_to_mailbox.execute(
            SaveToMailboxCommand("primary", ("recipient@example.test",), "s", "b", flags=(r"\Deleted",))
        )
    provider.save_to_mailbox.assert_not_awaited()


@pytest.mark.parametrize("revoke_delete", [False, True])
@pytest.mark.asyncio
async def test_deleted_append_checks_delete_at_provider_effect(email_settings, revoke_delete):
    from mcp_email_server.emails.classic import ClassicEmailHandler

    snapshot = _account(allowed_mutations=("append", "delete"))
    fresh = MagicMock(return_value=snapshot)
    handler = ClassicEmailHandler(email_settings)
    provider = ClassicMutationProvider(handler, fresh)
    imap = _imap()

    async def connected(*args, **kwargs):
        if revoke_delete:
            fresh.return_value = replace(snapshot, allowed_mutations=("append",))
        return imap

    command = SaveToMailboxCommand("primary", ("recipient@example.test",), "s", "b", flags=(r"\Deleted",))
    with patch.object(handler.incoming_client, "_connect_imap_server", AsyncMock(side_effect=connected)):
        if revoke_delete:
            with pytest.raises(PermissionError, match="delete"):
                await provider.save_to_mailbox(command, snapshot)
            imap.append.assert_not_awaited()
        else:
            result = await provider.save_to_mailbox(command, snapshot)
            assert result.status == "succeeded"
            assert imap.append.await_args.kwargs["flags"] == r"(\Deleted)"
    imap.uid.assert_not_awaited()
    imap.expunge.assert_not_awaited()


@pytest.mark.asyncio
async def test_per_uid_fresh_guard_preserves_first_success(email_server):
    client = EmailClient(email_server)
    imap = _imap()
    client.mutation_guard = MagicMock(side_effect=[None, PermissionError("revoked")])
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.set_email_flags_with_outcome(["1", "2"], "add", [r"\Seen"], allowed_senders=[])
    assert [item.status for item in result.outcomes] == ["succeeded", "failed"]
    assert imap.uid.await_count == 1


@pytest.mark.asyncio
async def test_move_revocation_after_copy_is_unknown_no_delete_or_expunge(email_server):
    client = EmailClient(email_server)
    imap = _imap()
    client.mutation_guard = MagicMock(side_effect=[None, PermissionError("revoked")])
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["1"], "INBOX", "Other", allowed_senders=[])
    assert result.outcomes[0].status == "unknown"
    assert result.reconciliation_needed
    assert [call.args[0] for call in imap.uid.await_args_list] == ["copy"]


def test_managed_roundtrip_nullable_override_reset(tmp_path: Path, email_server):
    catalog = ManagedCatalog.initialize(tmp_path / "managed.sqlite3")
    assert catalog.policy().allowed_mutations == DEFAULT_ALLOWED_MUTATIONS
    catalog.update_policy(
        expected_revision=1,
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
        allowed_mutations=(),
    )
    catalog.add_account(
        name="primary",
        full_name="Primary",
        email_address="primary@example.test",
        incoming=email_server,
        outgoing=None,
        allowed_mutations=("draft",),
        drafts_mailbox="Localized",
    )
    assert catalog.show_account("primary").allowed_mutations == ("draft",)
    assert catalog.show_account("primary").drafts_mailbox == "Localized"
    catalog.set_secret("primary", "incoming", "synthetic-secret", expected_revision=1)
    catalog.update_account("primary", expected_revision=2, allowed_mutations=(), update_allowed_mutations=True)
    assert catalog.show_account("primary").allowed_mutations == ()
    catalog.update_account(
        "primary", expected_revision=3, update_allowed_mutations=True, drafts_mailbox=None, update_drafts_mailbox=True
    )
    assert catalog.show_account("primary").allowed_mutations is None
    assert catalog.show_account("primary").drafts_mailbox is None


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["grant", "account"])
async def test_smtp_revocation_after_rcpt_is_definite_no_data(email_settings, revocation):
    from mcp_email_server.emails.classic import ClassicEmailHandler
    from tests.test_mutation_provider_outcomes import _smtp

    handler = ClassicEmailHandler(email_settings)
    snapshot = _account(allowed_mutations=("send",), allowed_recipients=("*@example.test",))
    fresh = MagicMock(return_value=snapshot)
    provider = ClassicMutationProvider(handler, fresh)
    smtp = _smtp()

    async def revoke(*args, **kwargs):
        if revocation == "account":
            fresh.side_effect = ValueError("Account disabled")
        else:
            fresh.return_value = replace(snapshot, allowed_mutations=())

    smtp.rcpt.side_effect = revoke
    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await provider.send(SendCommand("primary", ("recipient@example.test",), "s", "b"), snapshot)
    assert [item.status for item in result.outcomes] == ["failed"]
    smtp.data.assert_not_awaited()


@pytest.mark.parametrize("error", [ValueError("disabled"), RuntimeError("catalog unavailable")])
def test_fresh_resolution_denial_is_permission_error(error):
    provider = ClassicMutationProvider(MagicMock(), MagicMock(side_effect=error))
    with pytest.raises(PermissionError, match="authority is unavailable"):
        provider._guard(_account(), "organize")


@pytest.mark.parametrize("source_version", [3, 4])
def test_migration_preserves_full_default_inheritance_and_existing_rows(tmp_path, source_version):
    import json
    import sqlite3

    from mcp_email_server import managed as managed_module
    from tests.test_managed_catalog import _tag, _v3_catalog

    catalog = _v3_catalog(tmp_path)
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.row_factory = sqlite3.Row
        if source_version == 4:
            connection.executescript(managed_module._SCHEMA_V4_ADDITIONS)
            connection.execute("UPDATE schema_metadata SET version = 4")
            connection.execute("UPDATE catalog SET enable_attachment_content = 1")
            connection.execute("UPDATE managed_account SET tags_json = ?", (json.dumps([_tag().model_dump()]),))
        connection.commit()
        original = {
            query: connection.execute(query).fetchall()
            for query in (
                "SELECT * FROM catalog",
                "SELECT * FROM managed_account",
                "SELECT * FROM endpoint",
                "SELECT * FROM secret_binding",
                "SELECT * FROM managed_secret",
            )
        }
    store = MagicMock()
    migrated = ManagedCatalog(catalog.path, secret_store=store)
    policy = migrated.policy()
    account = migrated.show_account("alice")
    assert policy.allowed_mutations == DEFAULT_ALLOWED_MUTATIONS
    assert account.allowed_mutations is None
    assert account.drafts_mailbox is None
    assert policy.allowed_recipients == ("bob@example.test",)
    assert policy.enable_attachment_content is (source_version == 4)
    assert account.tags == ((_tag(),) if source_version == 4 else ())
    store.get.assert_not_called()
    store.put.assert_not_called()
    store.delete.assert_not_called()
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == 5
        for query, old_rows in original.items():
            new_rows = connection.execute(query).fetchall()
            columns = tuple(old_rows[0].keys())
            assert [tuple(row[key] for key in columns) for row in new_rows] == [tuple(row) for row in old_rows]
    assert ManagedCatalog(catalog.path, secret_store=store).policy() == policy
    assert migrated.show_account("alice") == account


@pytest.mark.parametrize("source_version", [3, 4])
def test_v5_migration_failure_rolls_back_source_schema_and_can_retry(monkeypatch, tmp_path, source_version):
    import sqlite3

    from mcp_email_server import managed as managed_module
    from tests.test_managed_catalog import _v3_catalog

    catalog = _v3_catalog(tmp_path)
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.row_factory = sqlite3.Row
        if source_version == 4:
            connection.executescript(managed_module._SCHEMA_V4_ADDITIONS)
            connection.execute("UPDATE schema_metadata SET version = 4")
            connection.commit()
        original_schema = managed_module._schema_objects(connection)
    execute_schema = managed_module._execute_schema

    def fail_during_v5(connection, schema):
        if schema == managed_module._SCHEMA_V5_ADDITIONS:
            connection.execute("ALTER TABLE managed_account ADD COLUMN drafts_mailbox TEXT")
            raise RuntimeError("synthetic v5 migration failure")
        execute_schema(connection, schema)

    with monkeypatch.context() as scoped:
        scoped.setattr(managed_module, "_execute_schema", fail_during_v5)
        with pytest.raises(RuntimeError, match="synthetic v5 migration failure"):
            catalog.policy()
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == source_version
        assert managed_module._schema_objects(connection) == original_schema
        assert connection.execute("SELECT revision FROM catalog").fetchone()[0] == 7
        assert connection.execute("SELECT revision FROM managed_account").fetchone()[0] == 5
        assert connection.execute("SELECT secret_value FROM managed_secret").fetchone()[0] == "stored-secret"
    assert catalog.policy().allowed_mutations == DEFAULT_ALLOWED_MUTATIONS


@pytest.mark.asyncio
async def test_tags_revocation_is_definite_failure(email_server):
    client = EmailClient(email_server)
    client.mutation_guard = MagicMock(side_effect=PermissionError("revoked"))
    imap = _imap()
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.set_email_tags_with_outcome(["1"], "add", ["$Tag"], allowed_senders=[])
    assert result.outcomes[0].status == "failed"
    imap.uid.assert_not_awaited()


@pytest.mark.asyncio
async def test_sent_copy_revalidates_recipients_without_append_grant(email_settings):
    from email.mime.text import MIMEText

    from mcp_email_server.emails.classic import ClassicEmailHandler

    snapshot = _account(allowed_mutations=("send",), allowed_recipients=("*@example.test",))
    fresh = MagicMock(return_value=snapshot)
    handler = ClassicEmailHandler(email_settings)
    handler.incoming_client.append_to_sent_with_outcome = AsyncMock()
    provider = ClassicMutationProvider(handler, fresh)
    message = MIMEText("body")
    message["To"] = "recipient@example.test"
    fresh.return_value = replace(snapshot, allowed_recipients=())
    with pytest.raises(PermissionError):
        await provider.save_sent_copy(message, ())
    handler.incoming_client.append_to_sent_with_outcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_expunge_revocation_preserves_copy_store_evidence(email_server):
    client = EmailClient(email_server)
    client.mutation_guard = MagicMock(side_effect=[None, None, PermissionError("revoked")])
    imap = _imap()
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["1"], "INBOX", "Other", allowed_senders=[])
    assert result.outcomes[0].status == "unknown"
    assert result.reconciliation_needed
    assert [call.args[0] for call in imap.uid.await_args_list] == ["copy", "store"]
    imap.expunge.assert_not_awaited()


@pytest.mark.asyncio
async def test_append_revocation_before_effect(email_server):
    from email.mime.text import MIMEText

    client = EmailClient(email_server)
    client.mutation_guard = MagicMock(side_effect=PermissionError("revoked"))
    imap = _imap()
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        with pytest.raises(PermissionError):
            await client.append_to_mailbox_with_outcome(MIMEText("body"), email_server, "Drafts")
    imap.append.assert_not_awaited()
