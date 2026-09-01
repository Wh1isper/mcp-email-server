from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application import mutations as mutations_module
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    ArchiveCommand,
    BatchMutationOutcome,
    DeleteCommand,
    DeliveryMutationOutcome,
    FlagOperation,
    ForwardCommand,
    ForwardSource,
    ForwardSourcePart,
    MarkReadCommand,
    MoveCommand,
    MutableEmailFlag,
    MutationAccountSnapshot,
    MutationProjectionError,
    MutationProviderAccess,
    MutationProviderError,
    MutationServices,
    RecipientPolicyDeniedError,
    SaveToMailboxCommand,
    SendCommand,
    SendMutationOutcome,
    SentCopyMutationOutcome,
    SetEmailFlagsCommand,
    SetEmailTagsCommand,
    TargetMutationOutcome,
)
from mcp_email_server.emails.classic import ClassicEmailHandler
from mcp_email_server.imap_keywords import ImapKeywordRegistry, ImapKeywordTag


def _account(**changes: object) -> MutationAccountSnapshot:
    account = MutationAccountSnapshot(
        account_name="primary",
        mode="managed",
        allowed_senders=(),
        allowed_recipients=(),
        report_blocked_mutations=False,
        can_send=True,
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


def _tag_registry() -> ImapKeywordRegistry:
    return ImapKeywordRegistry.from_tags((
        ImapKeywordTag(name="todo", keyword="$label4", writable=True),
        ImapKeywordTag(name="important", keyword="$label1", writable=False),
    ))


@pytest.mark.asyncio
async def test_set_email_tags_resolves_writable_names_and_invalidates_projection() -> None:
    provider = MagicMock()
    provider.set_tags = AsyncMock(return_value=_batch(TargetMutationOutcome("1", "succeeded")))
    services, _authority, _factory, projection = _services(
        account=_account(tag_registry=_tag_registry()),
        provider=provider,
    )

    result = await services.set_tags.execute(SetEmailTagsCommand("primary", ("1",), "add", ("todo",), "Archive"))

    assert result.targets("succeeded") == ["1"]
    dispatched = provider.set_tags.await_args.args[0]
    assert dispatched.operation == "add"
    assert dispatched.tags == ("$label4",)
    assert dispatched.mailbox == "Archive"
    projection.invalidate.assert_awaited_once_with(("Archive",))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "expected_error"),
    [("missing", ValueError), ("important", PermissionError)],
)
async def test_set_email_tags_rejects_unknown_or_read_only_before_provider(
    tag: str,
    expected_error: type[Exception],
) -> None:
    services, _authority, factory, _projection = _services(account=_account(tag_registry=_tag_registry()))

    with pytest.raises(expected_error):
        await services.set_tags.execute(SetEmailTagsCommand("primary", ("1",), "remove", (tag,), "INBOX"))

    factory.open.assert_not_called()


def test_set_email_tags_command_requires_non_empty_tags() -> None:
    with pytest.raises(ValueError, match="tags must not be empty"):
        SetEmailTagsCommand("primary", ("1",), "add", (), "INBOX").validate()


@pytest.mark.parametrize(
    ("operation", "flags", "message"),
    [
        ("replace", (r"\Seen",), "operation must be"),
        ("add", (), "flags must not be empty"),
        (
            "add",
            (r"\Seen", r"\Flagged", r"\Answered", r"\Draft", r"\Seen"),
            "flags must contain at most",
        ),
        ("remove", (1,), "flags must contain strings"),
        ("remove", (r"\Seen", r"\Seen"), "flags must not contain duplicates"),
        ("add", (r"\Deleted",), "unsupported mutable email flag"),
        ("remove", (r"\Recent",), "unsupported mutable email flag"),
        ("add", ("ProviderKeyword",), "unsupported mutable email flag"),
    ],
)
def test_set_email_flags_command_rejects_unsupported_contract(
    operation: str,
    flags: tuple[object, ...],
    message: str,
) -> None:
    command = SetEmailFlagsCommand(
        "primary",
        ("1",),
        cast(FlagOperation, operation),
        cast(tuple[MutableEmailFlag, ...], flags),
    )

    with pytest.raises(ValueError, match=message):
        command.validate()


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_set_email_flags_command_accepts_supported_contract(operation: FlagOperation) -> None:
    SetEmailFlagsCommand(
        "primary",
        ("1", "2"),
        operation,
        (r"\Seen", r"\Flagged", r"\Answered", r"\Draft"),
        "Archive",
    ).validate()


def test_unknown_outcome_models_always_require_reconciliation() -> None:
    batch = BatchMutationOutcome((TargetMutationOutcome("1", "unknown"),))
    append = AppendMutationOutcome("unknown", "", mailbox="Drafts")
    delivery = SendMutationOutcome(
        (TargetMutationOutcome("alice@example.test", "unknown"),),
        SentCopyMutationOutcome("skipped"),
    )
    sent_copy = SendMutationOutcome(
        (TargetMutationOutcome("alice@example.test", "succeeded"),),
        SentCopyMutationOutcome("unknown", "Sent"),
    )

    assert batch.reconciliation_needed is True
    assert append.reconciliation_needed is True
    assert delivery.reconciliation_needed is True
    assert sent_copy.reconciliation_needed is True


@pytest.mark.asyncio
async def test_mark_read_unknown_is_not_replayed_and_invalidates_projection() -> None:
    provider = MagicMock()
    provider.set_flags = AsyncMock(
        return_value=_batch(
            TargetMutationOutcome("9", "succeeded"),
            TargetMutationOutcome("10", "unknown", "store"),
        )
    )
    services, _, factory, projection = _services(provider=provider)

    result = await services.mark_read.execute(MarkReadCommand("primary", ("9", "10")))

    assert result.targets("succeeded") == ["9"]
    assert result.targets("unknown") == ["10"]
    assert result.reconciliation_needed is True
    provider.set_flags.assert_awaited_once()
    flag_command = provider.set_flags.await_args.args[0]
    assert flag_command.operation == "add"
    assert flag_command.flags == (r"\Seen",)
    factory.open.assert_called_once_with("primary", expected_mode="managed", purpose="incoming")
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
    assert result.reconciliation_needed is True
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
    assert result.reconciliation_needed is True
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
    sent_handler.incoming_client._connect_imap_server = AsyncMock(return_value=imap)

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


@pytest.mark.parametrize(
    "command",
    [
        MarkReadCommand("primary\x7f", ("1",)),
        SendCommand("primary", ("to@example.test",), "subject\x00", "body"),
        SendCommand(
            "primary",
            ("to@example.test",),
            "subject",
            "body",
            in_reply_to="message\x1f",
        ),
        SaveToMailboxCommand(
            "primary",
            ("to@example.test",),
            "subject",
            "body",
            attachments=("attachment\x7f.txt",),
        ),
        SaveToMailboxCommand(
            "primary",
            ("to@example.test",),
            "subject",
            "body",
            flags=("flag\x00",),
        ),
    ],
)
def test_mutation_controlled_fields_reject_c0_and_del(
    command: MarkReadCommand | SendCommand | SaveToMailboxCommand,
) -> None:
    with pytest.raises(ValueError, match="control characters"):
        command.validate()


def test_mutation_subject_uses_utf8_byte_limit() -> None:
    SendCommand(
        "primary",
        ("to@example.test",),
        "é" * (APPLICATION_LIMITS.subject_bytes // 2),
        "body",
    ).validate()

    with pytest.raises(ValueError, match="exceeds"):
        SendCommand(
            "primary",
            ("to@example.test",),
            "é" * (APPLICATION_LIMITS.subject_bytes // 2) + "a",
            "body",
        ).validate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "valid"),
    [("aaa", True), ("éé", True), ("ééa", False)],
)
async def test_mutation_error_detail_limit_uses_utf8_bytes_at_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    detail: str,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        mutations_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, error_detail_bytes=4),
    )
    provider = MagicMock()
    provider.delete = AsyncMock(return_value=_batch(TargetMutationOutcome("1", "failed", detail)))
    services, _, _, _ = _services(provider=provider)

    if valid:
        assert (await services.delete.execute(DeleteCommand("primary", ("1",)))).outcomes[0].detail == detail
    else:
        with pytest.raises(MutationProviderError, match="limit_exceeded"):
            await services.delete.execute(DeleteCommand("primary", ("1",)))


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome_count", "valid"), [(1, True), (2, True), (3, False)])
async def test_mutation_warning_item_limit_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    outcome_count: int,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        mutations_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, warning_items=2),
    )
    provider = MagicMock()
    provider.delete = AsyncMock(
        return_value=_batch(*(TargetMutationOutcome(str(uid), "failed") for uid in range(1, outcome_count + 1)))
    )
    services, _, _, _ = _services(provider=provider)

    if valid:
        assert len((await services.delete.execute(DeleteCommand("primary", ("1",)))).outcomes) == outcome_count
    else:
        with pytest.raises(MutationProviderError, match="limit_exceeded"):
            await services.delete.execute(DeleteCommand("primary", ("1",)))


async def _hang_provider(*_args, **_kwargs):
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["mark_read", "save", "send"])
async def test_mutation_timeout_is_unknown_and_never_replayed(monkeypatch, workflow: str) -> None:
    monkeypatch.setattr(
        mutations_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, provider_timeout_seconds=0.001),
    )
    provider = MagicMock()
    services, _, _, projection = _services(provider=provider)

    if workflow == "mark_read":
        provider.set_flags = AsyncMock(side_effect=_hang_provider)
        result = await services.mark_read.execute(MarkReadCommand("primary", ("7", "8")))
        assert result.targets("unknown") == ["7", "8"]
        assert result.reconciliation_needed is True
        provider.set_flags.assert_awaited_once()
        projection.invalidate.assert_awaited_once_with(("INBOX",))
    elif workflow == "save":
        provider.save_to_mailbox = AsyncMock(side_effect=_hang_provider)
        result = await services.save_to_mailbox.execute(
            SaveToMailboxCommand(
                account_name="primary",
                recipients=("recipient@example.test",),
                subject="Draft",
                body="body",
            )
        )
        assert result.status == "unknown"
        assert result.detail == "provider-timeout"
        assert result.reconciliation_needed is True
        provider.save_to_mailbox.assert_awaited_once()
    else:
        provider.send = AsyncMock(side_effect=_hang_provider)
        result = await services.send.execute(
            SendCommand(
                account_name="primary",
                recipients=("recipient@example.test",),
                subject="Message",
                body="body",
            )
        )
        assert result.recipients("unknown") == ["recipient@example.test"]
        assert result.sent_copy.status == "skipped"
        assert result.reconciliation_needed is True
        provider.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_sent_copy_timeout_preserves_delivery_and_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        mutations_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, provider_timeout_seconds=0.001),
    )
    provider = MagicMock()
    provider.send = AsyncMock(
        return_value=DeliveryMutationOutcome(
            (TargetMutationOutcome("recipient@example.test", "succeeded"),),
            sent_message=object(),
        )
    )
    provider.save_sent_copy = AsyncMock(side_effect=_hang_provider)
    services, _, _, _ = _services(provider=provider)

    result = await services.send.execute(
        SendCommand(
            account_name="primary",
            recipients=("recipient@example.test",),
            subject="Message",
            body="body",
        )
    )

    assert result.recipients("succeeded") == ["recipient@example.test"]
    assert result.sent_copy.status == "unknown"
    assert result.sent_copy.detail == "provider-timeout"
    assert result.reconciliation_needed is True
    provider.send.assert_awaited_once()
    provider.save_sent_copy.assert_awaited_once()


def _forward_source(**changes: object) -> ForwardSource:
    source = ForwardSource(
        subject="Quarterly report",
        sender="author@example.test",
        body_text="---------- Forwarded message ----------\nFrom: author@example.test\n\noriginal body",
        parts=(),
    )
    return replace(source, **changes)


def _part(byte_size: int) -> ForwardSourcePart:
    return ForwardSourcePart(byte_size=byte_size, raw_part=object())


def _forward_command(**changes: object) -> ForwardCommand:
    command = ForwardCommand(
        account_name="primary",
        recipients=("recipient@example.test",),
        subject="",
        body="",
        source_email_id="42",
    )
    return replace(command, **changes)


def _forward_provider(
    *,
    source: ForwardSource | None = None,
    delivery: DeliveryMutationOutcome | None = None,
    sent_copy: SentCopyMutationOutcome | None = None,
) -> MagicMock:
    provider = MagicMock()
    provider.fetch_forward_source = AsyncMock(return_value=source if source is not None else _forward_source())
    provider.forward = AsyncMock(
        return_value=delivery
        if delivery is not None
        else DeliveryMutationOutcome(
            (TargetMutationOutcome("recipient@example.test", "succeeded"),),
            object(),
        )
    )
    provider.save_sent_copy = AsyncMock(
        return_value=sent_copy if sent_copy is not None else SentCopyMutationOutcome("succeeded", "Sent")
    )
    return provider


@pytest.mark.asyncio
async def test_forward_reopens_authority_between_retrieval_delivery_and_sent_copy() -> None:
    provider = _forward_provider()
    services, _, factory, projection = _services(provider=provider)

    result = await services.forward.execute(_forward_command(body="please review"))

    assert result.recipients("succeeded") == ["recipient@example.test"]
    assert result.sent_copy.status == "succeeded"
    assert result.reconciliation_needed is False
    assert factory.open.call_count == 3
    assert [call.kwargs["purpose"] for call in factory.open.call_args_list] == [
        "incoming",
        "outgoing",
        "sent-copy",
    ]
    projection.invalidate.assert_awaited_once_with(("Sent",))


@pytest.mark.asyncio
async def test_forward_derives_subject_and_body_from_provider_source() -> None:
    source = _forward_source()
    provider = _forward_provider(source=source)
    services, _, _, _ = _services(provider=provider)

    await services.forward.execute(_forward_command(body="please review"))

    provider.fetch_forward_source.assert_awaited_once()
    assert provider.fetch_forward_source.await_args.args[0].source_email_id == "42"
    forwarded = provider.forward.await_args.args[0]
    assert forwarded.subject == "Fwd: Quarterly report"
    assert forwarded.body == f"please review\n\n{source.body_text}"
    assert forwarded.source_email_id == "42"
    assert forwarded.source_mailbox == "INBOX"
    # The unmodified provider evidence travels alongside the derived command.
    assert provider.forward.await_args.args[1] is source


@pytest.mark.asyncio
async def test_forward_body_is_the_provider_block_when_no_note_is_supplied() -> None:
    source = _forward_source()
    provider = _forward_provider(source=source)
    services, _, _, _ = _services(provider=provider)

    await services.forward.execute(_forward_command())

    assert provider.forward.await_args.args[0].body == source.body_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original_subject", "expected"),
    [
        ("Quarterly report", "Fwd: Quarterly report"),
        ("Fwd: Quarterly report", "Fwd: Quarterly report"),
        ("fwd: quarterly report", "fwd: quarterly report"),
        ("FWD: Quarterly report", "FWD: Quarterly report"),
        ("FwD:Quarterly report", "FwD:Quarterly report"),
        ("Re: Quarterly report", "Fwd: Re: Quarterly report"),
        ("", "Fwd: "),
    ],
)
async def test_forward_subject_is_never_double_prefixed(original_subject: str, expected: str) -> None:
    provider = _forward_provider(source=_forward_source(subject=original_subject))
    services, _, _, _ = _services(provider=provider)

    await services.forward.execute(_forward_command())

    assert provider.forward.await_args.args[0].subject == expected


@pytest.mark.asyncio
async def test_forward_source_failure_aborts_before_any_delivery() -> None:
    provider = _forward_provider()
    provider.fetch_forward_source = AsyncMock(side_effect=MutationProviderError("provider_failure: fetch failed"))
    services, _, factory, projection = _services(provider=provider)

    with pytest.raises(MutationProviderError, match="fetch failed"):
        await services.forward.execute(_forward_command())

    provider.forward.assert_not_awaited()
    provider.save_sent_copy.assert_not_awaited()
    assert factory.open.call_count == 1
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_source_not_found_aborts_before_any_delivery() -> None:
    provider = _forward_provider()
    provider.fetch_forward_source = AsyncMock(side_effect=ValueError("Email 42 not found in INBOX"))
    services, _, _, _ = _services(provider=provider)

    with pytest.raises(ValueError, match="not found"):
        await services.forward.execute(_forward_command())

    provider.forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_source_timeout_raises_instead_of_producing_an_outcome(monkeypatch) -> None:
    monkeypatch.setattr(
        mutations_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, provider_timeout_seconds=0.001),
    )
    provider = _forward_provider()
    provider.fetch_forward_source = AsyncMock(side_effect=_hang_provider)
    services, _, _, projection = _services(provider=provider)

    with pytest.raises(MutationProviderError, match="forward source retrieval timed out"):
        await services.forward.execute(_forward_command())

    provider.forward.assert_not_awaited()
    provider.save_sent_copy.assert_not_awaited()
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parts", "message"),
    [
        (
            tuple(_part(1) for index in range(APPLICATION_LIMITS.attachments + 1)),
            "at most",
        ),
        ((_part(APPLICATION_LIMITS.attachment_bytes + 1),), "a forwarded part exceeds"),
        (
            (
                _part(APPLICATION_LIMITS.attachment_bytes),
                _part(APPLICATION_LIMITS.attachment_bytes),
                _part(1),
            ),
            "bytes in total",
        ),
        ((_part(-1),), "non-negative integer"),
    ],
)
async def test_forward_rejects_out_of_bound_parts_before_any_delivery(
    parts: tuple[ForwardSourcePart, ...],
    message: str,
) -> None:
    provider = _forward_provider(source=_forward_source(parts=parts))
    services, _, factory, _ = _services(provider=provider)

    with pytest.raises(ValueError, match=message):
        await services.forward.execute(_forward_command())

    provider.forward.assert_not_awaited()
    assert factory.open.call_count == 1


@pytest.mark.asyncio
async def test_forward_accepts_parts_at_the_aggregate_boundary() -> None:
    parts = (
        _part(APPLICATION_LIMITS.attachment_bytes),
        _part(APPLICATION_LIMITS.total_attachment_bytes - APPLICATION_LIMITS.attachment_bytes),
    )
    provider = _forward_provider(source=_forward_source(parts=parts))
    services, _, _, _ = _services(provider=provider)

    result = await services.forward.execute(_forward_command())

    assert result.recipients("succeeded") == ["recipient@example.test"]
    provider.forward.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_changes", "message"),
    [
        ({"subject": "Quarterly\r\nBcc: other@example.test"}, "control characters"),
        ({"subject": "é" * (APPLICATION_LIMITS.subject_bytes // 2)}, "exceeds"),
        ({"body_text": "a" * (APPLICATION_LIMITS.body_bytes + 1)}, "body exceeds"),
    ],
)
async def test_forward_rejects_out_of_bound_derived_content_before_any_delivery(
    source_changes: dict[str, object],
    message: str,
) -> None:
    provider = _forward_provider(source=_forward_source(**source_changes))
    services, _, _, _ = _services(provider=provider)

    with pytest.raises(ValueError, match=message):
        await services.forward.execute(_forward_command())

    provider.forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_note_pushing_body_over_the_limit_fails_before_any_delivery() -> None:
    # The source alone is within the ceiling; only the derived note + block exceeds it.
    provider = _forward_provider(source=_forward_source(body_text="a" * APPLICATION_LIMITS.body_bytes))
    services, _, _, _ = _services(provider=provider)

    with pytest.raises(ValueError, match="body exceeds"):
        await services.forward.execute(_forward_command(body="note"))

    provider.forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_rejects_invalid_source_uid_before_provider_access() -> None:
    provider = _forward_provider()
    services, _, factory, _ = _services(provider=provider)

    with pytest.raises(ValueError, match="canonical positive decimal IMAP UID"):
        await services.forward.execute(_forward_command(source_email_id="0"))

    factory.open.assert_not_called()
    provider.fetch_forward_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_rejects_source_mailbox_control_characters_before_provider_access() -> None:
    provider = _forward_provider()
    services, _, factory, _ = _services(provider=provider)

    with pytest.raises(ValueError, match="control characters"):
        await services.forward.execute(_forward_command(source_mailbox="INBOX\r\nEXPUNGE"))

    factory.open.assert_not_called()
    provider.fetch_forward_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_recipient_policy_denial_fails_before_provider_access() -> None:
    provider = _forward_provider()
    services, _, factory, _ = _services(
        account=_account(allowed_recipients=("allowed@example.test",)),
        provider=provider,
    )

    with pytest.raises(RecipientPolicyDeniedError):
        await services.forward.execute(_forward_command(recipients=("blocked@example.test",)))

    factory.open.assert_not_called()
    provider.fetch_forward_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_recipient_policy_denial_after_open_fails_before_retrieval() -> None:
    provider = _forward_provider()
    services, _, factory, _ = _services(provider=provider)
    factory.open.return_value = MutationProviderAccess(
        _account(allowed_recipients=("allowed@example.test",)),
        provider,
    )

    with pytest.raises(RecipientPolicyDeniedError):
        await services.forward.execute(_forward_command(recipients=("blocked@example.test",)))

    assert factory.open.call_count == 1
    provider.fetch_forward_source.assert_not_awaited()
    provider.forward.assert_not_awaited()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"subject": "URGENT: contract"}, "forward subject is derived"),
        ({"html": True}, "composed as plain text"),
        ({"attachments": ("extra.pdf",)}, "does not accept caller attachments"),
    ],
)
def test_forward_command_rejects_unsupported_compose_input(changes: dict[str, object], message: str) -> None:
    """Locked fields fail loudly instead of being silently overwritten or mishandled."""
    with pytest.raises(ValueError, match=message):
        _forward_command(**changes).validate()


@pytest.mark.asyncio
async def test_send_incapable_account_is_rejected_before_the_outgoing_open() -> None:
    provider = MagicMock()
    provider.send = AsyncMock()
    services, _, factory, _ = _services(account=_account(can_send=False), provider=provider)

    with pytest.raises(MutationProviderError, match="SMTP is not configured"):
        await services.send.execute(SendCommand("primary", ("a@example.test",), "s", "b"))

    factory.open.assert_not_called()
    provider.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_send_capability_loss_on_the_outgoing_open_fails_before_delivery() -> None:
    provider = _forward_provider()
    services, _, factory, _ = _services(provider=provider)
    factory.open.side_effect = [
        factory.open.return_value,
        MutationProviderAccess(_account(can_send=False), provider),
    ]

    with pytest.raises(MutationProviderError, match="SMTP is not configured"):
        await services.forward.execute(_forward_command())

    provider.fetch_forward_source.assert_awaited_once()
    provider.forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_read_snapshot_sender_denial_aborts_before_outgoing_open() -> None:
    provider = _forward_provider(source=_forward_source(sender="author@example.test"))
    services, _, factory, _ = _services(provider=provider)
    factory.open.return_value = MutationProviderAccess(
        _account(allowed_senders=("other@example.test",)),
        provider,
    )

    with pytest.raises(ValueError, match=r"^Failed to fetch email with UID 42$"):
        await services.forward.execute(_forward_command())

    provider.fetch_forward_source.assert_awaited_once()
    provider.forward.assert_not_awaited()
    provider.save_sent_copy.assert_not_awaited()
    assert factory.open.call_count == 1


@pytest.mark.asyncio
async def test_forward_sender_denial_precedes_concurrent_capability_and_recipient_denials() -> None:
    provider = _forward_provider(source=_forward_source(sender="author@example.test"))
    services, _, factory, _ = _services(provider=provider)
    factory.open.side_effect = [
        MutationProviderAccess(_account(allowed_senders=("*@example.test",)), provider),
        MutationProviderAccess(
            _account(
                allowed_senders=("other@example.test",),
                allowed_recipients=("other@example.test",),
                can_send=False,
            ),
            provider,
        ),
    ]

    with pytest.raises(ValueError, match=r"^Failed to fetch email with UID 42$"):
        await services.forward.execute(_forward_command())

    provider.fetch_forward_source.assert_awaited_once()
    provider.forward.assert_not_awaited()
    provider.save_sent_copy.assert_not_awaited()
    assert factory.open.call_count == 2


@pytest.mark.asyncio
async def test_forward_send_incapable_account_performs_no_provider_access() -> None:
    # An IMAP-only account must be refused before the source message is logged
    # into, downloaded, or parsed — not after a full source read.
    provider = _forward_provider()
    services, _, factory, _ = _services(account=_account(can_send=False), provider=provider)

    with pytest.raises(MutationProviderError, match="SMTP is not configured"):
        await services.forward.execute(_forward_command())

    factory.open.assert_not_called()
    provider.fetch_forward_source.assert_not_awaited()
    provider.forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_send_capability_loss_after_open_fails_before_retrieval() -> None:
    provider = _forward_provider()
    services, _, factory, _ = _services(provider=provider)
    factory.open.return_value = MutationProviderAccess(_account(can_send=False), provider)

    with pytest.raises(MutationProviderError, match="SMTP is not configured"):
        await services.forward.execute(_forward_command())

    assert factory.open.call_count == 1
    provider.fetch_forward_source.assert_not_awaited()
    provider.forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_recipient_policy_denial_before_delivery_fails_after_retrieval() -> None:
    provider = _forward_provider()
    services, _, factory, _ = _services(provider=provider)
    factory.open.side_effect = [
        factory.open.return_value,
        MutationProviderAccess(_account(allowed_recipients=("allowed@example.test",)), provider),
    ]

    with pytest.raises(RecipientPolicyDeniedError):
        await services.forward.execute(_forward_command(recipients=("blocked@example.test",)))

    provider.fetch_forward_source.assert_awaited_once()
    provider.forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_delivery_timeout_is_unknown_and_never_replayed(monkeypatch) -> None:
    monkeypatch.setattr(
        mutations_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, provider_timeout_seconds=0.001),
    )
    provider = _forward_provider()
    provider.forward = AsyncMock(side_effect=_hang_provider)
    services, _, _, projection = _services(provider=provider)

    result = await services.forward.execute(
        _forward_command(recipients=("recipient@example.test",), cc=("copied@example.test",))
    )

    assert result.recipients("unknown") == ["recipient@example.test", "copied@example.test"]
    assert result.sent_copy.status == "skipped"
    assert result.reconciliation_needed is True
    provider.forward.assert_awaited_once()
    provider.save_sent_copy.assert_not_awaited()
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_preserves_partial_delivery_and_skips_sent_copy_without_evidence() -> None:
    provider = _forward_provider(
        delivery=DeliveryMutationOutcome(
            (
                TargetMutationOutcome("accepted@example.test", "succeeded"),
                TargetMutationOutcome("rejected@example.test", "failed", "smtp-recipient-rejected"),
            ),
            None,
        )
    )
    services, _, factory, _ = _services(provider=provider)

    result = await services.forward.execute(
        _forward_command(recipients=("accepted@example.test", "rejected@example.test"))
    )

    assert result.recipients("succeeded") == ["accepted@example.test"]
    assert result.recipients("failed") == ["rejected@example.test"]
    assert result.sent_copy.status == "skipped"
    assert result.reconciliation_needed is False
    provider.save_sent_copy.assert_not_awaited()
    assert factory.open.call_count == 2


@pytest.mark.asyncio
async def test_forward_sent_copy_failure_does_not_downgrade_delivery() -> None:
    provider = _forward_provider()
    provider.save_sent_copy = AsyncMock(side_effect=MutationProviderError("provider_failure: append failed"))
    services, _, _, projection = _services(provider=provider)

    result = await services.forward.execute(_forward_command())

    assert result.recipients("succeeded") == ["recipient@example.test"]
    assert result.sent_copy.status == "failed"
    assert result.sent_copy.detail == "sent-copy-unavailable"
    assert result.reconciliation_needed is False
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_sent_copy_timeout_preserves_delivery_and_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        mutations_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, provider_timeout_seconds=0.001),
    )
    provider = _forward_provider()
    provider.save_sent_copy = AsyncMock(side_effect=_hang_provider)
    services, _, _, _ = _services(provider=provider)

    result = await services.forward.execute(_forward_command())

    assert result.recipients("succeeded") == ["recipient@example.test"]
    assert result.sent_copy.status == "unknown"
    assert result.sent_copy.detail == "provider-timeout"
    assert result.reconciliation_needed is True


@pytest.mark.asyncio
async def test_forward_sends_bcc_only_to_the_sent_copy() -> None:
    provider = _forward_provider()
    services, _, _, _ = _services(provider=provider)

    await services.forward.execute(_forward_command(bcc=("secret@example.test",)))

    provider.save_sent_copy.assert_awaited_once()
    assert provider.save_sent_copy.await_args.args[1] == ("secret@example.test",)


@pytest.mark.asyncio
async def test_forward_threads_include_attachments_to_the_provider() -> None:
    provider = _forward_provider(source=_forward_source(parts=(_part(1024),)))
    services, _, _, _ = _services(provider=provider)

    await services.forward.execute(_forward_command(include_attachments=False))

    assert provider.fetch_forward_source.await_args.args[0].include_attachments is False
    assert provider.forward.await_args.args[0].include_attachments is False
