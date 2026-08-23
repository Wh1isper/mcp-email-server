from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import Parser
from email.policy import SMTP, SMTPUTF8
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aioimaplib
import pytest
from aiosmtplib.errors import SMTPNotSupported

from mcp_email_server.emails.classic import (
    EmailClient,
    ImapTransportError,
    _append_message,
    _ImapAppendMode,
    _ImapSearchLiteral,
    _message_requires_smtputf8,
    _parse_list_responses,
    _uid_search,
    _validate_flags,
)


class _RecordingTransport(asyncio.WriteTransport):
    def __init__(self, *, fail_write_at: int | None = None) -> None:
        self.writes: list[bytes] = []
        self.aborted = False
        self.fail_write_at = fail_write_at

    def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
        if len(self.writes) == self.fail_write_at:
            raise ConnectionError("transport write failed")
        self.writes.append(bytes(data))

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.aborted = True

    def is_closing(self) -> bool:
        return self.aborted


@pytest.fixture
def rfc_email_client(email_server):
    return EmailClient(
        email_server,
        sender="Test User <test@example.com>",
        sender_address="test@example.com",
    )


def test_recipient_headers_preserve_quoted_names_and_flatten_groups(rfc_email_client):
    raw_email = (
        b"From: sender@example.test\r\n"
        b'To: "Doe, John" <john@example.test>, Team: a@example.test, b@example.test;\r\n'
        b"Cc: cc@example.test\r\n"
        b"Subject: Address list\r\n"
        b"Date: Sat, 8 Aug 2026 00:00:00 +0000\r\n"
        b"\r\n"
        b"body"
    )

    parsed = rfc_email_client._parse_email_data(raw_email)
    metadata = rfc_email_client._parse_headers("1", raw_email.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n")

    expected = [
        '"Doe, John" <john@example.test>',
        "a@example.test",
        "b@example.test",
        "cc@example.test",
    ]
    assert parsed["to"] == expected
    assert metadata is not None
    assert metadata["to"] == expected


def test_unknown_single_part_charset_falls_back_without_losing_message(rfc_email_client):
    raw_email = (
        b"From: sender@example.test\r\n"
        b"To: recipient@example.test\r\n"
        b"Subject: Unknown charset\r\n"
        b"Content-Type: text/plain; charset=x-no-such-codec\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n"
        b"hello \xff"
    )

    parsed = rfc_email_client._parse_email_data(raw_email)

    assert parsed["subject"] == "Unknown charset"
    assert parsed["body"] == "hello �"
    assert parsed["to"] == ["recipient@example.test"]


def test_unknown_multipart_charset_does_not_hide_valid_parts(rfc_email_client):
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message["Subject"] = "Mixed charsets"
    message.make_mixed()

    unknown = EmailMessage()
    unknown.set_type("text/plain")
    unknown.set_param("charset", "x-no-such-codec")
    unknown["Content-Transfer-Encoding"] = "8bit"
    unknown.set_payload(b"unknown \xff")
    message.attach(unknown)

    valid = EmailMessage()
    valid.set_content(" valid", charset="utf-8")
    message.attach(valid)

    parsed = rfc_email_client._parse_email_data(message.as_bytes(policy=SMTP))

    assert parsed["body"] == "unknown � valid\r\n"


def _attached_message(*, filename: str | None) -> bytes:
    disposition = b""
    parameters = b""
    if filename is not None:
        parameters = f'; name="{filename}"'.encode()
        disposition = f'Content-Disposition: attachment; filename="{filename}"\r\n'.encode()
    return (
        b"From: sender@example.test\r\n"
        b"To: recipient@example.test\r\n"
        b"Subject: Outer\r\n"
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="outer"\r\n'
        b"\r\n"
        b"--outer\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nOUTER\r\n"
        b"--outer\r\nContent-Type: message/rfc822" + parameters + b"\r\n" + disposition + b"\r\n"
        b"From: nested@example.test\r\n"
        b"To: nested-recipient@example.test\r\n"
        b"Subject: Nested\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"INNER\r\n"
        b"--outer--\r\n"
    )


@pytest.mark.parametrize(
    ("filename", "expected_attachments"),
    [("forwarded.eml", ["forwarded.eml"]), (None, [])],
)
def test_attached_message_subtree_is_not_promoted_to_outer_body(
    rfc_email_client,
    filename,
    expected_attachments,
):
    parsed = rfc_email_client._parse_email_data(_attached_message(filename=filename))

    assert parsed["body"] == "OUTER"
    assert parsed["attachments"] == expected_attachments


def test_imap_keyword_validation_accepts_complete_atom_grammar():
    assert _validate_flags(["$Forwarded", "$Junk", "project.name", "123flag"]) == (
        "($Forwarded $Junk project.name 123flag)"
    )


@pytest.mark.parametrize("invalid", ["", "two words", "bad(flag", "bad{flag", "bad%flag", "bad*flag", 'bad"flag'])
def test_imap_keyword_validation_rejects_atom_specials(invalid):
    with pytest.raises(ValueError, match="Invalid IMAP flag"):
        _validate_flags([invalid])


def test_imap_dates_use_fixed_english_month_names():
    expected_months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

    for month, expected in enumerate(expected_months, start=1):
        value = datetime(2026, month, 1, tzinfo=UTC)
        assert EmailClient._build_search_criteria(since=value) == ["SINCE", f"01-{expected}-2026"]


def test_list_response_framing_ignores_completion_and_reassembles_literal():
    parsed = _parse_list_responses([
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\sent) "/" {4}',
        bytearray(b"Sent"),
        b"LIST completed.",
    ])

    assert [(mailbox.name, mailbox.delimiter, mailbox.flags) for mailbox in parsed] == [
        ("INBOX", "/", [r"\HasNoChildren"]),
        ("Sent", "/", [r"\sent"]),
    ]


@pytest.mark.parametrize(
    "lines",
    [
        [b'(\\Sent) "/" {4}'],
        [b'(\\Sent) "/" {5}', bytearray(b"Sent")],
    ],
)
def test_list_response_framing_rejects_invalid_literals(lines):
    with pytest.raises(ValueError, match="LIST literal"):
        _parse_list_responses(lines)


@pytest.mark.asyncio
async def test_sent_folder_special_use_matching_is_case_insensitive(rfc_email_client):
    imap = AsyncMock()
    imap.list.return_value = (
        "OK",
        [b'(\\sent \\HasNoChildren) "/" "Gesendet"', b"LIST completed."],
    )

    assert await rfc_email_client._find_sent_folder_by_flag(imap) == "Gesendet"


def test_search_astring_encoding_preserves_user_text(rfc_email_client):
    assert rfc_email_client._sanitize_imap_value("simple") == "simple"
    assert rfc_email_client._sanitize_imap_value("a]b") == "a]b"
    assert rfc_email_client._sanitize_imap_value("two words") == '"two words"'
    assert rfc_email_client._sanitize_imap_value("foo(bar)") == '"foo(bar)"'
    assert rfc_email_client._sanitize_imap_value('say"hi"') == '"say\\"hi\\""'
    assert rfc_email_client._sanitize_imap_value(r"C:\Path") == '"C:\\\\Path"'
    assert rfc_email_client._sanitize_imap_value("100%") == '"100%"'

    literal = rfc_email_client._sanitize_imap_value("会议")
    assert isinstance(literal, _ImapSearchLiteral)
    assert literal.data == "会议".encode()

    with pytest.raises(ValueError, match="control characters"):
        rfc_email_client._sanitize_imap_value("bad\r\nvalue")


def _recording_imap(timeout: float = 1.0, *, fail_write_at: int | None = None):
    protocol = aioimaplib.IMAP4ClientProtocol(asyncio.get_running_loop())
    protocol.state = aioimaplib.SELECTED
    transport = _RecordingTransport(fail_write_at=fail_write_at)
    protocol.transport = transport
    imap = object.__new__(aioimaplib.IMAP4)
    imap.protocol = protocol
    imap.timeout = timeout
    return imap, protocol, transport


@pytest.mark.asyncio
async def test_uid_search_uses_one_synchronizing_literal_per_unicode_value():
    imap, protocol, transport = _recording_imap()
    subject = "会议".encode()
    body = "正文".encode()

    search_task = asyncio.create_task(
        _uid_search(
            imap,
            ["SUBJECT", _ImapSearchLiteral(subject), "BODY", _ImapSearchLiteral(body)],
        )
    )
    await asyncio.sleep(0)

    first_line = transport.writes[0]
    tag = first_line.split(maxsplit=1)[0]
    assert first_line.endswith(b"UID SEARCH CHARSET UTF-8 SUBJECT {6}\r\n")
    assert subject not in first_line

    protocol.data_received(b"+ first literal\r\n")
    assert transport.writes[1:] == [subject, b" BODY {6}\r\n"]

    protocol.data_received(b"+ second literal\r\n")
    assert transport.writes[3:] == [body, b"\r\n"]

    protocol.data_received(b"* SEARCH 12 34\r\n" + tag + b" OK SEARCH completed\r\n")
    response = await search_task

    assert response.result == "OK"
    assert response.lines == [b"12 34", b"SEARCH completed"]


@pytest.mark.asyncio
async def test_uid_search_does_not_send_literal_after_definitive_rejection():
    imap, protocol, transport = _recording_imap()
    literal = "会议".encode()

    search_task = asyncio.create_task(_uid_search(imap, ["SUBJECT", _ImapSearchLiteral(literal)]))
    await asyncio.sleep(0)
    tag = transport.writes[0].split(maxsplit=1)[0]
    protocol.data_received(tag + b" NO SEARCH rejected\r\n")

    response = await search_task

    assert response.result == "NO"
    assert transport.writes == [transport.writes[0]]
    assert literal not in transport.writes[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_first_literal", [False, True])
async def test_literal_search_cancellation_releases_command_and_aborts_connection(after_first_literal):
    imap, protocol, transport = _recording_imap()
    subject = "会议".encode()
    body = "正文".encode()
    task = asyncio.create_task(
        _uid_search(
            imap,
            ["SUBJECT", _ImapSearchLiteral(subject), "BODY", _ImapSearchLiteral(body)],
        )
    )
    await asyncio.sleep(0)

    if after_first_literal:
        protocol.data_received(b"+ first literal\r\n")
        assert subject in transport.writes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert transport.aborted is True
    assert imap.protocol is None
    assert protocol.pending_sync_command is None
    assert body not in transport.writes


@pytest.mark.asyncio
async def test_literal_search_initial_write_failure_aborts_connection():
    imap, protocol, transport = _recording_imap(fail_write_at=0)

    with pytest.raises(ImapTransportError, match="SEARCH failed"):
        await _uid_search(imap, ["SUBJECT", _ImapSearchLiteral("会议".encode())])

    assert transport.aborted is True
    assert imap.protocol is None
    assert protocol.pending_sync_command is None
    assert transport.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_write_at", [1, 2])
async def test_literal_search_continuation_write_failure_aborts_without_replay(fail_write_at):
    imap, protocol, transport = _recording_imap(fail_write_at=fail_write_at)
    literal = "会议".encode()
    task = asyncio.create_task(_uid_search(imap, ["SUBJECT", _ImapSearchLiteral(literal)]))
    await asyncio.sleep(0)

    protocol.data_received(b"+ send literal\r\n")
    with pytest.raises(ImapTransportError, match="SEARCH failed"):
        await task

    assert transport.aborted is True
    assert imap.protocol is None
    assert protocol.pending_sync_command is None
    assert transport.writes.count(literal) <= 1


def _smtp_with_extensions(*extensions: str) -> AsyncMock:
    smtp = AsyncMock()
    smtp.__aenter__.return_value = smtp
    smtp.__aexit__.return_value = False
    smtp.login.return_value = None
    advertised = {extension.casefold() for extension in extensions}
    smtp.supports_extension = MagicMock(side_effect=lambda name: name.casefold() in advertised)
    return smtp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header_kwargs", "expected_utf8"),
    [
        ({"reply_to": "回复@example.test"}, "回复@example.test"),
        ({"references": "<线程@example.test>"}, "<线程@example.test>"),
    ],
)
async def test_legacy_send_uses_smtputf8_for_message_headers_with_ascii_envelope(
    rfc_email_client,
    header_kwargs,
    expected_utf8,
):
    smtp = _smtp_with_extensions("SMTPUTF8", "8BITMIME")

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        returned = await rfc_email_client.send_email(
            ["recipient@example.test"],
            "Subject",
            "body",
            **header_kwargs,
        )

    smtp.send_message.assert_not_awaited()
    smtp.sendmail.assert_awaited_once()
    sender, recipients, payload = smtp.sendmail.await_args.args
    assert sender == "test@example.com"
    assert recipients == ["recipient@example.test"]
    assert smtp.sendmail.await_args.kwargs["mail_options"] == ["SMTPUTF8", "BODY=8BITMIME"]
    assert expected_utf8.encode() in payload
    assert returned.policy.utf8 is True


@pytest.mark.asyncio
async def test_legacy_send_rejects_message_smtputf8_before_mail_when_unsupported(rfc_email_client):
    smtp = _smtp_with_extensions()

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        with pytest.raises(SMTPNotSupported, match="SMTPUTF8"):
            await rfc_email_client.send_email(
                ["recipient@example.test"],
                "Subject",
                "body",
                reply_to="回复@example.test",
            )

    smtp.mail.assert_not_awaited()
    smtp.sendmail.assert_not_awaited()
    smtp.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_smtputf8_send_rejects_before_mail_when_8bitmime_is_missing(rfc_email_client):
    smtp = _smtp_with_extensions("SMTPUTF8")

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        with pytest.raises(SMTPNotSupported, match="8BITMIME"):
            await rfc_email_client.send_email(
                ["recipient@example.test"],
                "Subject",
                "body",
                reply_to="回复@example.test",
            )

    smtp.mail.assert_not_awaited()
    smtp.sendmail.assert_not_awaited()
    smtp.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unicode_reply_to_requires_smtputf8_for_ascii_envelope(rfc_email_client):
    smtp = _smtp_with_extensions("SMTPUTF8", "8BITMIME", "SIZE")

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        outcome = await rfc_email_client.send_email_with_outcome(
            ["recipient@example.test"],
            "Subject",
            "body",
            reply_to="回复@example.test",
        )

    assert [(item.status, item.detail) for item in outcome.outcomes] == [("succeeded", None)]
    options = smtp.mail.await_args.kwargs["options"]
    payload = smtp.data.await_args.args[0]
    assert options == [f"SIZE={len(payload)}", "SMTPUTF8", "BODY=8BITMIME"]
    assert smtp.mail.await_args.kwargs["encoding"] == "utf-8"
    assert "回复@example.test".encode() in payload

    parsed = Parser(policy=SMTPUTF8).parsestr(payload.decode("utf-8"))
    reply_to = parsed["Reply-To"]
    assert reply_to is not None
    assert [address.addr_spec for address in reply_to.addresses] == ["回复@example.test"]
    assert [type(defect).__name__ for defect in reply_to.defects] == ["NonASCIILocalPartDefect"]


@pytest.mark.asyncio
async def test_smtputf8_outcome_rejects_before_mail_when_8bitmime_is_missing(rfc_email_client):
    smtp = _smtp_with_extensions("SMTPUTF8")

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        outcome = await rfc_email_client.send_email_with_outcome(
            ["recipient@example.test"],
            "Subject",
            "body",
            reply_to="回复@example.test",
        )

    assert [(item.status, item.detail) for item in outcome.outcomes] == [("failed", "smtp-8bitmime-required")]
    smtp.mail.assert_not_awaited()
    smtp.rcpt.assert_not_awaited()
    smtp.data.assert_not_awaited()


@pytest.mark.asyncio
async def test_unicode_reply_to_fails_before_mail_when_smtputf8_is_unavailable(rfc_email_client):
    smtp = _smtp_with_extensions()

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        outcome = await rfc_email_client.send_email_with_outcome(
            ["recipient@example.test"],
            "Subject",
            "body",
            reply_to="回复@example.test",
        )

    assert [(item.status, item.detail) for item in outcome.outcomes] == [("failed", "smtp-utf8-unsupported")]
    smtp.mail.assert_not_awaited()
    smtp.rcpt.assert_not_awaited()
    smtp.data.assert_not_awaited()


@pytest.mark.asyncio
async def test_unicode_display_name_with_ascii_addr_spec_does_not_require_smtputf8(rfc_email_client):
    smtp = _smtp_with_extensions()

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        outcome = await rfc_email_client.send_email_with_outcome(
            ["recipient@example.test"],
            "Subject",
            "body",
            reply_to='"回复人" <reply@example.test>',
        )

    assert [(item.status, item.detail) for item in outcome.outcomes] == [("succeeded", None)]
    smtp.mail.assert_awaited_once_with("test@example.com", options=[], encoding="ascii")
    payload = smtp.data.await_args.args[0]
    assert b"SMTPUTF8" not in payload
    assert b"=?utf-8?" in payload.lower()


def test_message_utf8_requirement_covers_address_and_thread_headers(rfc_email_client):
    ascii_message = rfc_email_client.compose_message(
        ["recipient@example.test"],
        "Subject",
        "body",
        reply_to='"回复人" <reply@example.test>',
    )
    eai_message = rfc_email_client.compose_message(
        ["用户@example.test"],
        "Subject",
        "body",
    )
    thread_message = rfc_email_client.compose_message(
        ["recipient@example.test"],
        "Subject",
        "body",
        references="<线程@example.test>",
    )

    assert _message_requires_smtputf8(ascii_message) is False
    assert _message_requires_smtputf8(eai_message) is True
    assert _message_requires_smtputf8(thread_message) is True
    assert eai_message.policy.utf8 is True
    assert thread_message.policy.utf8 is True


@pytest.mark.asyncio
async def test_rfc6855_append_uses_utf8_literal8_framing(rfc_email_client):
    message = rfc_email_client.compose_message(["用户@example.test"], "Draft", "body")
    imap, protocol, transport = _recording_imap()

    append_task = asyncio.create_task(
        _append_message(
            imap,
            message,
            mailbox='"草稿"',
            flags=r"(\Draft \Seen)",
            mode=_ImapAppendMode(message_requires_utf8=True, session_utf8_enabled=True),
        )
    )
    await asyncio.sleep(0)

    command_line = transport.writes[0]
    tag = command_line.split(maxsplit=1)[0]
    assert ' APPEND "草稿" (\\Draft \\Seen) UTF8 (~{'.encode() in command_line
    assert command_line.endswith(b"}\r\n")
    assert "用户@example.test".encode() not in command_line

    protocol.data_received(b"+ ready for literal8\r\n")
    payload_with_closing_parenthesis = transport.writes[1]
    assert payload_with_closing_parenthesis.endswith(b")")
    payload = payload_with_closing_parenthesis[:-1]
    assert transport.writes[2] == b"\r\n"
    assert "用户@example.test".encode() in payload
    assert b"\n" in payload
    assert re.search(rb"(?<!\r)\n", payload) is None

    parsed = Parser(policy=SMTPUTF8).parsestr(payload.decode("utf-8"))
    recipient = parsed["To"]
    assert recipient is not None
    assert [address.addr_spec for address in recipient.addresses] == ["用户@example.test"]
    assert [type(defect).__name__ for defect in recipient.defects] == ["NonASCIILocalPartDefect"]

    protocol.data_received(tag + b" OK [APPENDUID 99 7] APPEND completed\r\n")
    response = await append_task
    assert response.result == "OK"


@pytest.mark.asyncio
async def test_eai_draft_is_rejected_before_select_when_utf8_accept_is_unavailable(
    rfc_email_client,
    email_server,
):
    message = rfc_email_client.compose_message(["用户@example.test"], "Draft", "body")
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.id.return_value = MagicMock(result="OK", lines=[])
    imap.logout.return_value = ("BYE", [])
    protocol = MagicMock()
    protocol.capabilities = {"IMAP4REV1", "UIDPLUS"}
    protocol.capability = AsyncMock()
    imap.protocol = protocol

    with patch.object(rfc_email_client, "_connect_imap_server", AsyncMock(return_value=imap)):
        outcome = await rfc_email_client.append_to_mailbox_with_outcome(message, email_server, "Drafts")

    assert (outcome.status, outcome.detail) == ("failed", "utf8-append-unsupported")
    imap.enable.assert_not_awaited()
    imap.select.assert_not_awaited()
    imap.append.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("utf8_capability", ["UTF8=ACCEPT", "UTF8=ONLY"])
async def test_eai_draft_enables_rfc6855_before_select_and_appends_once(
    rfc_email_client,
    email_server,
    utf8_capability,
):
    message = rfc_email_client.compose_message(["用户@example.test"], "Draft", "body")
    mailbox = "草稿" if utf8_capability == "UTF8=ONLY" else "Drafts"
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.enable.return_value = ("OK", [b"ENABLED UTF8=ACCEPT", b"ENABLE completed"])
    imap.select.return_value = ("OK", [])
    imap.logout.return_value = ("BYE", [])
    protocol = MagicMock()
    protocol.capabilities = {"IMAP4REV1", "ENABLE", utf8_capability}
    protocol.capability = AsyncMock()
    imap.protocol = protocol
    append = AsyncMock(return_value=("OK", [b"[APPENDUID 99 7] APPEND completed"]))

    with patch.object(rfc_email_client, "_connect_imap_server", AsyncMock(return_value=imap)):
        with patch("mcp_email_server.emails.classic._append_message", append):
            outcome = await rfc_email_client.append_to_mailbox_with_outcome(message, email_server, mailbox)

    assert (outcome.status, outcome.uid, outcome.detail) == ("succeeded", "7", None)
    protocol.capability.assert_awaited_once()
    imap.enable.assert_awaited_once_with("UTF8=ACCEPT")
    imap.select.assert_awaited_once_with(f'"{mailbox}"')
    append.assert_awaited_once_with(
        imap,
        message,
        mailbox=f'"{mailbox}"',
        flags=r"(\Draft \Seen)",
        mode=_ImapAppendMode(message_requires_utf8=True, session_utf8_enabled=True),
    )
    call_names = [call[0] for call in imap.mock_calls]
    assert call_names.index("enable") < call_names.index("select")


@pytest.mark.asyncio
async def test_utf8_only_enables_ascii_message_before_base_append(rfc_email_client, email_server):
    message = rfc_email_client.compose_message(["recipient@example.test"], "Draft", "body")
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.enable.return_value = ("OK", [b"ENABLED UTF8=ACCEPT", b"ENABLE completed"])
    imap.select.return_value = ("OK", [])
    imap.append.return_value = ("OK", [b"APPEND completed"])
    imap.logout.return_value = ("BYE", [])
    protocol = MagicMock()
    protocol.capabilities = {"IMAP4REV1", "ENABLE", "UTF8=ONLY"}
    protocol.capability = AsyncMock()
    imap.protocol = protocol

    with patch.object(rfc_email_client, "_connect_imap_server", AsyncMock(return_value=imap)):
        outcome = await rfc_email_client.append_to_mailbox_with_outcome(message, email_server, "Drafts")

    assert (outcome.status, outcome.detail) == ("succeeded", None)
    imap.enable.assert_awaited_once_with("UTF8=ACCEPT")
    imap.select.assert_awaited_once_with('"Drafts"')
    imap.append.assert_awaited_once()
    call_names = [call[0] for call in imap.mock_calls]
    assert call_names.index("enable") < call_names.index("select") < call_names.index("append")


@pytest.mark.asyncio
async def test_utf8_sent_discovery_preserves_literal_ampersand_name(rfc_email_client, email_server):
    message = rfc_email_client.compose_message(["用户@example.test"], "Sent", "body")
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.enable.return_value = ("OK", [b"ENABLED UTF8=ACCEPT", b"ENABLE completed"])
    imap.list.return_value = ("OK", [b'(\\Sent) "/" "A&-B"', b"LIST completed"])
    imap.select.return_value = ("OK", [])
    imap.logout.return_value = ("BYE", [])
    protocol = MagicMock()
    protocol.capabilities = {"IMAP4REV1", "ENABLE", "UTF8=ONLY"}
    protocol.capability = AsyncMock()
    imap.protocol = protocol
    append = AsyncMock(return_value=("OK", [b"APPEND completed"]))

    with patch.object(rfc_email_client, "_connect_imap_server", AsyncMock(return_value=imap)):
        with patch("mcp_email_server.emails.classic._append_message", append):
            outcome = await rfc_email_client.append_to_sent_with_outcome(message, email_server)

    assert (outcome.status, outcome.mailbox) == ("succeeded", "A&-B")
    imap.select.assert_awaited_once_with('"A&-B"')
    append.assert_awaited_once_with(
        imap,
        message,
        mailbox='"A&-B"',
        flags=r"(\Seen)",
        mode=_ImapAppendMode(message_requires_utf8=True, session_utf8_enabled=True),
    )


@pytest.mark.asyncio
async def test_eai_draft_rejects_incomplete_enable_evidence_before_select(
    rfc_email_client,
    email_server,
):
    message = rfc_email_client.compose_message(["用户@example.test"], "Draft", "body")
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.enable.return_value = ("OK", [b"ENABLE completed"])
    imap.logout.return_value = ("BYE", [])
    protocol = MagicMock()
    protocol.capabilities = {"IMAP4REV1", "ENABLE", "UTF8=ACCEPT"}
    protocol.capability = AsyncMock()
    imap.protocol = protocol

    with patch.object(rfc_email_client, "_connect_imap_server", AsyncMock(return_value=imap)):
        outcome = await rfc_email_client.append_to_mailbox_with_outcome(message, email_server, "Drafts")

    assert (outcome.status, outcome.detail) == ("failed", "utf8-append-unsupported")
    imap.enable.assert_awaited_once_with("UTF8=ACCEPT")
    imap.select.assert_not_awaited()
    imap.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_sent_copy_does_not_replay_ascii_append_after_timeout(rfc_email_client, email_server):
    message = rfc_email_client.compose_message(["recipient@example.test"], "Sent", "body")
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.list.return_value = ("OK", [])
    imap.select.return_value = ("OK", [])
    imap.append.side_effect = TimeoutError("APPEND completion lost")
    imap.logout.return_value = ("BYE", [])
    protocol = MagicMock()
    protocol.capabilities = {"IMAP4REV1"}
    protocol.capability = AsyncMock()
    imap.protocol = protocol

    with patch.object(rfc_email_client, "_connect_imap_server", AsyncMock(return_value=imap)):
        saved = await rfc_email_client.append_to_sent(message, email_server, "Sent")

    assert saved is False
    imap.append.assert_awaited_once()
    imap.select.assert_awaited_once_with('"Sent"')


@pytest.mark.asyncio
async def test_unicode_envelope_recipient_uses_smtputf8_for_mail_rcpt_and_data(rfc_email_client):
    smtp = _smtp_with_extensions("SMTPUTF8", "8BITMIME")

    with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
        outcome = await rfc_email_client.send_email_with_outcome(
            ["用户@example.test"],
            "Subject",
            "body",
        )

    assert [(item.status, item.detail) for item in outcome.outcomes] == [("succeeded", None)]
    smtp.mail.assert_awaited_once_with(
        "test@example.com",
        options=["SMTPUTF8", "BODY=8BITMIME"],
        encoding="utf-8",
    )
    smtp.rcpt.assert_awaited_once_with("用户@example.test", encoding="utf-8")
    assert "用户@example.test".encode() in smtp.data.await_args.args[0]


@pytest.mark.asyncio
async def test_literal_search_timeout_aborts_unframed_connection():
    imap, _protocol, transport = _recording_imap(timeout=0.01)

    with pytest.raises(TimeoutError, match="SEARCH timed out"):
        await _uid_search(imap, ["SUBJECT", _ImapSearchLiteral("会议".encode())])

    assert transport.aborted is True
    assert imap.protocol is None


def test_utf8_list_mode_preserves_literal_ampersand_mailbox_spelling():
    lines = [b'(\\Sent) "/" "A&-B"', b'(\\HasNoChildren) "/" "A&AOk-B"']

    assert [mailbox.name for mailbox in _parse_list_responses(lines, utf8=True)] == ["A&-B", "A&AOk-B"]
    assert [mailbox.name for mailbox in _parse_list_responses(lines)] == ["A&B", "AéB"]


def test_list_literal_round_trips_spaces_quotes_and_backslashes():
    mailbox_name = b'A "quoted" \\ mailbox'
    parsed = _parse_list_responses([
        b'() "/" {' + str(len(mailbox_name)).encode("ascii") + b"}",
        bytearray(mailbox_name),
        b"LIST completed",
    ])

    assert len(parsed) == 1
    assert parsed[0].name == mailbox_name.decode("ascii")
