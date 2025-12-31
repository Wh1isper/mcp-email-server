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
