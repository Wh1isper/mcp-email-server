"""Tests for the save_to_mailbox feature — IMAP APPEND to arbitrary folders."""

import asyncio
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient, _validate_flags

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_OUTGOING_SERVER = {
    "user_name": "test_user",
    "password": "test_password",
    "host": "smtp.example.com",
    "port": 465,
    "use_ssl": True,
}

_INCOMING_SERVER = {
    "user_name": "test_user",
    "password": "test_password",
    "host": "imap.example.com",
    "port": 993,
    "use_ssl": True,
}


@pytest.fixture
def outgoing_server():
    return EmailServer(**_OUTGOING_SERVER)


@pytest.fixture
def incoming_server():
    return EmailServer(**_INCOMING_SERVER)


@pytest.fixture
def email_client(outgoing_server):
    return EmailClient(outgoing_server, sender="Test User <test@example.com>")


@pytest.fixture
def email_settings():
    return EmailSettings(
        account_name="test_account",
        full_name="Test User",
        email_address="test@example.com",
        incoming=EmailServer(**_INCOMING_SERVER),
        outgoing=EmailServer(**_OUTGOING_SERVER),
    )


@pytest.fixture
def mock_imap():
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    mock.select = AsyncMock(return_value=("OK", []))
    mock.append = AsyncMock(return_value=("OK", []))
    mock.logout = AsyncMock()
    return mock


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------


class TestValidateFlags:
    """Tests for _validate_flags — IMAP flag injection prevention."""

    def test_valid_system_flags(self):
        result = _validate_flags([r"\Draft", r"\Seen"])
        assert result == r"(\Draft \Seen)"

    def test_valid_custom_keyword(self):
        result = _validate_flags(["MyLabel", r"\Flagged"])
        assert result == r"(MyLabel \Flagged)"

    def test_empty_list(self):
        assert _validate_flags([]) == "()"

    def test_rejects_parentheses(self):
        with pytest.raises(ValueError, match="Invalid IMAP flag"):
            _validate_flags([r"\Seen) 25-Dec-2025 {99999}"])

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="Invalid IMAP flag"):
            _validate_flags([r"\Seen \Deleted"])

    def test_rejects_braces(self):
        with pytest.raises(ValueError, match="Invalid IMAP flag"):
            _validate_flags(["{literal}"])

    def test_rejects_asterisk(self):
        with pytest.raises(ValueError, match="Invalid IMAP flag"):
            _validate_flags(["*"])

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="Invalid IMAP flag"):
            _validate_flags([""])

    def test_rejects_numeric_start(self):
        with pytest.raises(ValueError, match="Invalid IMAP flag"):
            _validate_flags(["123flag"])


# ---------------------------------------------------------------------------
# Cycle 1: compose_message
# ---------------------------------------------------------------------------


class TestComposeMessage:
    """Tests for EmailClient.compose_message — extracted message composition."""

    def test_plain_text_message(self, email_client):
        msg = email_client.compose_message(
            recipients=["recipient@example.com"],
            subject="Test Subject",
            body="Hello world",
        )
        assert msg["Subject"] == "Test Subject"
        assert "recipient@example.com" in msg["To"]
        assert msg["From"] == "Test User <test@example.com>"
        assert msg["Date"] is not None
        assert msg["Message-Id"] is not None

    def test_html_message(self, email_client):
        msg = email_client.compose_message(
            recipients=["r@example.com"],
            subject="HTML",
            body="<b>bold</b>",
            html=True,
        )
        assert msg.get_content_type() == "text/html"

    def test_cc_header(self, email_client):
        msg = email_client.compose_message(
            recipients=["r@example.com"],
            subject="CC",
            body="body",
            cc=["cc1@example.com", "cc2@example.com"],
        )
        assert "cc1@example.com" in msg["Cc"]
        assert "cc2@example.com" in msg["Cc"]

    def test_bcc_not_in_headers(self, email_client):
        msg = email_client.compose_message(
            recipients=["r@example.com"],
            subject="BCC",
            body="body",
            bcc=["secret@example.com"],
        )
        assert msg["Bcc"] is None  # BCC must not appear in headers

    def test_threading_headers(self, email_client):
        msg = email_client.compose_message(
            recipients=["r@example.com"],
            subject="Re: Thread",
            body="reply",
            in_reply_to="<original@example.com>",
            references="<original@example.com>",
        )
        assert msg["In-Reply-To"] == "<original@example.com>"
        assert msg["References"] == "<original@example.com>"

    def test_unicode_subject(self, email_client):
        msg = email_client.compose_message(
            recipients=["r@example.com"],
            subject="Tesüöä",
            body="body",
        )
        # Should not raise; subject is encoded via Header
        assert msg["Subject"] is not None

    def test_with_attachments(self, email_client, tmp_path):
        test_file = tmp_path / "doc.txt"
        test_file.write_text("file content")
        msg = email_client.compose_message(
            recipients=["r@example.com"],
            subject="Attach",
            body="see attached",
            attachments=[str(test_file)],
        )
        assert msg.get_content_type() == "multipart/mixed"


class TestComposeMessageBccHeader:
    """Tests for BCC header inclusion in compose_message."""

    def test_bcc_header_included_when_flag_true(self, email_client):
        msg = email_client.compose_message(
            ["r@example.com"],
            "Sub",
            "Body",
            bcc=["secret@example.com"],
            include_bcc_header=True,
        )
        assert msg["Bcc"] == "secret@example.com"

    def test_bcc_header_multiple_recipients(self, email_client):
        msg = email_client.compose_message(
            ["r@example.com"],
            "Sub",
            "Body",
            bcc=["a@example.com", "b@example.com"],
            include_bcc_header=True,
        )
        assert msg["Bcc"] == "a@example.com, b@example.com"

    def test_bcc_header_omitted_when_empty_list(self, email_client):
        msg = email_client.compose_message(
            ["r@example.com"],
            "Sub",
            "Body",
            bcc=[],
            include_bcc_header=True,
        )
        assert msg["Bcc"] is None

    def test_bcc_header_omitted_when_none(self, email_client):
        msg = email_client.compose_message(
            ["r@example.com"],
            "Sub",
            "Body",
            bcc=None,
            include_bcc_header=True,
        )
        assert msg["Bcc"] is None


# ---------------------------------------------------------------------------
# Cycle 2: append_to_mailbox
# ---------------------------------------------------------------------------


class TestAppendToMailbox:
    """Tests for EmailClient.append_to_mailbox — IMAP APPEND to a specific folder."""

    @pytest.mark.asyncio
    async def test_append_success(self, email_client, incoming_server, mock_imap):
        msg = MIMEText("Draft body")
        msg["Subject"] = "Draft"
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Drafts")
        assert result == "unknown"  # no APPENDUID in mock response
        mock_imap.select.assert_called_with('"Drafts"')
        mock_imap.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_returns_uid_from_appenduid(self, email_client, incoming_server, mock_imap):
        mock_imap.append = AsyncMock(return_value=("OK", [b"[APPENDUID 1234567890 42] APPEND completed"]))
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Drafts")
        assert result == "42"

    @pytest.mark.asyncio
    async def test_append_with_custom_flags(self, email_client, incoming_server, mock_imap):
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            await email_client.append_to_mailbox(msg, incoming_server, "Templates", flags=r"(\Seen \Flagged)")
        _, kwargs = mock_imap.append.call_args
        assert kwargs["flags"] == r"(\Seen \Flagged)"

    @pytest.mark.asyncio
    async def test_append_folder_not_found(self, email_client, incoming_server, mock_imap):
        mock_imap.select = AsyncMock(return_value=("NO", []))
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_append_login_failure(self, email_client, incoming_server, mock_imap):
        mock_imap.login = AsyncMock(side_effect=Exception("Auth failed"))
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Drafts")
        assert result is None

    @pytest.mark.asyncio
    async def test_append_imap_append_fails(self, email_client, incoming_server, mock_imap):
        mock_imap.append = AsyncMock(return_value=("NO", [b"APPEND failed"]))
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Drafts")
        assert result is None

    @pytest.mark.asyncio
    async def test_append_non_ssl(self, mock_imap):
        server = EmailServer(
            user_name="test_user",
            password="test_password",
            host="smtp.example.com",
            port=25,
            use_ssl=False,
        )
        client = EmailClient(server)
        incoming_non_ssl = EmailServer(
            user_name="test_user",
            password="test_password",
            host="imap.example.com",
            port=143,
            use_ssl=False,
        )
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4.return_value = mock_imap
            result = await client.append_to_mailbox(msg, incoming_non_ssl, "Drafts")
        assert result == "unknown"
        mock_lib.IMAP4.assert_called_once()


# ---------------------------------------------------------------------------
# Cycle 3: ClassicEmailHandler.save_to_mailbox
# ---------------------------------------------------------------------------


class TestClassicEmailHandlerSaveToMailbox:
    """Tests for ClassicEmailHandler.save_to_mailbox — end-to-end orchestration."""

    @pytest.mark.asyncio
    async def test_save_to_drafts_default_flags(self, email_settings):
        handler = ClassicEmailHandler(email_settings)
        mock_compose = MIMEText("body")
        mock_compose["Message-Id"] = "<test-id@example.com>"
        mock_append = AsyncMock(return_value="42")

        with patch.object(handler.outgoing_client, "compose_message", return_value=mock_compose):
            with patch.object(handler.outgoing_client, "append_to_mailbox", mock_append):
                result = await handler.save_to_mailbox(
                    recipients=["r@example.com"],
                    subject="Draft",
                    body="draft body",
                )

        assert "<test-id@example.com>" in result
        assert "uid:42" in result
        mock_append.assert_called_once_with(
            mock_compose,
            email_settings.incoming,
            "Drafts",
            r"(\Draft \Seen)",
        )

    @pytest.mark.asyncio
    async def test_save_to_custom_folder_custom_flags(self, email_settings):
        handler = ClassicEmailHandler(email_settings)
        mock_compose = MIMEText("body")
        mock_compose["Message-Id"] = "<test-id@example.com>"
        mock_append = AsyncMock(return_value="99")

        with patch.object(handler.outgoing_client, "compose_message", return_value=mock_compose):
            with patch.object(handler.outgoing_client, "append_to_mailbox", mock_append):
                await handler.save_to_mailbox(
                    recipients=["r@example.com"],
                    subject="Template",
                    body="body",
                    mailbox="Templates",
                    flags=[r"\Seen", r"\Flagged"],
                )

        mock_append.assert_called_once_with(
            mock_compose,
            email_settings.incoming,
            "Templates",
            r"(\Seen \Flagged)",
        )

    @pytest.mark.asyncio
    async def test_save_raises_on_failure(self, email_settings):
        handler = ClassicEmailHandler(email_settings)
        mock_compose = MIMEText("body")
        mock_append = AsyncMock(return_value=None)

        with patch.object(handler.outgoing_client, "compose_message", return_value=mock_compose):
            with patch.object(handler.outgoing_client, "append_to_mailbox", mock_append):
                with pytest.raises(RuntimeError, match="Failed to save email"):
                    await handler.save_to_mailbox(
                        recipients=["r@example.com"],
                        subject="Fail",
                        body="body",
                        mailbox="Nonexistent",
                    )

    @pytest.mark.asyncio
    async def test_save_rejects_invalid_flags(self, email_settings):
        handler = ClassicEmailHandler(email_settings)
        with pytest.raises(ValueError, match="Invalid IMAP flag"):
            await handler.save_to_mailbox(
                recipients=["r@example.com"],
                subject="Bad flags",
                body="body",
                flags=[r"\Seen) {9999}"],
            )


class TestSaveToMailboxBcc:
    """Tests that save_to_mailbox preserves BCC in the saved message."""

    @pytest.mark.asyncio
    async def test_save_to_mailbox_includes_bcc_header(self, email_settings):
        handler = ClassicEmailHandler(email_settings)
        mock_append = AsyncMock(return_value="42")

        # Don't mock compose_message — let it run for real so we verify
        # include_bcc_header=True is actually passed and produces a Bcc header
        with patch.object(handler.outgoing_client, "append_to_mailbox", mock_append):
            await handler.save_to_mailbox(
                recipients=["r@example.com"],
                subject="Draft",
                body="draft body",
                bcc=["secret@example.com"],
            )

        appended_msg = mock_append.call_args[0][0]
        assert appended_msg["Bcc"] == "secret@example.com"


# ---------------------------------------------------------------------------
# Cycle 4: MCP tool
# ---------------------------------------------------------------------------


class TestSaveToMailboxTool:
    """Tests for the save_to_mailbox MCP tool in app.py."""

    @pytest.mark.asyncio
    async def test_save_to_mailbox_tool_success(self, monkeypatch):
        mock_handler = AsyncMock()
        mock_handler.save_to_mailbox = AsyncMock(return_value="<msg-id@example.com>|uid:42")
        monkeypatch.setattr("mcp_email_server.app.dispatch_handler", lambda _: mock_handler)

        from mcp_email_server.app import save_to_mailbox

        result = await save_to_mailbox(
            account_name="test",
            recipients=["r@example.com"],
            subject="Draft",
            body="body",
        )
        assert "Drafts" in result
        assert "<msg-id@example.com>" in result
        assert "email_id: 42" in result

    @pytest.mark.asyncio
    async def test_save_to_mailbox_tool_custom_folder(self, monkeypatch):
        mock_handler = AsyncMock()
        mock_handler.save_to_mailbox = AsyncMock(return_value="<msg-id@example.com>|uid:99")
        monkeypatch.setattr("mcp_email_server.app.dispatch_handler", lambda _: mock_handler)

        from mcp_email_server.app import save_to_mailbox

        result = await save_to_mailbox(
            account_name="test",
            recipients=["r@example.com"],
            subject="Draft",
            body="body",
            mailbox="INBOX.Drafts",
            flags=[r"\Draft", r"\Seen"],
        )
        assert "INBOX.Drafts" in result
        mock_handler.save_to_mailbox.assert_called_once_with(
            ["r@example.com"],
            "Draft",
            "body",
            "INBOX.Drafts",
            None,
            None,
            False,
            None,
            None,
            None,
            [r"\Draft", r"\Seen"],
        )
