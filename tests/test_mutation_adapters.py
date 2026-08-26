import asyncio
from dataclasses import replace
from email import encoders
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.policy import SMTP as SMTP_POLICY
from email.policy import SMTPUTF8 as SMTPUTF8_POLICY
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    ForwardCommand,
    ForwardSource,
    ForwardSourcePart,
    MutationAccountSnapshot,
    MutationProviderError,
    SaveToMailboxCommand,
    SendCommand,
    SentCopyMutationOutcome,
    SetEmailFlagsCommand,
)
from mcp_email_server.config import EmailSettings


def _set_flags_provider(error: BaseException) -> ClassicMutationProvider:
    handler = MagicMock()
    handler.incoming_client.set_email_flags_with_outcome = AsyncMock(side_effect=error)
    return ClassicMutationProvider(handler)


def _account() -> MutationAccountSnapshot:
    return MutationAccountSnapshot("primary", "managed", (), (), False, can_send=True)


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
    provider = _set_flags_provider(error)

    with pytest.raises(type(error)) as caught:
        await provider.set_flags(SetEmailFlagsCommand("primary", ("1",), "add", (r"\Seen",)), _account())

    assert caught.value is error


@pytest.mark.asyncio
async def test_mutation_adapter_sanitizes_unexpected_provider_failure() -> None:
    provider_detail = "provider-controlled secret detail"
    provider = _set_flags_provider(RuntimeError(provider_detail))

    with pytest.raises(
        MutationProviderError,
        match=r"^provider_failure: mutation provider request failed$",
    ) as caught:
        await provider.set_flags(SetEmailFlagsCommand("primary", ("1",), "add", (r"\Seen",)), _account())

    assert provider_detail not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_mutation_adapter_forwards_generic_flag_contract_and_policy() -> None:
    outcome = MagicMock()
    handler = MagicMock()
    handler.incoming_client.set_email_flags_with_outcome = AsyncMock(return_value=outcome)
    provider = ClassicMutationProvider(handler)
    account = MutationAccountSnapshot("primary", "managed", ("*@allowed.test",), (), True, can_send=True)
    command = SetEmailFlagsCommand("primary", ("1", "2"), "remove", (r"\Seen", r"\Flagged"), "Archive")

    assert await provider.set_flags(command, account) is outcome
    handler.incoming_client.set_email_flags_with_outcome.assert_awaited_once_with(
        ["1", "2"],
        "remove",
        [r"\Seen", r"\Flagged"],
        "Archive",
        ["*@allowed.test"],
        True,
    )


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


def _forward_part(payload: bytes = b"report bytes", filename: str = "report.pdf") -> Message:
    part = MIMEBase("application", "pdf", name=filename)
    part.set_payload(payload)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    return part


def _forward_wire_size(part: Message) -> int:
    return max(len(part.as_bytes(policy=SMTP_POLICY)), len(part.as_bytes(policy=SMTPUTF8_POLICY)))


def _forward_source_payload(parts: list[Message] | None = None) -> dict[str, object]:
    return {
        "subject": "Quarterly report",
        "from": "author@example.test",
        "recipients": ["team@example.test", "cc@example.test"],
        "date": "Mon, 01 Jul 2026 09:00:00 +0000",
        "body": "---------- Forwarded message ----------\nFrom: author@example.test\n\noriginal body",
        "parts": [] if parts is None else parts,
    }


def _forward_command(**changes: object) -> ForwardCommand:
    command = ForwardCommand(
        "primary",
        ("recipient@example.test",),
        "Fwd: Quarterly report",
        "note\n\nforwarded block",
        source_email_id="42",
        source_mailbox="Archive",
    )
    return replace(command, **changes)  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_mutation_adapter_threads_allowlist_and_attachment_choice_into_forward_source() -> None:
    part = _forward_part()
    handler = MagicMock()
    handler.incoming_client.fetch_forward_source = AsyncMock(return_value=_forward_source_payload([part]))
    account = MutationAccountSnapshot("primary", "managed", ("*@allowed.test",), (), False, can_send=True)

    source = await ClassicMutationProvider(handler).fetch_forward_source(
        _forward_command(include_attachments=False), account
    )

    handler.incoming_client.fetch_forward_source.assert_awaited_once_with(
        "42",
        "Archive",
        ["*@allowed.test"],
        False,
    )
    assert source.subject == "Quarterly report"
    assert source.sender == "author@example.test"
    assert source.body_text.endswith("original body")
    # The provider decides what to retain; the adapter reports exactly what it returned.
    assert [item.byte_size for item in source.parts] == [_forward_wire_size(part)]
    assert source.parts[0].raw_part is part


@pytest.mark.asyncio
async def test_mutation_adapter_reports_serialized_forward_part_size() -> None:
    """The bounded size is what SMTP carries, not the decoded payload length."""
    payload = b"x" * 4096
    part = _forward_part(payload)
    handler = MagicMock()
    handler.incoming_client.fetch_forward_source = AsyncMock(return_value=_forward_source_payload([part]))

    source = await ClassicMutationProvider(handler).fetch_forward_source(_forward_command(), _account())

    assert source.parts[0].byte_size == _forward_wire_size(part)
    assert source.parts[0].byte_size > len(payload)


@pytest.mark.asyncio
async def test_mutation_adapter_counts_smtp_crlf_expansion_in_forward_part_size() -> None:
    part = MIMEText("x\n" * 1000, "plain", "us-ascii")
    part.add_header("Content-Disposition", "attachment", filename="lines.txt")
    handler = MagicMock()
    handler.incoming_client.fetch_forward_source = AsyncMock(return_value=_forward_source_payload([part]))

    source = await ClassicMutationProvider(handler).fetch_forward_source(_forward_command(), _account())

    assert len(part.as_bytes()) < len(part.as_bytes(policy=SMTP_POLICY))
    assert source.parts[0].byte_size == _forward_wire_size(part)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        asyncio.CancelledError(),
        ValueError("Email 42 not found in Archive"),
        PermissionError("read denied"),
    ],
)
async def test_mutation_adapter_preserves_forward_source_sentinel_exceptions(error: BaseException) -> None:
    handler = MagicMock()
    handler.incoming_client.fetch_forward_source = AsyncMock(side_effect=error)

    with pytest.raises(type(error)) as caught:
        await ClassicMutationProvider(handler).fetch_forward_source(_forward_command(), _account())

    assert caught.value is error


@pytest.mark.asyncio
async def test_mutation_adapter_sanitizes_unexpected_forward_source_failure() -> None:
    provider_detail = "IMAP FETCH failed: server-controlled secret detail"
    handler = MagicMock()
    handler.incoming_client.fetch_forward_source = AsyncMock(side_effect=RuntimeError(provider_detail))

    with pytest.raises(
        MutationProviderError,
        match=r"^provider_failure: mutation provider request failed$",
    ) as caught:
        await ClassicMutationProvider(handler).fetch_forward_source(_forward_command(), _account())

    assert provider_detail not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_mutation_adapter_sanitizes_unserializable_forward_part() -> None:
    """A part that cannot be measured must not escape as a raw provider exception."""
    broken = MagicMock(spec=Message)
    broken.get_content_type.return_value = "application/pdf"
    broken.get_filename.return_value = "report.pdf"
    broken.as_bytes.side_effect = RuntimeError("flatten failed")
    handler = MagicMock()
    handler.incoming_client.fetch_forward_source = AsyncMock(return_value=_forward_source_payload([broken]))

    with pytest.raises(MutationProviderError, match=r"^provider_failure: mutation provider request failed$"):
        await ClassicMutationProvider(handler).fetch_forward_source(_forward_command(), _account())


@pytest.mark.asyncio
async def test_mutation_adapter_rejects_forward_without_smtp() -> None:
    handler = MagicMock()
    handler.outgoing_client = None

    with pytest.raises(MutationProviderError, match="capability_unavailable: SMTP is not configured"):
        await ClassicMutationProvider(handler).forward(
            _forward_command(), ForwardSource("s", "author@example.test", "block", ()), _account()
        )


@pytest.mark.asyncio
async def test_mutation_adapter_submits_forward_body_verbatim_with_reattached_parts() -> None:
    part = _forward_part()
    source = ForwardSource(
        "Quarterly report",
        "author@example.test",
        "---------- Forwarded message ----------\n\noriginal body",
        (ForwardSourcePart(_forward_wire_size(part), part),),
    )
    outcome = MagicMock()
    handler = MagicMock()
    handler.outgoing_client.send_email_with_outcome = AsyncMock(return_value=outcome)
    command = _forward_command(cc=("cc@example.test",))

    assert await ClassicMutationProvider(handler).forward(command, source, _account()) is outcome
    handler.outgoing_client.send_email_with_outcome.assert_awaited_once_with(
        ["recipient@example.test"],
        # The application already derived the subject and prefixed the note; neither is rebuilt here.
        "Fwd: Quarterly report",
        "note\n\nforwarded block",
        ["cc@example.test"],
        None,
        False,
        None,
        None,
        None,
        None,
        extra_parts=[part],
    )


@pytest.mark.asyncio
async def test_mutation_adapter_rejects_invalid_forward_part_evidence() -> None:
    handler = MagicMock()
    source = ForwardSource(
        "Quarterly report",
        "author@example.test",
        "block",
        (ForwardSourcePart(10, object()),),
    )

    with pytest.raises(MutationProviderError, match="forwarded part evidence is invalid"):
        await ClassicMutationProvider(handler).forward(_forward_command(), source, _account())
    handler.outgoing_client.send_email_with_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_mutation_adapter_sanitizes_unexpected_forward_delivery_failure() -> None:
    provider_detail = "SMTP 550 server-controlled secret detail"
    handler = MagicMock()
    handler.outgoing_client.send_email_with_outcome = AsyncMock(side_effect=RuntimeError(provider_detail))

    with pytest.raises(
        MutationProviderError,
        match=r"^provider_failure: mutation provider request failed$",
    ) as caught:
        await ClassicMutationProvider(handler).forward(
            _forward_command(), ForwardSource("s", "author@example.test", "block", ()), _account()
        )

    assert provider_detail not in str(caught.value)
