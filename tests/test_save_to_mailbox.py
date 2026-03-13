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
