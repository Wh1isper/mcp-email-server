"""Junk/restore workflows absorbed from PR #254 into the existing move tool."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server import app as app_module
from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import (
    BatchMutationOutcome,
    MoveCommand,
    MoveMutationOutcome,
    MutationProjectionError,
    MutationProviderAccess,
    MutationProviderError,
    TargetMutationOutcome,
)
from mcp_email_server.emails.classic import ClassicEmailHandler
from mcp_email_server.emails.models import MailboxInfo
from tests.test_mutation_application import _account, _batch, _services


def _mailbox(name, *flags):
    return MailboxInfo(name=name, delimiter="/", flags=list(flags))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mailboxes", "expected"),
    [
        ([_mailbox("Spam"), _mailbox("Junk"), _mailbox("Courrier indésirable", r"\Junk")], "Courrier indésirable"),
        ([_mailbox("Localized", r"\jUnK")], "Localized"),
        ([_mailbox("Unavailable", r"\Junk", r"\NoSelect"), _mailbox("Spam")], "Spam"),
        ([_mailbox("Junk", r"\Noselect")], None),
        ([_mailbox("Other")], None),
        ([], None),
        *[([_mailbox(name)], name) for name in ("junk", "sPaM", "[Gmail]/Spam", "Junk E-mail", "Junk Email")],
    ],
)
async def test_junk_discovery_selectable_special_use_then_common_name(email_settings, mailboxes, expected):
    handler = ClassicEmailHandler(email_settings)
    handler.incoming_client.list_mailboxes = AsyncMock(return_value=mailboxes)
    assert await handler._find_junk_folder() == expected
    handler.incoming_client.list_mailboxes.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mailboxes",
    [
        [_mailbox("One", r"\Junk"), _mailbox("Two", r"\Junk")],
        [_mailbox("Junk"), _mailbox("Spam")],
        [_mailbox("Junk"), _mailbox("junk")],
    ],
)
async def test_junk_discovery_rejects_ambiguity_in_either_list_order(email_settings, mailboxes):
    handler = ClassicEmailHandler(email_settings)
    for ordered in (mailboxes, list(reversed(mailboxes))):
        handler.incoming_client.list_mailboxes = AsyncMock(return_value=ordered)
        with pytest.raises(ValueError, match="ambiguous"):
            await handler._find_junk_folder()


@pytest.mark.asyncio
async def test_junk_adapter_missing_and_provider_failure(email_settings):
    handler = ClassicEmailHandler(email_settings)
    handler.incoming_client.list_mailboxes = AsyncMock(return_value=[])
    provider = ClassicMutationProvider(handler)
    with pytest.raises(ValueError, match="No selectable Junk"):
        await provider.find_junk_mailbox()
    handler.incoming_client.list_mailboxes.side_effect = RuntimeError("synthetic raw provider error")
    with pytest.raises(MutationProviderError, match="provider_failure") as failure:
        await provider.find_junk_mailbox()
    assert "synthetic raw" not in str(failure.value)


@pytest.mark.asyncio
async def test_provider_rejects_unresolved_role_before_move(email_settings):
    handler = ClassicEmailHandler(email_settings)
    handler.incoming_client.move_emails_with_outcome = AsyncMock()
    provider = ClassicMutationProvider(handler)
    with pytest.raises(ValueError, match="must be resolved"):
        await provider.move(MoveCommand("primary", ("1",), "INBOX", destination_role="junk"), _account())
    handler.incoming_client.move_emails_with_outcome.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("destination", "role", "message"),
    [
        (None, None, "exactly one"),
        ("Junk", "junk", "exactly one"),
        (None, "archive", "must be 'junk'"),
        ("", None, "empty"),
    ],
)
async def test_move_selector_validation_precedes_account_and_provider(destination, role, message):
    services, authority, factory, _ = _services()
    with pytest.raises(ValueError, match=message):
        await services.move.execute(MoveCommand("primary", ("1",), "INBOX", destination, role))
    authority.resolve.assert_not_called()
    factory.open.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["managed", "legacy"])
async def test_junk_move_resolves_only_destination_and_reopens_authority(mode):
    provider = MagicMock()
    provider.find_junk_mailbox = AsyncMock(return_value="Courrier indésirable")
    provider.move = AsyncMock(return_value=_batch(TargetMutationOutcome("11", "succeeded")))
    services, _, factory, projection = _services(account=_account(mode=mode), provider=provider)
    result = await services.move.execute(MoveCommand("primary", ("11",), "Reviewed", destination_role="junk"))
    assert result.destination_mailbox == "Courrier indésirable"
    assert result.batch.targets("succeeded") == ["11"]
    assert factory.open.call_count == 2
    assert all(call.kwargs["expected_mode"] == mode for call in factory.open.call_args_list)
    provider.move.assert_awaited_once_with(
        MoveCommand("primary", ("11",), "Reviewed", "Courrier indésirable"), factory.open.return_value.account
    )
    projection.invalidate.assert_awaited_once_with(("Reviewed", "Courrier indésirable"))


@pytest.mark.asyncio
async def test_explicit_restore_uses_caller_junk_uids_without_discovery():
    provider = MagicMock()
    provider.find_junk_mailbox = AsyncMock()
    provider.move = AsyncMock(return_value=_batch(TargetMutationOutcome("72", "succeeded")))
    services, _, factory, projection = _services(provider=provider)
    command = MoveCommand("primary", ("72",), "Localized Junk", "INBOX")
    result = await services.move.execute(command)
    assert result.destination_mailbox == "INBOX"
    provider.find_junk_mailbox.assert_not_awaited()
    provider.move.assert_awaited_once_with(command, factory.open.return_value.account)
    factory.open.assert_called_once()
    projection.invalidate.assert_awaited_once_with(("Localized Junk", "INBOX"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["resolve", "open", "after-discovery"])
async def test_junk_move_requires_organize_before_discovery_and_after_it(stage):
    allowed = _account(allowed_mutations=("organize",))
    denied = replace(allowed, allowed_mutations=())
    provider = MagicMock()
    provider.find_junk_mailbox = AsyncMock(return_value="Junk")
    provider.move = AsyncMock()
    services, _, factory, projection = _services(account=denied if stage == "resolve" else allowed, provider=provider)
    factory.open.side_effect = [
        MutationProviderAccess(denied if stage == "open" else allowed, provider),
        MutationProviderAccess(denied, provider),
    ]
    with pytest.raises(PermissionError, match="organize"):
        await services.move.execute(MoveCommand("primary", ("11",), "INBOX", destination_role="junk"))
    assert provider.find_junk_mailbox.await_count == (1 if stage == "after-discovery" else 0)
    provider.move.assert_not_awaited()
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_disabled_after_junk_discovery_prevents_move():
    provider = MagicMock()
    provider.find_junk_mailbox = AsyncMock(return_value="Junk")
    provider.move = AsyncMock()
    services, _, factory, projection = _services(provider=provider)
    factory.open.side_effect = [MutationProviderAccess(_account(), provider), ValueError("Account disabled")]
    with pytest.raises(ValueError, match="disabled"):
        await services.move.execute(MoveCommand("primary", ("11",), "INBOX", destination_role="junk"))
    provider.move.assert_not_awaited()
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["INBOX", "inbox", "Bad\nMailbox"])
async def test_discovered_destination_is_validated_before_move(destination):
    provider = MagicMock()
    provider.find_junk_mailbox = AsyncMock(return_value=destination)
    provider.move = AsyncMock()
    services, _, factory, projection = _services(provider=provider)
    with pytest.raises(ValueError):
        await services.move.execute(MoveCommand("primary", ("11",), "INBOX", destination_role="junk"))
    factory.open.assert_called_once()
    provider.move.assert_not_awaited()
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_timeout_is_definite_before_move():
    provider = MagicMock()
    provider.find_junk_mailbox = AsyncMock(side_effect=TimeoutError)
    provider.move = AsyncMock()
    services, _, factory, projection = _services(provider=provider)
    with pytest.raises(MutationProviderError, match="junk mailbox discovery timed out"):
        await services.move.execute(MoveCommand("primary", ("11",), "INBOX", destination_role="junk"))
    factory.open.assert_called_once()
    provider.move.assert_not_awaited()
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, "junk"])
@pytest.mark.parametrize("status", ["failed", "unknown", "succeeded"])
async def test_move_stale_uid_timeout_and_projection_failure_share_one_pipeline(role, status):
    provider = MagicMock()
    provider.find_junk_mailbox = AsyncMock(return_value="Junk")
    provider.move = AsyncMock(return_value=_batch(TargetMutationOutcome("11", status)))
    if status == "unknown":
        provider.move.side_effect = TimeoutError
    projection = MagicMock()
    projection.invalidate = AsyncMock(side_effect=MutationProjectionError("synthetic projection failure"))
    services, _, _, _ = _services(provider=provider, projection=projection)
    result = await services.move.execute(MoveCommand("primary", ("11",), "INBOX", None if role else "Junk", role))
    assert result.destination_mailbox == "Junk"
    assert result.batch.targets(status) == ["11"]
    assert result.batch.reconciliation_needed is (status != "failed")
    provider.move.assert_awaited_once()
    if status == "failed":
        projection.invalidate.assert_not_awaited()
    else:
        projection.invalidate.assert_awaited_once_with(("INBOX", "Junk"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "unknown"])
async def test_mcp_junk_move_reports_resolved_mailbox_and_preserves_order(status):
    batch = BatchMutationOutcome((TargetMutationOutcome("11", "succeeded"), TargetMutationOutcome("12", status)))
    handler = AsyncMock(return_value=MoveMutationOutcome(batch, "Localized Junk"))
    with patch("mcp_email_server.app.move_emails_command", handler):
        result = await app_module.move_emails(
            "primary", ["11", "12"], source_mailbox="Reviewed", destination_role="junk"
        )
    command = handler.await_args.args[0]
    assert command.source_mailbox == "Reviewed"
    assert command.destination_mailbox is None
    assert command.destination_role == "junk"
    if status == "succeeded":
        assert result == "Successfully moved 2 email(s) to Localized Junk"
    else:
        assert (
            result
            == "Move result [succeeded: 11; unknown: 12; warning: reconciliation needed; mailbox: Localized Junk]"
        )


@pytest.mark.asyncio
async def test_move_schema_is_additive_without_new_tools():
    tools = {tool.name: tool for tool in await app_module.mcp.list_tools()}
    assert len(tools) == 20
    assert not {"mark_as_spam", "mark_as_ham"} & tools.keys()
    schema = tools["move_emails"].inputSchema
    assert schema["required"] == ["account_name", "email_ids"]
    assert schema["properties"]["destination_mailbox"]["default"] is None
    assert schema["properties"]["source_mailbox"]["default"] == "INBOX"
    assert schema["properties"]["destination_role"]["anyOf"][0]["const"] == "junk"
