"""Tests for the save_to_mailbox feature — IMAP APPEND to arbitrary folders."""

import asyncio
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient


# ---------------------------------------------------------------------------
# Cycle 1: _compose_message
# ---------------------------------------------------------------------------


class TestComposeMessage:
    """Tests for EmailClient._compose_message — extracted message composition."""

    @pytest.fixture
    def email_client(self):
        server = EmailServer(
            user_name="test_user",
            password="test_password",
            host="smtp.example.com",
            port=465,
            use_ssl=True,
        )
        return EmailClient(server, sender="Test User <test@example.com>")

    def test_plain_text_message(self, email_client):
        msg = email_client._compose_message(
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
        msg = email_client._compose_message(
            recipients=["r@example.com"],
            subject="HTML",
            body="<b>bold</b>",
            html=True,
        )
        assert msg.get_content_type() == "text/html"

    def test_cc_header(self, email_client):
        msg = email_client._compose_message(
            recipients=["r@example.com"],
            subject="CC",
            body="body",
            cc=["cc1@example.com", "cc2@example.com"],
        )
        assert "cc1@example.com" in msg["Cc"]
        assert "cc2@example.com" in msg["Cc"]

    def test_bcc_not_in_headers(self, email_client):
        msg = email_client._compose_message(
            recipients=["r@example.com"],
            subject="BCC",
            body="body",
            bcc=["secret@example.com"],
        )
        assert msg["Bcc"] is None  # BCC must not appear in headers

    def test_threading_headers(self, email_client):
        msg = email_client._compose_message(
            recipients=["r@example.com"],
            subject="Re: Thread",
            body="reply",
            in_reply_to="<original@example.com>",
            references="<original@example.com>",
        )
        assert msg["In-Reply-To"] == "<original@example.com>"
        assert msg["References"] == "<original@example.com>"

    def test_unicode_subject(self, email_client):
        msg = email_client._compose_message(
            recipients=["r@example.com"],
            subject="Tesüöä",
            body="body",
        )
        # Should not raise; subject is encoded via Header
        assert msg["Subject"] is not None

    def test_with_attachments(self, email_client, tmp_path):
        test_file = tmp_path / "doc.txt"
        test_file.write_text("file content")
        msg = email_client._compose_message(
            recipients=["r@example.com"],
            subject="Attach",
            body="see attached",
            attachments=[str(test_file)],
        )
        assert msg.get_content_type() == "multipart/mixed"


# ---------------------------------------------------------------------------
# Cycle 2: append_to_mailbox
# ---------------------------------------------------------------------------


class TestAppendToMailbox:
    """Tests for EmailClient.append_to_mailbox — IMAP APPEND to a specific folder."""

    @pytest.fixture
    def email_client(self):
        server = EmailServer(
            user_name="test_user",
            password="test_password",
            host="smtp.example.com",
            port=465,
            use_ssl=True,
        )
        return EmailClient(server)

    @pytest.fixture
    def incoming_server(self):
        return EmailServer(
            user_name="test_user",
            password="test_password",
            host="imap.example.com",
            port=993,
            use_ssl=True,
        )

    @pytest.fixture
    def mock_imap(self):
        mock = AsyncMock()
        mock._client_task = asyncio.Future()
        mock._client_task.set_result(None)
        mock.wait_hello_from_server = AsyncMock()
        mock.login = AsyncMock()
        mock.select = AsyncMock(return_value=("OK", []))
        mock.append = AsyncMock(return_value=("OK", []))
        mock.logout = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_append_success(self, email_client, incoming_server, mock_imap):
        msg = MIMEText("Draft body")
        msg["Subject"] = "Draft"
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Drafts")
        assert result is True
        mock_imap.select.assert_called_with('"Drafts"')
        mock_imap.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_with_custom_flags(self, email_client, incoming_server, mock_imap):
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            await email_client.append_to_mailbox(
                msg, incoming_server, "Templates", flags=r"(\Seen \Flagged)"
            )
        _, kwargs = mock_imap.append.call_args
        assert kwargs["flags"] == r"(\Seen \Flagged)"

    @pytest.mark.asyncio
    async def test_append_folder_not_found(self, email_client, incoming_server, mock_imap):
        mock_imap.select = AsyncMock(return_value=("NO", []))
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_append_login_failure(self, email_client, incoming_server, mock_imap):
        mock_imap.login = AsyncMock(side_effect=Exception("Auth failed"))
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Drafts")
        assert result is False

    @pytest.mark.asyncio
    async def test_append_imap_append_fails(self, email_client, incoming_server, mock_imap):
        mock_imap.append = AsyncMock(return_value=("NO", [b"APPEND failed"]))
        msg = MIMEText("body")
        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_lib:
            mock_lib.IMAP4_SSL.return_value = mock_imap
            result = await email_client.append_to_mailbox(msg, incoming_server, "Drafts")
        assert result is False

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
        assert result is True
        mock_lib.IMAP4.assert_called_once()
