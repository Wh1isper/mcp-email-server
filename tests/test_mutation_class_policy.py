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
def test_append_cannot_smuggle_deleted_flag(flag):
    with pytest.raises(ValueError, match="Deleted"):
        SaveToMailboxCommand("primary", ("recipient@example.test",), "s", "b", flags=(flag,)).validate()


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


def test_v4_migration_preserves_full_default_and_inheritance(tmp_path):
    import sqlite3

    from mcp_email_server import managed as managed_module
    from tests.test_managed_catalog import _v3_catalog

    catalog = _v3_catalog(tmp_path)
    with closing(sqlite3.connect(catalog.path)) as connection:
        connection.executescript(managed_module._SCHEMA_V4_ADDITIONS)
        connection.execute("UPDATE schema_metadata SET version = 4")
        connection.commit()
    migrated = ManagedCatalog(catalog.path)
    assert migrated.policy().allowed_mutations == DEFAULT_ALLOWED_MUTATIONS
    assert migrated.show_account("alice").allowed_mutations is None
    assert migrated.show_account("alice").drafts_mailbox is None
    assert migrated.policy().allowed_recipients == ("bob@example.test",)
    with closing(sqlite3.connect(catalog.path)) as connection:
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == 5


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
