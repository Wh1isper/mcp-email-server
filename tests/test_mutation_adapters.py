import asyncio
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    MarkReadCommand,
    MutationAccountSnapshot,
    MutationProviderError,
    SaveToMailboxCommand,
    SendCommand,
    SentCopyMutationOutcome,
)
from mcp_email_server.config import EmailSettings


def _mark_read_provider(error: BaseException) -> ClassicMutationProvider:
    handler = MagicMock()
    handler.incoming_client.mark_emails_as_read_with_outcome = AsyncMock(side_effect=error)
    return ClassicMutationProvider(handler)


def _account() -> MutationAccountSnapshot:
    return MutationAccountSnapshot("primary", "managed", (), (), False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        asyncio.CancelledError(),
        ValueError("invalid mutation"),
        PermissionError("mutation denied"),
    ],
)
async def test_mutation_adapter_preserves_control_and_policy_exceptions(error: BaseException) -> None:
    provider = _mark_read_provider(error)

    with pytest.raises(type(error)) as caught:
        await provider.mark_read(MarkReadCommand("primary", ("1",)), _account())

    assert caught.value is error


@pytest.mark.asyncio
async def test_mutation_adapter_sanitizes_unexpected_provider_failure() -> None:
    provider_detail = "provider-controlled secret detail"
    provider = _mark_read_provider(RuntimeError(provider_detail))

    with pytest.raises(
        MutationProviderError,
        match=r"^provider_failure: mutation provider request failed$",
    ) as caught:
        await provider.mark_read(MarkReadCommand("primary", ("1",)), _account())

    assert provider_detail not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("flags", "expected_flags"), [(None, r"(\Draft \Seen)"), ((r"\Flagged",), r"(\Flagged)")])
async def test_mutation_adapter_composes_and_appends_mailbox_message(
    email_settings: EmailSettings,
    flags: tuple[str, ...] | None,
    expected_flags: str,
) -> None:
    message = MIMEText("body")
    outcome = AppendMutationOutcome("succeeded", "message-id", mailbox="Drafts")
    handler = MagicMock()
    handler.email_settings = email_settings
    handler.incoming_client.compose_message = Mock(return_value=message)
    handler.incoming_client.append_to_mailbox_with_outcome = AsyncMock(return_value=outcome)
    provider = ClassicMutationProvider(handler)
    command = SaveToMailboxCommand(
        "primary",
        ("recipient@example.test",),
        "Subject",
        "Body",
        mailbox="Drafts",
        flags=flags,
    )

    assert await provider.save_to_mailbox(command, _account()) is outcome
    handler.incoming_client.compose_message.assert_called_once_with(
        ["recipient@example.test"],
        "Subject",
        "Body",
        None,
        None,
        False,
        None,
        None,
        None,
        include_bcc_header=True,
    )
    handler.incoming_client.append_to_mailbox_with_outcome.assert_awaited_once_with(
        message,
        email_settings.incoming,
        "Drafts",
        expected_flags,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_mailbox", [None, "INBOX"])
async def test_mutation_adapter_requires_distinct_archive_mailbox(archive_mailbox: str | None) -> None:
    handler = MagicMock()
    handler._find_archive_folder = AsyncMock(return_value=archive_mailbox)

    with pytest.raises(ValueError, match="No distinct Archive folder found"):
        await ClassicMutationProvider(handler).find_archive_mailbox("INBOX")


@pytest.mark.asyncio
async def test_mutation_adapter_returns_discovered_archive_mailbox() -> None:
    handler = MagicMock()
    handler._find_archive_folder = AsyncMock(return_value="Archive")

    assert await ClassicMutationProvider(handler).find_archive_mailbox("INBOX") == "Archive"


@pytest.mark.asyncio
async def test_mutation_adapter_rejects_send_without_smtp() -> None:
    handler = MagicMock()
    handler.outgoing_client = None
    command = SendCommand("primary", ("recipient@example.test",), "Subject", "Body")

    with pytest.raises(MutationProviderError, match="SMTP is not configured"):
        await ClassicMutationProvider(handler).send(command, _account())


@pytest.mark.asyncio
async def test_mutation_adapter_skips_disabled_sent_copy() -> None:
    handler = MagicMock()
    handler.save_to_sent = False

    assert await ClassicMutationProvider(handler).save_sent_copy(object(), ()) == SentCopyMutationOutcome("skipped")
    handler.incoming_client.append_to_sent_with_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_mutation_adapter_rejects_invalid_sent_message_evidence() -> None:
    handler = MagicMock()
    handler.save_to_sent = True

    with pytest.raises(MutationProviderError, match="sent message evidence is invalid"):
        await ClassicMutationProvider(handler).save_sent_copy(object(), ())


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_bcc", [None, "existing@example.test"])
async def test_mutation_adapter_appends_local_sent_copy_without_overwriting_bcc(
    email_settings: EmailSettings,
    existing_bcc: str | None,
) -> None:
    message = MIMEText("body")
    if existing_bcc is not None:
        message["Bcc"] = existing_bcc
    outcome = SentCopyMutationOutcome("succeeded", "Sent", "append")
    handler = MagicMock()
    handler.save_to_sent = True
    handler.email_settings = email_settings
    handler.sent_folder_name = "Sent"
    handler.incoming_client.append_to_sent_with_outcome = AsyncMock(return_value=outcome)

    result = await ClassicMutationProvider(handler).save_sent_copy(message, ("hidden@example.test",))

    assert result is outcome
    assert message["Bcc"] == (existing_bcc or "hidden@example.test")
    handler.incoming_client.append_to_sent_with_outcome.assert_awaited_once_with(
        message,
        email_settings.incoming,
        "Sent",
    )
