import asyncio
import email
from datetime import datetime
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient


@pytest.fixture
def email_server():
    return EmailServer(
        user_name="test_user",
        password="test_password",
        host="imap.example.com",
        port=993,
        use_ssl=True,
    )


@pytest.fixture
def email_client(email_server):
    return EmailClient(email_server, sender="Test User <test@example.com>")


class TestEmailClient:
    def test_init(self, email_server):
        """Test initialization of EmailClient."""
        client = EmailClient(email_server)
        assert client.email_server == email_server
        assert client.sender == email_server.user_name
        assert client.smtp_use_tls is True
        assert client.smtp_start_tls is False

        # Test with custom sender
        custom_sender = "Custom <custom@example.com>"
        client = EmailClient(email_server, sender=custom_sender)
        assert client.sender == custom_sender

    def test_parse_email_data_plain(self):
        """Test parsing plain text email."""
        # Create a simple plain text email
        msg = MIMEText("This is a test email body")
        msg["Subject"] = "Test Subject"
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Date"] = email.utils.formatdate()

        raw_email = msg.as_bytes()

        client = EmailClient(MagicMock())
        result = client._parse_email_data(raw_email)

        assert result["subject"] == "Test Subject"
        assert result["from"] == "sender@example.com"
        assert result["body"] == "This is a test email body"
        assert isinstance(result["date"], datetime)
        assert result["attachments"] == []

    def test_parse_email_data_with_attachments(self):
        """Test parsing email with attachments."""
        # This would require creating a multipart email with attachments
        # For simplicity, we'll mock the email parsing
        with patch("email.parser.BytesParser.parsebytes") as mock_parse:
            mock_email = MagicMock()
            mock_email.get.side_effect = lambda x, default=None: {
                "Subject": "Test Subject",
                "From": "sender@example.com",
                "Date": email.utils.formatdate(),
            }.get(x, default)
            mock_email.is_multipart.return_value = True

            # Mock parts
            text_part = MagicMock()
            text_part.get_content_type.return_value = "text/plain"
            text_part.get.return_value = ""  # Not an attachment
            text_part.get_payload.return_value = b"This is the email body"
            text_part.get_content_charset.return_value = "utf-8"

            attachment_part = MagicMock()
            attachment_part.get_content_type.return_value = "application/pdf"
            attachment_part.get.return_value = "attachment; filename=test.pdf"
            attachment_part.get_filename.return_value = "test.pdf"

            mock_email.walk.return_value = [text_part, attachment_part]
            mock_parse.return_value = mock_email

            client = EmailClient(MagicMock())
            result = client._parse_email_data(b"dummy email content")

            assert result["subject"] == "Test Subject"
            assert result["from"] == "sender@example.com"
            assert result["body"] == "This is the email body"
            assert isinstance(result["date"], datetime)
            assert result["attachments"] == ["test.pdf"]

    def test_build_search_criteria(self):
        """Test building search criteria for IMAP."""
        # Test with no criteria (should return ["ALL"])
        criteria = EmailClient._build_search_criteria()
        assert criteria == ["ALL"]

        # Test with before date
        before_date = datetime(2023, 1, 1)
        criteria = EmailClient._build_search_criteria(before=before_date)
        assert criteria == ["BEFORE", "01-JAN-2023"]

        # Test with since date
        since_date = datetime(2023, 1, 1)
        criteria = EmailClient._build_search_criteria(since=since_date)
        assert criteria == ["SINCE", "01-JAN-2023"]

        # Test with subject
        criteria = EmailClient._build_search_criteria(subject="Test")
        assert criteria == ["SUBJECT", "Test"]

        # Test with body
        criteria = EmailClient._build_search_criteria(body="Test")
        assert criteria == ["BODY", "Test"]

        # Test with text
        criteria = EmailClient._build_search_criteria(text="Test")
        assert criteria == ["TEXT", "Test"]

        # Test with from_address
        criteria = EmailClient._build_search_criteria(from_address="test@example.com")
        assert criteria == ["FROM", "test@example.com"]

        # Test with to_address
        criteria = EmailClient._build_search_criteria(to_address="test@example.com")
        assert criteria == ["TO", "test@example.com"]

        # Test with multiple criteria
        criteria = EmailClient._build_search_criteria(
            subject="Test", from_address="test@example.com", since=datetime(2023, 1, 1)
        )
        assert criteria == ["SINCE", "01-JAN-2023", "SUBJECT", "Test", "FROM", "test@example.com"]

    @pytest.mark.asyncio
    async def test_get_emails_stream(self, email_client):
        """Test getting emails stream."""
        # Mock IMAP client
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock()
        mock_imap.search = AsyncMock(return_value=(None, [b"1 2 3"]))
        mock_imap.fetch = AsyncMock(return_value=(None, [b"HEADER", bytearray(b"EMAIL CONTENT")]))
        mock_imap.logout = AsyncMock()

        # Mock IMAP class
        with patch.object(email_client, "imap_class", return_value=mock_imap):
            # Mock _parse_email_data
            with patch.object(email_client, "_parse_email_data") as mock_parse:
                mock_parse.return_value = {
                    "subject": "Test Subject",
                    "from": "sender@example.com",
                    "body": "Test Body",
                    "date": datetime.now(),
                    "attachments": [],
                }

                emails = []
                async for email_data in email_client.get_emails_stream(page=1, page_size=10):
                    emails.append(email_data)

                # We should get 3 emails (from the mocked search result "1 2 3")
                assert len(emails) == 3
                assert emails[0]["subject"] == "Test Subject"
                assert emails[0]["from"] == "sender@example.com"

                # Verify IMAP methods were called correctly
                mock_imap.login.assert_called_once_with(
                    email_client.email_server.user_name, email_client.email_server.password
                )
                mock_imap.select.assert_called_once_with("INBOX")
                mock_imap.search.assert_called_once_with("ALL")
                assert mock_imap.fetch.call_count == 3
                mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_email_count(self, email_client):
        """Test getting email count."""
        # Mock IMAP client
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock()
        mock_imap.search = AsyncMock(return_value=(None, [b"1 2 3 4 5"]))
        mock_imap.logout = AsyncMock()

        # Mock IMAP class
        with patch.object(email_client, "imap_class", return_value=mock_imap):
            count = await email_client.get_email_count()

            assert count == 5

            # Verify IMAP methods were called correctly
            mock_imap.login.assert_called_once_with(
                email_client.email_server.user_name, email_client.email_server.password
            )
            mock_imap.select.assert_called_once_with("INBOX")
            mock_imap.search.assert_called_once_with("ALL")
            mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_email(self, email_client):
        """Test sending email."""
        # Mock SMTP client
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
                cc=["cc@example.com"],
                bcc=["bcc@example.com"],
            )

            # Verify SMTP methods were called correctly
            mock_smtp.login.assert_called_once_with(
                email_client.email_server.user_name, email_client.email_server.password
            )
            mock_smtp.send_message.assert_called_once()

            # Check that the message was constructed correctly
            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            recipients = call_args[1]["recipients"]

            assert msg["Subject"] == "Test Subject"
            assert msg["From"] == email_client.sender
            assert msg["To"] == "recipient@example.com"
            assert msg["Cc"] == "cc@example.com"
            assert "Bcc" not in msg  # BCC should not be in headers

            # Check that all recipients are included in the SMTP call
            assert "recipient@example.com" in recipients
            assert "cc@example.com" in recipients
            assert "bcc@example.com" in recipients
