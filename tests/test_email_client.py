import asyncio
import email
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient, _create_smtp_ssl_context


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
        before_date = datetime(2023, 1, 1, tzinfo=timezone.utc)
        criteria = EmailClient._build_search_criteria(before=before_date)
        assert criteria == ["BEFORE", "01-JAN-2023"]

        # Test with since date
        since_date = datetime(2023, 1, 1, tzinfo=timezone.utc)
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
            subject="Test", from_address="test@example.com", since=datetime(2023, 1, 1, tzinfo=timezone.utc)
        )
        assert criteria == ["SINCE", "01-JAN-2023", "SUBJECT", "Test", "FROM", "test@example.com"]

        # Test with seen=True (read emails)
        criteria = EmailClient._build_search_criteria(seen=True)
        assert criteria == ["SEEN"]

        # Test with seen=False (unread emails)
        criteria = EmailClient._build_search_criteria(seen=False)
        assert criteria == ["UNSEEN"]

        # Test with seen=None (all emails - no criteria added)
        criteria = EmailClient._build_search_criteria(seen=None)
        assert criteria == ["ALL"]

        # Test with flagged=True (starred emails)
        criteria = EmailClient._build_search_criteria(flagged=True)
        assert criteria == ["FLAGGED"]

        # Test with flagged=False (non-starred emails)
        criteria = EmailClient._build_search_criteria(flagged=False)
        assert criteria == ["UNFLAGGED"]

        # Test with answered=True (replied emails)
        criteria = EmailClient._build_search_criteria(answered=True)
        assert criteria == ["ANSWERED"]

        # Test with answered=False (not replied emails)
        criteria = EmailClient._build_search_criteria(answered=False)
        assert criteria == ["UNANSWERED"]

        # Test compound criteria: unread emails from a specific sender
        criteria = EmailClient._build_search_criteria(seen=False, from_address="sender@example.com")
        assert "UNSEEN" in criteria
        assert "FROM" in criteria
        assert "sender@example.com" in criteria

        # Test compound criteria: flagged and answered
        criteria = EmailClient._build_search_criteria(flagged=True, answered=True)
        assert "FLAGGED" in criteria
        assert "ANSWERED" in criteria

        # Test compound criteria: unread, flagged, from specific sender, with subject
        criteria = EmailClient._build_search_criteria(
            seen=False, flagged=True, from_address="test@example.com", subject="Important"
        )
        assert "UNSEEN" in criteria
        assert "FLAGGED" in criteria
        assert "FROM" in criteria
        assert "test@example.com" in criteria
        assert "SUBJECT" in criteria
        assert "Important" in criteria

    @pytest.mark.asyncio
    async def test_get_emails_stream(self, email_client):
        """Test getting emails stream returns sorted, paginated results."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock()
        mock_imap.uid_search = AsyncMock(return_value=(None, [b"1 2 3"]))
        mock_imap.logout = AsyncMock()

        # Mock at the helper level - test behavior, not implementation
        mock_dates = {
            "1": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "2": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "3": datetime(2024, 1, 3, tzinfo=timezone.utc),
        }
        mock_metadata = {
            "1": {
                "email_id": "1",
                "subject": "Subject 1",
                "from": "a@test.com",
                "to": [],
                "date": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "attachments": [],
            },
            "2": {
                "email_id": "2",
                "subject": "Subject 2",
                "from": "b@test.com",
                "to": [],
                "date": datetime(2024, 1, 2, tzinfo=timezone.utc),
                "attachments": [],
            },
            "3": {
                "email_id": "3",
                "subject": "Subject 3",
                "from": "c@test.com",
                "to": [],
                "date": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "attachments": [],
            },
        }

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_dates", return_value=mock_dates) as mock_fetch_dates:
                with patch.object(
                    email_client, "_batch_fetch_headers", return_value=mock_metadata
                ) as mock_fetch_headers:
                    emails = []
                    async for email_data in email_client.get_emails_metadata_stream(page=1, page_size=10):
                        emails.append(email_data)

                    # Behavior: returns emails sorted by date desc (newest first)
                    assert len(emails) == 3
                    assert emails[0]["subject"] == "Subject 3"
                    assert emails[1]["subject"] == "Subject 2"
                    assert emails[2]["subject"] == "Subject 1"

                    mock_imap.login.assert_called_once()
                    mock_imap.logout.assert_called_once()

                    # Verify helpers called with correct arguments
                    mock_fetch_dates.assert_called_once_with(mock_imap, [b"1", b"2", b"3"])
                    # Headers fetched for page UIDs in sorted order (desc by date)
                    mock_fetch_headers.assert_called_once_with(mock_imap, ["3", "2", "1"])

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
        mock_imap.uid_search = AsyncMock(return_value=(None, [b"1 2 3 4 5"]))
        mock_imap.logout = AsyncMock()

        # Mock IMAP class
        with patch.object(email_client, "imap_class", return_value=mock_imap):
            count = await email_client.get_email_count()

            assert count == 5

            # Verify IMAP methods were called correctly
            mock_imap.login.assert_called_once_with(
                email_client.email_server.user_name, email_client.email_server.password
            )
            mock_imap.select.assert_called_once_with('"INBOX"')
            mock_imap.uid_search.assert_called_once_with("ALL")
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


class TestParseEmailData:
    def test_parse_email_extracts_message_id(self, email_client):
        """Test that Message-ID header is extracted during parsing."""
        raw_email = b"""Message-ID: <test123@example.com>
From: sender@example.com
To: recipient@example.com
Subject: Test Subject
Date: Mon, 1 Jan 2024 12:00:00 +0000

Test body content
"""
        result = email_client._parse_email_data(raw_email, email_id="1")
        assert result["message_id"] == "<test123@example.com>"

    def test_parse_email_handles_missing_message_id(self, email_client):
        """Test graceful handling when Message-ID is missing."""
        raw_email = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Subject
Date: Mon, 1 Jan 2024 12:00:00 +0000

Test body content
"""
        result = email_client._parse_email_data(raw_email, email_id="1")
        assert result["message_id"] is None


class TestSendEmailReplyHeaders:
    @pytest.mark.asyncio
    async def test_send_email_sets_in_reply_to_header(self, email_client):
        """Test that In-Reply-To header is set when provided."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Re: Test",
                body="Reply body",
                in_reply_to="<original123@example.com>",
            )

            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert msg["In-Reply-To"] == "<original123@example.com>"

    @pytest.mark.asyncio
    async def test_send_email_sets_references_header(self, email_client):
        """Test that References header is set when provided."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Re: Test",
                body="Reply body",
                references="<first@example.com> <second@example.com>",
            )

            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert msg["References"] == "<first@example.com> <second@example.com>"

    @pytest.mark.asyncio
    async def test_send_email_without_reply_headers(self, email_client):
        """Test that send works without reply headers (backward compatibility)."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test",
                body="Body",
            )

            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert "In-Reply-To" not in msg
            assert "References" not in msg


class TestSmtpSslContext:
    """Tests for SMTP SSL context creation."""

    def test_create_smtp_ssl_context_with_verification(self):
        """When verify_ssl=True, should return None (use default verification)."""
        result = _create_smtp_ssl_context(verify_ssl=True)
        assert result is None

    def test_create_smtp_ssl_context_without_verification(self):
        """When verify_ssl=False, should return permissive SSL context."""
        result = _create_smtp_ssl_context(verify_ssl=False)

        assert result is not None
        assert isinstance(result, ssl.SSLContext)
        assert result.check_hostname is False
        assert result.verify_mode == ssl.CERT_NONE

    def test_email_client_get_smtp_ssl_context_default(self):
        """EmailClient should use verify_ssl from EmailServer (default True)."""
        server = EmailServer(
            user_name="test",
            password="test",
            host="smtp.example.com",
            port=587,
        )
        client = EmailClient(server)

        # Default verify_ssl is True, so should return None
        assert client.smtp_verify_ssl is True
        assert client._get_smtp_ssl_context() is None

    def test_email_client_get_smtp_ssl_context_disabled(self):
        """EmailClient should return permissive context when verify_ssl=False."""
        server = EmailServer(
            user_name="test",
            password="test",
            host="smtp.example.com",
            port=587,
            verify_ssl=False,
        )
        client = EmailClient(server)

        assert client.smtp_verify_ssl is False
        ctx = client._get_smtp_ssl_context()
        assert ctx is not None
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    @pytest.mark.asyncio
    async def test_send_email_passes_tls_context(self):
        """send_email should pass tls_context to SMTP connection."""
        server = EmailServer(
            user_name="test",
            password="test",
            host="smtp.example.com",
            port=587,
            verify_ssl=False,
        )
        client = EmailClient(server, sender="test@example.com")

        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp) as mock_smtp_class:
            await client.send_email(
                recipients=["recipient@example.com"],
                subject="Test",
                body="Body",
            )

            # Verify SMTP was called with tls_context
            call_kwargs = mock_smtp_class.call_args.kwargs
            assert "tls_context" in call_kwargs
            ctx = call_kwargs["tls_context"]
            assert ctx is not None
            assert ctx.check_hostname is False
            assert ctx.verify_mode == ssl.CERT_NONE


class TestParseHeaders:
    def test_parse_headers_extracts_metadata(self, email_client):
        """Test that _parse_headers correctly extracts email metadata."""
        raw_headers = b"""From: sender@example.com
To: recipient@example.com, other@example.com
Cc: cc@example.com
Subject: Test Subject
Date: Mon, 1 Jan 2024 12:00:00 +0000

"""
        result = email_client._parse_headers("123", raw_headers)

        assert result["email_id"] == "123"
        assert result["subject"] == "Test Subject"
        assert result["from"] == "sender@example.com"
        assert "recipient@example.com" in result["to"]
        assert "cc@example.com" in result["to"]
        assert result["attachments"] == []

    def test_parse_headers_handles_missing_fields(self, email_client):
        """Test that _parse_headers handles emails with missing headers."""
        raw_headers = b"""Subject: Minimal Email

"""
        result = email_client._parse_headers("456", raw_headers)

        assert result["email_id"] == "456"
        assert result["subject"] == "Minimal Email"
        assert result["from"] == ""
        assert result["to"] == []


class TestBatchFetchDates:
    @pytest.mark.asyncio
    async def test_batch_fetch_dates_parses_imap_response(self, email_client):
        """Test that _batch_fetch_dates correctly parses IMAP INTERNALDATE responses."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b'1 FETCH (UID 100 INTERNALDATE "01-Jan-2024 12:00:00 +0000")',
                    b'2 FETCH (UID 200 INTERNALDATE "02-Jan-2024 12:00:00 +0000")',
                    b"FETCH completed",
                ],
            )
        )

        result = await email_client._batch_fetch_dates(mock_imap, [b"100", b"200"])

        assert len(result) == 2
        assert "100" in result
        assert "200" in result
        assert result["100"].day == 1
        assert result["200"].day == 2

    @pytest.mark.asyncio
    async def test_batch_fetch_dates_empty_input(self, email_client):
        """Test that _batch_fetch_dates returns empty dict for empty input."""
        mock_imap = AsyncMock()
        result = await email_client._batch_fetch_dates(mock_imap, [])
        assert result == {}
        mock_imap.uid.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_fetch_dates_handles_fastmail_format(self, email_client):
        """Test that _batch_fetch_dates handles space-padded dates (Fastmail)."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b'1 FETCH (UID 100 INTERNALDATE " 1-Jan-2024 12:00:00 +0000")',
                    b"FETCH completed",
                ],
            )
        )

        result = await email_client._batch_fetch_dates(mock_imap, [b"100"])

        assert result["100"] == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestBatchFetchHeaders:
    @pytest.mark.asyncio
    async def test_batch_fetch_headers_parses_imap_response(self, email_client):
        """Test that _batch_fetch_headers correctly parses IMAP header responses."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b"1 FETCH (UID 100 BODY[HEADER] {50}",
                    bytearray(b"From: a@test.com\r\nSubject: Test\r\n\r\n"),
                    b")",
                    b"FETCH completed",
                ],
            )
        )

        result = await email_client._batch_fetch_headers(mock_imap, ["100"])

        assert "100" in result
        assert result["100"]["subject"] == "Test"
        assert result["100"]["from"] == "a@test.com"

    @pytest.mark.asyncio
    async def test_batch_fetch_headers_empty_input(self, email_client):
        """Test that _batch_fetch_headers returns empty dict for empty input."""
        mock_imap = AsyncMock()
        result = await email_client._batch_fetch_headers(mock_imap, [])
        assert result == {}
        mock_imap.uid.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_fetch_headers_preserves_uid_mapping(self, email_client):
        """Test that _batch_fetch_headers returns dict keyed by UID."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b"1 FETCH (UID 100 BODY[HEADER] {50}",
                    bytearray(b"From: a@test.com\r\nSubject: First\r\n\r\n"),
                    b")",
                    b"2 FETCH (UID 200 BODY[HEADER] {50}",
                    bytearray(b"From: b@test.com\r\nSubject: Second\r\n\r\n"),
                    b")",
                    b"FETCH completed",
                ],
            )
        )

        result = await email_client._batch_fetch_headers(mock_imap, ["100", "200"])

        assert len(result) == 2
        assert result["100"]["subject"] == "First"
        assert result["200"]["subject"] == "Second"
