from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailBodyResponse,
    EmailContentBatchResponse,
    EmailMetadata,
    EmailMetadataPageResponse,
    ForwardEmailResponse,
)


@pytest.fixture
def email_settings():
    return EmailSettings(
        account_name="test_account",
        full_name="Test User",
        email_address="test@example.com",
        incoming=EmailServer(
            user_name="test_user",
            password="test_password",
            host="imap.example.com",
            port=993,
            use_ssl=True,
        ),
        outgoing=EmailServer(
            user_name="test_user",
            password="test_password",
            host="smtp.example.com",
            port=465,
            use_ssl=True,
        ),
    )


@pytest.fixture
def classic_handler(email_settings):
    return ClassicEmailHandler(email_settings)


class TestClassicEmailHandler:
    def test_init(self, email_settings):
        """Test initialization of ClassicEmailHandler."""
        handler = ClassicEmailHandler(email_settings)

        assert handler.email_settings == email_settings
        assert isinstance(handler.incoming_client, EmailClient)
        assert isinstance(handler.outgoing_client, EmailClient)

        # Check that clients are initialized correctly
        assert handler.incoming_client.email_server == email_settings.incoming
        assert handler.outgoing_client.email_server == email_settings.outgoing
        assert handler.outgoing_client.sender == f"{email_settings.full_name} <{email_settings.email_address}>"

    @pytest.mark.asyncio
    async def test_get_emails(self, classic_handler):
        """Test get_emails method."""
        # Create test data
        now = datetime.now(timezone.utc)
        email_data = {
            "email_id": "123",
            "subject": "Test Subject",
            "from": "sender@example.com",
            "to": ["recipient@example.com"],
            "date": now,
            "attachments": [],
        }

        # Mock the get_emails_stream method to yield our test data
        mock_stream = AsyncMock()
        mock_stream.__aiter__.return_value = [email_data]

        # Mock the get_email_count method
        mock_count = AsyncMock(return_value=1)

        # Apply the mocks
        with patch.object(classic_handler.incoming_client, "get_emails_metadata_stream", return_value=mock_stream):
            with patch.object(classic_handler.incoming_client, "get_email_count", mock_count):
                # Call the method
                result = await classic_handler.get_emails_metadata(
                    page=1,
                    page_size=10,
                    before=now,
                    since=None,
                    subject="Test",
                    from_address="sender@example.com",
                    to_address=None,
                )

                # Verify the result
                assert isinstance(result, EmailMetadataPageResponse)
                assert result.page == 1
                assert result.page_size == 10
                assert result.before == now
                assert result.since is None
                assert result.subject == "Test"
                assert len(result.emails) == 1
                assert isinstance(result.emails[0], EmailMetadata)
                assert result.emails[0].subject == "Test Subject"
                assert result.emails[0].sender == "sender@example.com"
                assert result.emails[0].date == now
                assert result.emails[0].attachments == []
                assert result.total == 1

                # Verify the client methods were called correctly
                classic_handler.incoming_client.get_emails_metadata_stream.assert_called_once_with(
                    1, 10, now, None, "Test", "sender@example.com", None, "desc", "INBOX"
                )
                mock_count.assert_called_once_with(
                    now, None, "Test", from_address="sender@example.com", to_address=None, mailbox="INBOX"
                )

    @pytest.mark.asyncio
    async def test_get_emails_with_mailbox(self, classic_handler):
        """Test get_emails method with custom mailbox."""
        now = datetime.now(timezone.utc)
        email_data = {
            "email_id": "456",
            "subject": "Sent Mail Subject",
            "from": "me@example.com",
            "to": ["recipient@example.com"],
            "date": now,
            "attachments": [],
        }

        mock_stream = AsyncMock()
        mock_stream.__aiter__.return_value = [email_data]
        mock_count = AsyncMock(return_value=1)

        with patch.object(classic_handler.incoming_client, "get_emails_metadata_stream", return_value=mock_stream):
            with patch.object(classic_handler.incoming_client, "get_email_count", mock_count):
                result = await classic_handler.get_emails_metadata(
                    page=1,
                    page_size=10,
                    mailbox="Sent",
                )

                assert isinstance(result, EmailMetadataPageResponse)
                assert len(result.emails) == 1

                # Verify mailbox parameter was passed correctly
                classic_handler.incoming_client.get_emails_metadata_stream.assert_called_once_with(
                    1, 10, None, None, None, None, None, "desc", "Sent"
                )
                mock_count.assert_called_once_with(None, None, None, from_address=None, to_address=None, mailbox="Sent")

    @pytest.mark.asyncio
    async def test_send_email(self, classic_handler):
        """Test send_email method."""
        # Mock the outgoing_client.send_email method
        mock_send = AsyncMock()

        # Apply the mock
        with patch.object(classic_handler.outgoing_client, "send_email", mock_send):
            # Call the method
            await classic_handler.send_email(
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
                cc=["cc@example.com"],
                bcc=["bcc@example.com"],
            )

            # Verify the client method was called correctly
            mock_send.assert_called_once_with(
                ["recipient@example.com"],
                "Test Subject",
                "Test Body",
                ["cc@example.com"],
                ["bcc@example.com"],
                False,
                None,
                None,
                None,
            )

    @pytest.mark.asyncio
    async def test_send_email_with_attachments(self, classic_handler, tmp_path):
        """Test send_email method with attachments."""
        # Create a temporary test file
        test_file = tmp_path / "test_attachment.txt"
        test_file.write_text("This is a test attachment")

        # Mock the outgoing_client.send_email method
        mock_send = AsyncMock()

        # Apply the mock
        with patch.object(classic_handler.outgoing_client, "send_email", mock_send):
            # Call the method with attachments
            await classic_handler.send_email(
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body with attachment",
                attachments=[str(test_file)],
            )

            # Verify the client method was called correctly with attachments
            mock_send.assert_called_once_with(
                ["recipient@example.com"],
                "Test Subject",
                "Test Body with attachment",
                None,
                None,
                False,
                [str(test_file)],
                None,
                None,
            )

    @pytest.mark.asyncio
    async def test_delete_emails(self, classic_handler):
        """Test delete_emails method."""
        mock_delete = AsyncMock(return_value=(["123", "456"], []))

        with patch.object(classic_handler.incoming_client, "delete_emails", mock_delete):
            deleted_ids, failed_ids = await classic_handler.delete_emails(
                email_ids=["123", "456"],
                mailbox="INBOX",
            )

            assert deleted_ids == ["123", "456"]
            assert failed_ids == []
            mock_delete.assert_called_once_with(["123", "456"], "INBOX")

    @pytest.mark.asyncio
    async def test_delete_emails_with_failures(self, classic_handler):
        """Test delete_emails method with some failures."""
        mock_delete = AsyncMock(return_value=(["123"], ["456"]))

        with patch.object(classic_handler.incoming_client, "delete_emails", mock_delete):
            deleted_ids, failed_ids = await classic_handler.delete_emails(
                email_ids=["123", "456"],
                mailbox="Trash",
            )

            assert deleted_ids == ["123"]
            assert failed_ids == ["456"]
            mock_delete.assert_called_once_with(["123", "456"], "Trash")

    @pytest.mark.asyncio
    async def test_delete_emails_custom_mailbox(self, classic_handler):
        """Test delete_emails method with custom mailbox."""
        mock_delete = AsyncMock(return_value=(["789"], []))

        with patch.object(classic_handler.incoming_client, "delete_emails", mock_delete):
            deleted_ids, failed_ids = await classic_handler.delete_emails(
                email_ids=["789"],
                mailbox="Archive",
            )

            assert deleted_ids == ["789"]
            assert failed_ids == []
            mock_delete.assert_called_once_with(["789"], "Archive")

    @pytest.mark.asyncio
    async def test_download_attachment(self, classic_handler, tmp_path):
        """Test download_attachment method."""
        save_path = str(tmp_path / "downloaded_attachment.pdf")

        mock_result = {
            "email_id": "123",
            "attachment_name": "document.pdf",
            "mime_type": "application/pdf",
            "size": 1024,
            "saved_path": save_path,
        }

        mock_download = AsyncMock(return_value=mock_result)

        with patch.object(classic_handler.incoming_client, "download_attachment", mock_download):
            result = await classic_handler.download_attachment(
                email_id="123",
                attachment_name="document.pdf",
                save_path=save_path,
            )

            assert isinstance(result, AttachmentDownloadResponse)
            assert result.email_id == "123"
            assert result.attachment_name == "document.pdf"
            assert result.mime_type == "application/pdf"
            assert result.size == 1024
            assert result.saved_path == save_path

            mock_download.assert_called_once_with("123", "document.pdf", save_path, "INBOX")

    @pytest.mark.asyncio
    async def test_send_email_with_reply_headers(self, classic_handler):
        """Test sending email with reply headers."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await classic_handler.send_email(
                recipients=["recipient@example.com"],
                subject="Re: Test",
                body="Reply body",
                in_reply_to="<original@example.com>",
                references="<original@example.com>",
            )

            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert msg["In-Reply-To"] == "<original@example.com>"
            assert msg["References"] == "<original@example.com>"

    @pytest.mark.asyncio
    async def test_get_emails_content_includes_message_id(self, classic_handler):
        """Test that get_emails_content returns message_id from parsed email data."""
        now = datetime.now(timezone.utc)
        email_data = {
            "email_id": "123",
            "message_id": "<test-message-id@example.com>",
            "subject": "Test Subject",
            "from": "sender@example.com",
            "to": ["recipient@example.com"],
            "date": now,
            "body": "Test email body",
            "attachments": [],
        }

        # Mock the get_email_body_by_id method to return our test data
        mock_get_body = AsyncMock(return_value=email_data)

        with patch.object(classic_handler.incoming_client, "get_email_body_by_id", mock_get_body):
            result = await classic_handler.get_emails_content(
                email_ids=["123"],
                mailbox="INBOX",
            )

            # Verify the result
            assert isinstance(result, EmailContentBatchResponse)
            assert len(result.emails) == 1
            assert isinstance(result.emails[0], EmailBodyResponse)
            assert result.emails[0].email_id == "123"
            assert result.emails[0].message_id == "<test-message-id@example.com>"
            assert result.emails[0].subject == "Test Subject"
            assert result.emails[0].sender == "sender@example.com"
            assert result.emails[0].body == "Test email body"

            # Verify the client method was called correctly
            mock_get_body.assert_called_once_with("123", "INBOX")

    @pytest.mark.asyncio
    async def test_forward_email(self, classic_handler):
        """Test forward_email method."""
        original_email = {
            "email_id": "123",
            "subject": "Test Subject",
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "cc": "",
            "date": "Mon, 1 Jan 2024 12:00:00 +0000",
            "body": "Original email body",
            "attachment_parts": [],
        }

        mock_get_forward = AsyncMock(return_value=original_email)
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            with patch("aiosmtplib.SMTP", return_value=mock_smtp):
                result = await classic_handler.forward_email(
                    email_id="123",
                    recipients=["forward@example.com"],
                    additional_message="FYI",
                )

                assert isinstance(result, ForwardEmailResponse)
                assert result.original_email_id == "123"
                assert result.forwarded_to == ["forward@example.com"]
                assert result.subject == "Fwd: Test Subject"
                assert result.success is True
                mock_get_forward.assert_called_once_with("123", "INBOX")

    @pytest.mark.asyncio
    async def test_forward_email_with_custom_sender(self, classic_handler):
        """Test forward_email method with custom sender address."""
        original_email = {
            "email_id": "456",
            "subject": "Important Message",
            "from": "original@example.com",
            "to": "me@example.com",
            "cc": "",
            "date": "Tue, 2 Jan 2024 14:00:00 +0000",
            "body": "Important content",
            "attachment_parts": [],
        }

        mock_get_forward = AsyncMock(return_value=original_email)
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            with patch("aiosmtplib.SMTP", return_value=mock_smtp):
                result = await classic_handler.forward_email(
                    email_id="456",
                    recipients=["forward@example.com"],
                    from_address="Custom Sender <custom@example.com>",
                )

                assert isinstance(result, ForwardEmailResponse)
                assert result.from_address == "Custom Sender <custom@example.com>"
                assert result.success is True

    @pytest.mark.asyncio
    async def test_forward_email_not_found(self, classic_handler):
        """Test forward_email method when email is not found."""
        mock_get_forward = AsyncMock(return_value=None)

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            result = await classic_handler.forward_email(
                email_id="999",
                recipients=["forward@example.com"],
            )

            assert isinstance(result, ForwardEmailResponse)
            assert result.success is False
            assert "Could not retrieve email" in result.message

    @pytest.mark.asyncio
    async def test_forward_email_already_fwd_subject(self, classic_handler):
        """Test forward_email preserves Fwd: prefix if already present."""
        original_email = {
            "email_id": "789",
            "subject": "Fwd: Already Forwarded",
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "cc": "",
            "date": "Wed, 3 Jan 2024 16:00:00 +0000",
            "body": "Already forwarded content",
            "attachment_parts": [],
        }

        mock_get_forward = AsyncMock(return_value=original_email)
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            with patch("aiosmtplib.SMTP", return_value=mock_smtp):
                result = await classic_handler.forward_email(
                    email_id="789",
                    recipients=["forward@example.com"],
                )

                assert result.subject == "Fwd: Already Forwarded"  # Should not add another Fwd:

    @pytest.mark.asyncio
    async def test_forward_html_email(self, classic_handler):
        """Test forward_email method with HTML-only email content."""
        original_email = {
            "email_id": "html123",
            "subject": "HTML Newsletter",
            "from": "newsletter@example.com",
            "to": "subscriber@example.com",
            "cc": "",
            "date": "Thu, 4 Jan 2024 10:00:00 +0000",
            "body": "",  # No plain text body
            "html_body": "<html><body><h1>Welcome!</h1><p>This is <b>HTML</b> content.</p></body></html>",
            "is_html": True,
            "attachment_parts": [],
        }

        mock_get_forward = AsyncMock(return_value=original_email)
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            with patch("aiosmtplib.SMTP", return_value=mock_smtp):
                result = await classic_handler.forward_email(
                    email_id="html123",
                    recipients=["forward@example.com"],
                    additional_message="Check this out!",
                )

                assert isinstance(result, ForwardEmailResponse)
                assert result.success is True
                assert result.subject == "Fwd: HTML Newsletter"

                # Verify send_message was called with HTML content
                mock_smtp.send_message.assert_called_once()
                sent_msg = mock_smtp.send_message.call_args[0][0]
                # The message should be HTML type
                assert sent_msg.get_content_type() == "text/html"
                # Should contain the original HTML body (decode=True handles base64)
                payload = sent_msg.get_payload(decode=True).decode("utf-8")
                assert "<h1>Welcome!</h1>" in payload
                assert "Forwarded message" in payload

    @pytest.mark.asyncio
    async def test_forward_plain_text_email(self, classic_handler):
        """Test forward_email method with plain text only email."""
        original_email = {
            "email_id": "plain123",
            "subject": "Plain Text Message",
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "cc": "",
            "date": "Thu, 4 Jan 2024 10:00:00 +0000",
            "body": "This is a plain text message.\n\nNo HTML here.",
            "html_body": "",  # No HTML body
            "is_html": False,
            "attachment_parts": [],
        }

        mock_get_forward = AsyncMock(return_value=original_email)
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            with patch("aiosmtplib.SMTP", return_value=mock_smtp):
                result = await classic_handler.forward_email(
                    email_id="plain123",
                    recipients=["forward@example.com"],
                )

                assert isinstance(result, ForwardEmailResponse)
                assert result.success is True

                # Verify send_message was called with plain text content
                mock_smtp.send_message.assert_called_once()
                sent_msg = mock_smtp.send_message.call_args[0][0]
                # The message should be plain text type
                assert sent_msg.get_content_type() == "text/plain"
                payload = sent_msg.get_payload(decode=True).decode("utf-8")
                assert "This is a plain text message." in payload
                assert "Forwarded message" in payload

    @pytest.mark.asyncio
    async def test_forward_multipart_email_uses_clean_text(self, classic_handler):
        """Test forward_email with multipart email (both HTML and plain text).

        When an email has both HTML and plain text parts, we should forward as
        plain text but use the HTML converted to clean text to avoid raw HTML tags.
        """
        original_email = {
            "email_id": "multi123",
            "subject": "Multipart Message",
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "cc": "",
            "date": "Thu, 4 Jan 2024 10:00:00 +0000",
            "body": "Plain text version with some content",  # Has plain text
            "html_body": "<div>HTML version</div><p>with <b>formatting</b></p>",  # Also has HTML
            "is_html": False,  # Not HTML-only since plain text exists
            "attachment_parts": [],
        }

        mock_get_forward = AsyncMock(return_value=original_email)
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            with patch("aiosmtplib.SMTP", return_value=mock_smtp):
                result = await classic_handler.forward_email(
                    email_id="multi123",
                    recipients=["forward@example.com"],
                )

                assert isinstance(result, ForwardEmailResponse)
                assert result.success is True

                # Verify send_message was called with plain text content
                mock_smtp.send_message.assert_called_once()
                sent_msg = mock_smtp.send_message.call_args[0][0]
                assert sent_msg.get_content_type() == "text/plain"
                payload = sent_msg.get_payload(decode=True).decode("utf-8")

                # Should NOT contain raw HTML tags
                assert "<div>" not in payload
                assert "<p>" not in payload
                assert "<b>" not in payload

                # Should contain clean text extracted from HTML
                assert "HTML version" in payload
                assert "formatting" in payload
                assert "Forwarded message" in payload

    @pytest.mark.asyncio
    async def test_forward_multipart_email_no_html_contamination(self, classic_handler):
        """Test that multipart email forwarding doesn't include raw HTML tags.

        This specifically tests the case where a poorly formatted email has
        HTML tags in its plain text part - we should use the HTML body converted
        to clean text instead.
        """
        original_email = {
            "email_id": "contaminated123",
            "subject": "Contaminated Plain Text",
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "cc": "",
            "date": "Thu, 4 Jan 2024 10:00:00 +0000",
            # Plain text that contains HTML tags (badly formatted email)
            "body": "<div>This should not appear as raw HTML</div>",
            "html_body": "<div>Clean HTML content here</div>",
            "is_html": False,
            "attachment_parts": [],
        }

        mock_get_forward = AsyncMock(return_value=original_email)
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            with patch("aiosmtplib.SMTP", return_value=mock_smtp):
                result = await classic_handler.forward_email(
                    email_id="contaminated123",
                    recipients=["forward@example.com"],
                )

                assert isinstance(result, ForwardEmailResponse)
                assert result.success is True

                mock_smtp.send_message.assert_called_once()
                sent_msg = mock_smtp.send_message.call_args[0][0]
                assert sent_msg.get_content_type() == "text/plain"
                payload = sent_msg.get_payload(decode=True).decode("utf-8")

                # Should NOT contain raw HTML tags
                assert "<div>" not in payload

                # Should contain the clean text from the HTML body
                assert "Clean HTML content here" in payload

    @pytest.mark.asyncio
    async def test_forward_email_includes_date_header(self, classic_handler):
        """Test that forwarded emails include a Date header (RFC 5322 requirement)."""
        original_email = {
            "email_id": "date123",
            "subject": "Test Date Header",
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "cc": "",
            "date": "Thu, 4 Jan 2024 10:00:00 +0000",
            "body": "Test body",
            "html_body": "",
            "is_html": False,
            "attachment_parts": [],
        }

        mock_get_forward = AsyncMock(return_value=original_email)
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch.object(classic_handler.incoming_client, "get_email_for_forward", mock_get_forward):
            with patch("aiosmtplib.SMTP", return_value=mock_smtp):
                result = await classic_handler.forward_email(
                    email_id="date123",
                    recipients=["forward@example.com"],
                )

                assert result.success is True

                mock_smtp.send_message.assert_called_once()
                sent_msg = mock_smtp.send_message.call_args[0][0]

                # Must have a Date header (RFC 5322 requirement)
                assert sent_msg["Date"] is not None
                # Date should be in RFC 2822 format (e.g., "Fri, 17 Jan 2026 09:01:00 +0000")
                assert "," in sent_msg["Date"]  # Day, DD Mon YYYY format
