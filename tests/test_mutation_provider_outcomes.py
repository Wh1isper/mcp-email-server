from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosmtplib.errors import SMTPRecipientRefused, SMTPResponseException

from mcp_email_server.application.mutations import FlagOperation, MutableEmailFlag
from mcp_email_server.emails.classic import EmailClient


def _imap(*, capabilities: tuple[str, ...] = ("IMAP4rev1", "UIDPLUS")) -> AsyncMock:
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.id.return_value = MagicMock(result="OK")
    imap.select.return_value = ("OK", [])
    imap.uid.return_value = ("OK", [])
    imap.append.return_value = ("OK", [])
    imap.list.return_value = ("OK", [])
    imap.logout.return_value = ("BYE", [])
    imap.protocol = SimpleNamespace(capabilities=capabilities, capability=AsyncMock())
    return imap


@pytest.mark.asyncio
async def test_mark_read_transport_loss_is_unknown(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.side_effect = ConnectionError("lost after write")
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.mark_emails_as_read_with_outcome(["8"], allowed_senders=[])

    assert result.outcomes[0].status == "unknown"
    assert result.outcomes[0].detail == "store-unknown"
    assert imap.uid.await_count == 1


@pytest.mark.asyncio
async def test_mark_read_explicit_rejection_is_failed(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.return_value = ("NO", [b"provider detail must not escape"])
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.mark_emails_as_read_with_outcome(["8"], allowed_senders=[])

    assert result.outcomes[0].status == "failed"
    assert result.outcomes[0].detail == "store-rejected"


@pytest.mark.asyncio
async def test_mark_read_disconnect_response_is_unknown(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.return_value = ("BYE", [])
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.mark_emails_as_read_with_outcome(["8"], allowed_senders=[])

    assert (result.outcomes[0].status, result.outcomes[0].detail) == ("unknown", "store-unknown")


@pytest.mark.asyncio
async def test_set_email_flags_removes_multiple_flags_with_silent_store(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.set_email_flags_with_outcome(
            ["8", "9"],
            "remove",
            [r"\Seen", r"\Flagged"],
            mailbox="Archive",
            allowed_senders=[],
        )

    assert result.targets("succeeded") == ["8", "9"]
    assert [call.args for call in imap.uid.await_args_list] == [
        ("store", "8", "-FLAGS.SILENT", r"(\Seen \Flagged)"),
        ("store", "9", "-FLAGS.SILENT", r"(\Seen \Flagged)"),
    ]
    imap.select.assert_awaited_once_with('"Archive"')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "flags", "message"),
    [
        ("replace", [r"\Seen"], "operation must be"),
        ("add", [], "flags must not be empty"),
        (
            "add",
            [r"\Seen", r"\Flagged", r"\Answered", r"\Draft", r"\Seen"],
            "flags must contain at most",
        ),
        ("remove", [1], "flags must contain strings"),
        ("remove", [r"\Seen", r"\Seen"], "flags must not contain duplicates"),
        ("add", [r"\Deleted"], "unsupported mutable email flag"),
        ("remove", [r"\Recent"], "unsupported mutable email flag"),
        ("add", ["ProviderKeyword"], "unsupported mutable email flag"),
    ],
)
async def test_set_email_flags_rejects_invalid_contract_before_connect(
    email_server,
    operation: str,
    flags: list[object],
    message: str,
) -> None:
    client = EmailClient(email_server)
    connect = AsyncMock()

    with (
        patch.object(client, "_connect_imap", connect),
        pytest.raises(ValueError, match=message),
    ):
        await client.set_email_flags_with_outcome(
            ["8"],
            cast(FlagOperation, operation),
            cast(list[MutableEmailFlag], flags),
        )

    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_store_disconnect_response_is_unknown_and_stops_before_expunge(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.return_value = ("BYE", [])
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.delete_emails_with_outcome(["8"], allowed_senders=[])

    assert (result.outcomes[0].status, result.outcomes[0].detail) == ("unknown", "store-unknown")
    assert [call.args[0] for call in imap.uid.await_args_list] == ["store"]


@pytest.mark.asyncio
async def test_delete_expunge_rejection_is_unknown_after_store(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()

    async def uid(command: str, *_args: str):
        return ("NO", []) if command == "expunge" else ("OK", [])

    imap.uid.side_effect = uid
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.delete_emails_with_outcome(["8"], allowed_senders=[])

    assert result.outcomes[0].status == "unknown"
    assert result.outcomes[0].detail == "expunge-rejected"
    assert [call.args[0] for call in imap.uid.await_args_list] == ["store", "expunge"]


@pytest.mark.asyncio
async def test_delete_expunge_disconnect_response_is_unknown_not_rejected(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()

    async def uid(command: str, *_args: str):
        return ("BYE", []) if command == "expunge" else ("OK", [])

    imap.uid.side_effect = uid
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.delete_emails_with_outcome(["8"], allowed_senders=[])

    assert (result.outcomes[0].status, result.outcomes[0].detail) == ("unknown", "expunge-unknown")


@pytest.mark.asyncio
async def test_delete_without_uidplus_fails_before_store(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap(capabilities=("IMAP4rev1",))
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.delete_emails_with_outcome(["8"], allowed_senders=[])

    assert result.outcomes[0].status == "failed"
    assert result.outcomes[0].detail == "uidplus-unavailable"
    imap.uid.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_fallback_store_rejection_preserves_partial_fact_as_unknown(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()

    async def uid(command: str, *_args: str):
        return ("NO", []) if command == "store" else ("OK", [])

    imap.uid.side_effect = uid
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["8"], "INBOX", "Archive", allowed_senders=[])

    assert result.outcomes[0].status == "unknown"
    assert result.outcomes[0].detail == "copy-succeeded-store-failed"
    assert [call.args[0] for call in imap.uid.await_args_list] == ["copy", "store"]


@pytest.mark.asyncio
async def test_native_move_transport_loss_is_unknown(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap(capabilities=("IMAP4rev1", "UIDPLUS", "MOVE"))
    imap.uid.side_effect = ConnectionError("result lost")
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["8"], "INBOX", "Archive", allowed_senders=[])

    assert result.outcomes[0].status == "unknown"
    assert result.outcomes[0].detail == "move-unknown"
    assert imap.uid.await_count == 1


@pytest.mark.asyncio
async def test_native_move_disconnect_response_is_unknown(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap(capabilities=("IMAP4rev1", "UIDPLUS", "MOVE"))
    imap.uid.return_value = ("BYE", [])
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["8"], "INBOX", "Archive", allowed_senders=[])

    assert (result.outcomes[0].status, result.outcomes[0].detail) == ("unknown", "move-unknown")


@pytest.mark.asyncio
async def test_move_fallback_copy_disconnect_response_is_unknown_and_stops_uid(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.return_value = ("BYE", [])
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["8"], "INBOX", "Archive", allowed_senders=[])

    assert (result.outcomes[0].status, result.outcomes[0].detail) == ("unknown", "copy-unknown")
    assert [call.args[0] for call in imap.uid.await_args_list] == ["copy"]


@pytest.mark.asyncio
async def test_move_fallback_store_disconnect_preserves_copy_as_unknown(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.side_effect = [("OK", []), ("BYE", [])]
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["8"], "INBOX", "Archive", allowed_senders=[])

    assert (result.outcomes[0].status, result.outcomes[0].detail) == (
        "unknown",
        "copy-succeeded-store-unknown",
    )
    assert [call.args[0] for call in imap.uid.await_args_list] == ["copy", "store"]


@pytest.mark.asyncio
async def test_append_transport_loss_is_unknown_and_not_replayed(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Draft", "body")
    imap = _imap()
    imap.append.side_effect = ConnectionError("result lost")
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        result = await client.append_to_mailbox_with_outcome(message, email_server, "Drafts")

    assert result.status == "unknown"
    assert result.message_id == message["Message-Id"]
    assert result.mailbox == "Drafts"
    assert imap.append.await_count == 1


@pytest.mark.asyncio
async def test_append_disconnect_response_is_unknown_not_retriable_failure(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Draft", "body")
    imap = _imap()
    imap.append.return_value = ("BYE", [])
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        result = await client.append_to_mailbox_with_outcome(message, email_server, "Drafts")

    assert (result.status, result.detail) == ("unknown", "append-unknown")


@pytest.mark.asyncio
async def test_append_success_without_appenduid_does_not_invent_uid(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Draft", "body")
    imap = _imap()
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        result = await client.append_to_mailbox_with_outcome(message, email_server, "Drafts")

    assert result.status == "succeeded"
    assert result.uid is None


@pytest.mark.asyncio
async def test_sent_copy_transport_loss_stops_after_one_append(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Sent", "body")
    imap = _imap()
    imap.append.side_effect = ConnectionError("result lost")
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        result = await client.append_to_sent_with_outcome(message, email_server, "Sent")

    assert result.status == "unknown"
    assert result.mailbox == "Sent"
    assert imap.append.await_count == 1
    assert imap.select.await_count == 1


@pytest.mark.asyncio
async def test_sent_copy_disconnect_response_is_unknown_and_not_replayed(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Sent", "body")
    imap = _imap()
    imap.append.return_value = ("BYE", [])
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        result = await client.append_to_sent_with_outcome(message, email_server, "Sent")

    assert (result.status, result.mailbox, result.detail) == ("unknown", "Sent", "append-unknown")
    assert imap.append.await_count == 1


def _smtp() -> AsyncMock:
    smtp = AsyncMock()
    smtp.__aenter__.return_value = smtp
    smtp.__aexit__.return_value = False
    smtp.login.return_value = None
    smtp.supports_extension = MagicMock(return_value=False)
    return smtp


@pytest.mark.asyncio
async def test_smtp_partial_recipient_rejection_is_preserved(email_server) -> None:
    client = EmailClient(email_server, sender="Sender <sender@example.test>")
    smtp = _smtp()

    async def rcpt(recipient: str, **_kwargs: str) -> None:
        if recipient == "rejected@example.test":
            raise SMTPRecipientRefused(550, "rejected", recipient)

    smtp.rcpt.side_effect = rcpt

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await client.send_email_with_outcome(
            ["accepted@example.test", "rejected@example.test"],
            "Subject",
            "body",
        )

    assert [item.status for item in result.outcomes] == ["succeeded", "failed"]
    assert [item.detail for item in result.outcomes] == [None, "smtp-recipient-rejected"]
    assert result.sent_message is not None
    smtp.mail.assert_awaited_once()
    assert smtp.rcpt.await_count == 2
    smtp.data.assert_awaited_once()
    assert b"Bcc:" not in smtp.data.await_args.args[0]


@pytest.mark.asyncio
async def test_smtp_data_transport_loss_marks_accepted_recipients_unknown(email_server) -> None:
    client = EmailClient(email_server, sender="Sender <sender@example.test>")
    smtp = _smtp()
    smtp.data.side_effect = ConnectionError("result lost")

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await client.send_email_with_outcome(
            ["one@example.test", "two@example.test"],
            "Subject",
            "body",
        )

    assert [item.status for item in result.outcomes] == ["unknown", "unknown"]
    assert [item.detail for item in result.outcomes] == ["smtp-data-unknown", "smtp-data-unknown"]
    assert result.sent_message is None
    smtp.data.assert_awaited_once()


@pytest.mark.asyncio
async def test_smtp_data_rejection_is_failed_not_unknown(email_server) -> None:
    client = EmailClient(email_server, sender="Sender <sender@example.test>")
    smtp = _smtp()
    smtp.data.side_effect = SMTPResponseException(554, "rejected")

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await client.send_email_with_outcome(["one@example.test"], "Subject", "body")

    assert [(item.status, item.detail) for item in result.outcomes] == [("failed", "smtp-data-rejected")]
    assert result.sent_message is None


@pytest.mark.asyncio
async def test_smtp_data_cancellation_preserves_rcpt_rejection_and_marks_acceptance_unknown(email_server) -> None:
    client = EmailClient(email_server, sender="Sender <sender@example.test>")
    smtp = _smtp()

    async def rcpt(recipient: str, **_kwargs: str) -> None:
        if recipient == "rejected@example.test":
            raise SMTPRecipientRefused(550, "rejected", recipient)

    smtp.rcpt.side_effect = rcpt
    smtp.data.side_effect = asyncio.CancelledError()

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await client.send_email_with_outcome(
            ["accepted@example.test", "rejected@example.test"],
            "Subject",
            "body",
        )

    assert [(item.status, item.detail) for item in result.outcomes] == [
        ("unknown", "smtp-data-unknown"),
        ("failed", "smtp-recipient-rejected"),
    ]
    assert result.sent_message is None


@pytest.mark.asyncio
async def test_smtp_rcpt_cancellation_stops_before_data_and_marks_remaining_not_attempted(email_server) -> None:
    client = EmailClient(email_server, sender="Sender <sender@example.test>")
    smtp = _smtp()
    smtp.rcpt.side_effect = [None, asyncio.CancelledError()]

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await client.send_email_with_outcome(
            ["one@example.test", "two@example.test", "three@example.test"],
            "Subject",
            "body",
        )

    assert [(item.status, item.detail) for item in result.outcomes] == [
        ("failed", "smtp-cancelled-before-data"),
        ("failed", "smtp-cancelled-before-data"),
        ("failed", "not-attempted"),
    ]
    smtp.data.assert_not_awaited()


@pytest.mark.asyncio
async def test_smtp_context_exit_cancellation_does_not_erase_data_success(email_server) -> None:
    client = EmailClient(email_server, sender="Sender <sender@example.test>")
    smtp = _smtp()
    smtp.__aexit__.side_effect = asyncio.CancelledError()

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await client.send_email_with_outcome(["one@example.test"], "Subject", "body")

    assert [(item.status, item.detail) for item in result.outcomes] == [("succeeded", None)]
    assert result.sent_message is not None


@pytest.mark.asyncio
async def test_smtp_context_exit_failure_does_not_erase_data_success(email_server) -> None:
    client = EmailClient(email_server, sender="Sender <sender@example.test>")
    smtp = _smtp()
    smtp.__aexit__.side_effect = ConnectionError("quit failed")

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        result = await client.send_email_with_outcome(["one@example.test"], "Subject", "body")

    assert [(item.status, item.detail) for item in result.outcomes] == [("succeeded", None)]
    assert result.sent_message is not None


@pytest.mark.asyncio
async def test_mark_read_cancellation_preserves_partial_batch_and_stops(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.side_effect = [("OK", []), asyncio.CancelledError()]
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.mark_emails_as_read_with_outcome(["7", "8", "9"], allowed_senders=[])

    assert [(item.status, item.detail) for item in result.outcomes] == [
        ("succeeded", None),
        ("unknown", "store-unknown"),
        ("failed", "not-attempted"),
    ]
    assert imap.uid.await_count == 2


@pytest.mark.asyncio
async def test_native_move_cancellation_is_unknown_and_stops_batch(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap(capabilities=("IMAP4rev1", "UIDPLUS", "MOVE"))
    imap.uid.side_effect = asyncio.CancelledError()
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["8", "9"], "INBOX", "Archive", allowed_senders=[])

    assert [(item.status, item.detail) for item in result.outcomes] == [
        ("unknown", "move-unknown"),
        ("failed", "not-attempted"),
    ]
    assert imap.uid.await_count == 1


@pytest.mark.asyncio
async def test_move_fallback_store_cancellation_preserves_copy_and_stops(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.side_effect = [("OK", []), asyncio.CancelledError()]
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.move_emails_with_outcome(["8", "9"], "INBOX", "Archive", allowed_senders=[])

    assert [(item.status, item.detail) for item in result.outcomes] == [
        ("unknown", "copy-succeeded-store-unknown"),
        ("failed", "not-attempted"),
    ]
    assert [call.args[0] for call in imap.uid.await_args_list] == ["copy", "store"]


@pytest.mark.asyncio
async def test_delete_expunge_cancellation_is_unknown_after_store(email_server) -> None:
    client = EmailClient(email_server)
    imap = _imap()
    imap.uid.side_effect = [("OK", []), asyncio.CancelledError()]
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        result = await client.delete_emails_with_outcome(["8"], allowed_senders=[])

    assert [(item.status, item.detail) for item in result.outcomes] == [("unknown", "expunge-unknown")]
    assert [call.args[0] for call in imap.uid.await_args_list] == ["store", "expunge"]


@pytest.mark.asyncio
async def test_append_cancellation_is_unknown_and_logout_cancellation_does_not_erase_it(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Draft", "body")
    imap = _imap()
    imap.append.side_effect = asyncio.CancelledError()
    imap.logout.side_effect = asyncio.CancelledError()
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        result = await client.append_to_mailbox_with_outcome(message, email_server, "Drafts")

    assert (result.status, result.detail) == ("unknown", "append-unknown")
    assert imap.append.await_count == 1


@pytest.mark.asyncio
async def test_sent_copy_append_cancellation_is_unknown_and_not_replayed(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Sent", "body")
    imap = _imap()
    imap.append.side_effect = asyncio.CancelledError()
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        result = await client.append_to_sent_with_outcome(message, email_server, "Sent")

    assert (result.status, result.mailbox, result.detail) == ("unknown", "Sent", "append-unknown")
    assert imap.append.await_count == 1
    assert imap.select.await_count == 1


@pytest.mark.asyncio
async def test_sent_copy_ignores_invalid_provider_derived_mailbox(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Sent", "body")
    imap = _imap()
    with (
        patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)),
        patch.object(client, "_find_sent_folder_by_flag", AsyncMock(return_value="Bad\r\nMailbox")),
    ):
        result = await client.append_to_sent_with_outcome(message, email_server)

    assert result.status == "succeeded"
    assert result.mailbox == "Sent"
    assert imap.select.await_args.args == ('"Sent"',)


@pytest.mark.asyncio
async def test_append_ignores_malformed_appenduid_evidence(email_server) -> None:
    client = EmailClient(email_server)
    message = client.compose_message(["recipient@example.test"], "Draft", "body")
    imap = _imap()
    imap.append.return_value = ("OK", [b"[APPENDUID 123 7suffix] completed"])
    with patch.object(client, "_connect_imap_server", AsyncMock(return_value=imap)):
        result = await client.append_to_mailbox_with_outcome(message, email_server, "Drafts")

    assert result.status == "succeeded"
    assert result.uid is None


def test_attachment_read_enforces_per_file_limit_at_compose_time(email_server, tmp_path) -> None:
    client = EmailClient(email_server)
    attachment = tmp_path / "large.bin"
    attachment.write_bytes(b"12345")

    with patch("mcp_email_server.emails.classic.MAX_ATTACHMENT_BYTES", 4):
        with pytest.raises(ValueError, match="attachment exceeds"):
            client.compose_message(
                ["recipient@example.test"],
                "Subject",
                "body",
                attachments=[str(attachment)],
            )


def test_attachment_read_enforces_total_limit_at_compose_time(email_server, tmp_path) -> None:
    client = EmailClient(email_server)
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"1234")
    second.write_bytes(b"5678")

    with (
        patch("mcp_email_server.emails.classic.MAX_ATTACHMENT_BYTES", 4),
        patch("mcp_email_server.emails.classic.MAX_TOTAL_ATTACHMENT_BYTES", 7),
    ):
        with pytest.raises(ValueError, match="attachments exceed"):
            client.compose_message(
                ["recipient@example.test"],
                "Subject",
                "body",
                attachments=[str(first), str(second)],
            )
