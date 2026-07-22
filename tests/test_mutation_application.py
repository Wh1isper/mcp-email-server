from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    ArchiveCommand,
    BatchMutationOutcome,
    DeleteCommand,
    DeliveryMutationOutcome,
    MarkReadCommand,
    MoveCommand,
    MutationAccountSnapshot,
    MutationProjectionError,
    MutationProviderAccess,
    MutationServices,
    SaveToMailboxCommand,
    SendCommand,
    SentCopyMutationOutcome,
    TargetMutationOutcome,
)
from mcp_email_server.emails.classic import ClassicEmailHandler


def _account(**changes: object) -> MutationAccountSnapshot:
    account = MutationAccountSnapshot(
        account_name="primary",
        mode="managed",
        allowed_senders=(),
        allowed_recipients=(),
        report_blocked_mutations=False,
    )
    return replace(account, **changes)


def _batch(*outcomes: TargetMutationOutcome) -> BatchMutationOutcome:
    return BatchMutationOutcome(outcomes)


def _services(
    *,
    account: MutationAccountSnapshot | None = None,
    provider: MagicMock | None = None,
    projection: MagicMock | None = None,
) -> tuple[MutationServices, MagicMock, MagicMock, MagicMock]:
    current = account if account is not None else _account()
    authority = MagicMock()
    authority.resolve.return_value = current
    selected_provider = provider if provider is not None else MagicMock()
    factory = MagicMock()
    factory.open.return_value = MutationProviderAccess(current, selected_provider)
    selected_projection = projection if projection is not None else MagicMock()
    if projection is None:
        selected_projection.invalidate = AsyncMock()
    projections = MagicMock()
    projections.open = AsyncMock(return_value=selected_projection)
    return (
        MutationServices.compose(authority, factory, projections),
        authority,
        factory,
        selected_projection,
    )


@pytest.mark.asyncio
async def test_mark_read_unknown_is_not_replayed_and_invalidates_projection() -> None:
    provider = MagicMock()
    provider.mark_read = AsyncMock(
        return_value=_batch(
            TargetMutationOutcome("9", "succeeded"),
            TargetMutationOutcome("10", "unknown", "store"),
        )
    )
    services, _, factory, projection = _services(provider=provider)

    result = await services.mark_read.execute(MarkReadCommand("primary", ("9", "10")))

    assert result.targets("succeeded") == ["9"]
    assert result.targets("unknown") == ["10"]
    provider.mark_read.assert_awaited_once()
    factory.open.assert_called_once_with("primary", expected_mode="managed")
    projection.invalidate.assert_awaited_once_with(("INBOX",))


@pytest.mark.asyncio
async def test_known_provider_success_survives_projection_failure_with_warning() -> None:
    provider = MagicMock()
    provider.delete = AsyncMock(return_value=_batch(TargetMutationOutcome("7", "succeeded")))
    projection = MagicMock()
    projection.invalidate = AsyncMock(side_effect=MutationProjectionError("unavailable"))
    services, _, _, _ = _services(provider=provider, projection=projection)

    result = await services.delete.execute(DeleteCommand("primary", ("7",)))

    assert result.targets("succeeded") == ["7"]
    assert result.reconciliation_needed is True


@pytest.mark.asyncio
async def test_projection_cancellation_does_not_erase_known_provider_success() -> None:
    provider = MagicMock()
    provider.delete = AsyncMock(return_value=_batch(TargetMutationOutcome("7", "succeeded")))
    projection = MagicMock()
    projection.invalidate = AsyncMock(side_effect=asyncio.CancelledError())
    services, _, _, _ = _services(provider=provider, projection=projection)

    result = await services.delete.execute(DeleteCommand("primary", ("7",)))

    assert result.targets("succeeded") == ["7"]
    assert result.reconciliation_needed is True


@pytest.mark.asyncio
async def test_known_failure_does_not_invalidate_projection() -> None:
    provider = MagicMock()
    provider.delete = AsyncMock(return_value=_batch(TargetMutationOutcome("7", "failed", "uidplus-unavailable")))
    services, _, _, projection = _services(provider=provider)

    result = await services.delete.execute(DeleteCommand("primary", ("7",)))

    assert result.targets("failed") == ["7"]
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_unknown_is_returned_once_and_marks_mailbox_stale() -> None:
    provider = MagicMock()
    provider.save_to_mailbox = AsyncMock(
        return_value=AppendMutationOutcome("unknown", "<draft@example.test>", mailbox="Drafts", detail="append")
    )
    services, _, factory, projection = _services(provider=provider)

    result = await services.save_to_mailbox.execute(
        SaveToMailboxCommand(
            account_name="primary",
            recipients=("recipient@example.test",),
            subject="Draft",
            body="body",
        )
    )

    assert result.status == "unknown"
    provider.save_to_mailbox.assert_awaited_once()
    factory.open.assert_called_once()
    projection.invalidate.assert_awaited_once_with(("Drafts",))


@pytest.mark.asyncio
async def test_archive_reopens_authority_between_discovery_and_move() -> None:
    provider = MagicMock()
    provider.find_archive_mailbox = AsyncMock(return_value="Archive")
    provider.move = AsyncMock(return_value=_batch(TargetMutationOutcome("11", "succeeded")))
    services, _, factory, projection = _services(provider=provider)

    result = await services.archive.execute(ArchiveCommand("primary", ("11",)))

    assert result.archive_mailbox == "Archive"
    assert factory.open.call_count == 2
    provider.move.assert_awaited_once()
    projection.invalidate.assert_awaited_once_with(("INBOX", "Archive"))


@pytest.mark.asyncio
async def test_send_preserves_partial_delivery_and_separate_sent_copy() -> None:
    provider = MagicMock()
    sent_message = object()
    provider.send = AsyncMock(
        return_value=DeliveryMutationOutcome(
            (
                TargetMutationOutcome("accepted@example.test", "succeeded"),
                TargetMutationOutcome("rejected@example.test", "failed", "smtp-rejected"),
            ),
            sent_message,
        )
    )
    provider.save_sent_copy = AsyncMock(return_value=SentCopyMutationOutcome("unknown", "Sent", "append"))
    services, _, factory, projection = _services(provider=provider)

    result = await services.send.execute(
        SendCommand(
            account_name="primary",
            recipients=("accepted@example.test", "rejected@example.test"),
            subject="Subject",
            body="body",
        )
    )

    assert result.recipients("succeeded") == ["accepted@example.test"]
    assert result.recipients("failed") == ["rejected@example.test"]
    assert result.sent_copy.status == "unknown"
    assert factory.open.call_count == 2
    provider.save_sent_copy.assert_awaited_once_with(sent_message, ())
    projection.invalidate.assert_awaited_once_with(("Sent",))


@pytest.mark.asyncio
async def test_production_send_path_hides_bcc_from_smtp_and_adds_it_only_to_fresh_sent_copy(
    email_settings,
) -> None:
    first_handler = ClassicEmailHandler(email_settings)
    sent_handler = ClassicEmailHandler(email_settings)
    first_provider = ClassicMutationProvider(first_handler)
    sent_provider = ClassicMutationProvider(sent_handler)
    account = _account()

    authority = MagicMock()
    authority.resolve.return_value = account
    factory = MagicMock()
    factory.open.side_effect = [
        MutationProviderAccess(account, first_provider),
        MutationProviderAccess(account, sent_provider),
    ]
    projection = MagicMock()
    projection.invalidate = AsyncMock()
    projections = MagicMock()
    projections.open = AsyncMock(return_value=projection)
    services = MutationServices.compose(authority, factory, projections)

    smtp = AsyncMock()
    smtp.__aenter__.return_value = smtp
    smtp.__aexit__.return_value = False
    smtp.supports_extension = MagicMock(return_value=False)

    async def rcpt(recipient: str, **_kwargs: str) -> None:
        if recipient == "rejected@example.test":
            from aiosmtplib.errors import SMTPRecipientRefused

            raise SMTPRecipientRefused(550, "rejected", recipient)

    smtp.rcpt.side_effect = rcpt

    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.id.return_value = MagicMock(result="OK")
    imap.list.return_value = ("OK", [])
    imap.select.return_value = ("OK", [])
    imap.append.return_value = ("OK", [])
    imap.protocol = SimpleNamespace(capabilities=("IMAP4rev1",), capability=AsyncMock())
    sent_handler.outgoing_client._connect_imap_server = AsyncMock(return_value=imap)

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await services.send.execute(
            SendCommand(
                account_name="primary",
                recipients=("accepted@example.test", "rejected@example.test"),
                bcc=("secret@example.test",),
                subject="Subject",
                body="body",
            )
        )

    assert result.recipients("succeeded") == ["accepted@example.test", "secret@example.test"]
    assert result.recipients("failed") == ["rejected@example.test"]
    assert result.sent_copy.status == "succeeded"
    smtp.data.assert_awaited_once()
    assert b"Bcc:" not in smtp.data.await_args.args[0]
    imap.append.assert_awaited_once()
    assert b"Bcc: secret@example.test" in imap.append.await_args.args[0]
    assert factory.open.call_count == 2


@pytest.mark.asyncio
async def test_send_delivery_survives_authority_failure_before_sent_copy() -> None:
    provider = MagicMock()
    provider.send = AsyncMock(
        return_value=DeliveryMutationOutcome(
            (TargetMutationOutcome("accepted@example.test", "succeeded"),),
            object(),
        )
    )
    services, _, factory, projection = _services(provider=provider)
    factory.open.side_effect = [factory.open.return_value, RuntimeError("configuration changed")]

    result = await services.send.execute(
        SendCommand(
            account_name="primary",
            recipients=("accepted@example.test",),
            subject="Subject",
            body="body",
        )
    )

    assert result.recipients("succeeded") == ["accepted@example.test"]
    assert result.sent_copy.status == "failed"
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_delivery_survives_pre_append_sent_copy_cancellation_as_failed() -> None:
    provider = MagicMock()
    provider.send = AsyncMock(
        return_value=DeliveryMutationOutcome(
            (TargetMutationOutcome("accepted@example.test", "succeeded"),),
            object(),
        )
    )
    provider.save_sent_copy = AsyncMock(side_effect=asyncio.CancelledError())
    services, _, _, projection = _services(provider=provider)

    result = await services.send.execute(
        SendCommand(
            account_name="primary",
            recipients=("accepted@example.test",),
            subject="Subject",
            body="body",
        )
    )

    assert result.recipients("succeeded") == ["accepted@example.test"]
    assert result.sent_copy.status == "failed"
    assert result.sent_copy.detail == "sent-copy-unavailable"
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_delivery_survives_untyped_sent_copy_failure_as_unknown() -> None:
    provider = MagicMock()
    provider.send = AsyncMock(
        return_value=DeliveryMutationOutcome(
            (TargetMutationOutcome("accepted@example.test", "succeeded"),),
            object(),
        )
    )
    provider.save_sent_copy = AsyncMock(side_effect=RuntimeError("unexpected"))
    services, _, _, projection = _services(provider=provider)

    result = await services.send.execute(
        SendCommand(
            account_name="primary",
            recipients=("accepted@example.test",),
            subject="Subject",
            body="body",
        )
    )

    assert result.recipients("succeeded") == ["accepted@example.test"]
    assert result.sent_copy.status == "unknown"
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_application_call_rejects_packed_recipient_values() -> None:
    services, _, factory, _ = _services(account=_account(allowed_recipients=("allowed@example.test",)))

    with pytest.raises(ValueError, match="exactly one email address"):
        await services.send.execute(
            SendCommand(
                account_name="primary",
                recipients=("allowed@example.test, blocked@example.test",),
                subject="Subject",
                body="body",
            )
        )

    factory.open.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("email_ids", [(), ("0",), ("01",), ("1", "1"), (str(2**32),)])
async def test_invalid_uid_batches_fail_before_provider_access(email_ids: tuple[str, ...]) -> None:
    services, _, factory, _ = _services()

    with pytest.raises(ValueError):
        await services.mark_read.execute(MarkReadCommand("primary", email_ids))

    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_move_rejects_identical_mailboxes_before_provider_access() -> None:
    services, _, factory, _ = _services()

    with pytest.raises(ValueError, match="must differ"):
        await services.move.execute(MoveCommand("primary", ("1",), "INBOX", "INBOX"))

    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_move_rejects_reserved_inbox_case_variant_before_provider_access() -> None:
    services, _, factory, _ = _services()

    with pytest.raises(ValueError, match="must differ"):
        await services.move.execute(MoveCommand("primary", ("1",), "INBOX", "inbox"))

    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_mailbox_control_characters_fail_before_provider_access() -> None:
    services, _, factory, _ = _services()

    with pytest.raises(ValueError, match="control characters"):
        await services.delete.execute(DeleteCommand("primary", ("1",), "INBOX\r\nEXPUNGE"))

    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_recipient_control_characters_fail_before_provider_access() -> None:
    services, _, factory, _ = _services()

    with pytest.raises(ValueError, match="control characters"):
        await services.send.execute(
            SendCommand(
                account_name="primary",
                recipients=("victim@example.test\r\nBcc: other@example.test",),
                subject="Subject",
                body="body",
            )
        )

    factory.open.assert_not_called()
