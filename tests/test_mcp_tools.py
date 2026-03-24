from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.app import (
    _sender_allowed,
    add_email_account,
    delete_emails,
    download_attachment,
    get_emails_content,
    list_allowed_senders,
    list_available_accounts,
    list_emails_metadata,
    send_email,
)
from mcp_email_server.config import EmailServer, EmailSettings, ProviderSettings
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailBodyResponse,
    EmailContentBatchResponse,
    EmailMetadata,
    EmailMetadataPageResponse,
)


class TestMcpTools:
    @pytest.mark.asyncio
    async def test_list_available_accounts(self):
        """Test list_available_accounts MCP tool."""
        # Create test accounts
        email_settings = EmailSettings(
            account_name="test_email",
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

        provider_settings = ProviderSettings(
            account_name="test_provider",
            provider_name="test",
            api_key="test_key",
        )

        # Mock the get_settings function
        mock_settings = MagicMock()
        mock_settings.get_accounts.return_value = [email_settings, provider_settings]

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            # Call the function
            result = await list_available_accounts()

            # Verify the result
            assert len(result) == 2
            assert result[0].account_name == "test_email"
            assert result[1].account_name == "test_provider"

            # Verify get_accounts was called correctly
            mock_settings.get_accounts.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_email_account(self):
        """Test add_email_account MCP tool."""
        # Create test email settings
        email_settings = EmailSettings(
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

        # Mock the get_settings function
        mock_settings = MagicMock()

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            # Call the function
            result = await add_email_account(email_settings)

            # Verify the return value
            assert result == "Successfully added email account 'test_account'"

            # Verify add_email and store were called correctly
            mock_settings.add_email.assert_called_once_with(email_settings)
            mock_settings.store.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_emails_metadata(self):
        """Test list_emails_metadata MCP tool."""
        # Create test data
        now = datetime.now(timezone.utc)
        email_metadata = EmailMetadata(
            email_id="12345",
            subject="Test Subject",
            sender="sender@example.com",
            recipients=["recipient@example.com"],
            date=now,
            attachments=[],
        )

        email_metadata_page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=now,
            since=None,
            subject="Test",
            emails=[email_metadata],
            total=1,
        )

        # Mock the dispatch_handler function
        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = email_metadata_page

        mock_settings = MagicMock()
        mock_settings.allowed_senders = []

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                # Call the function
                result = await list_emails_metadata(
                    account_name="test_account",
                    page=1,
                    page_size=10,
                    before=now,
                    since=None,
                    subject="Test",
                    from_address="sender@example.com",
                    to_address=None,
                )

                # Verify the result
                assert result == email_metadata_page
                assert result.page == 1
                assert result.page_size == 10
                assert result.before == now
                assert result.subject == "Test"
                assert len(result.emails) == 1
                assert result.emails[0].subject == "Test Subject"
                assert result.emails[0].email_id == "12345"

                # Verify dispatch_handler and get_emails_metadata were called correctly
                mock_handler.get_emails_metadata.assert_called_once_with(
                    page=1,
                    page_size=10,
                    before=now,
                    since=None,
                    subject="Test",
                    from_address="sender@example.com",
                    to_address=None,
                    order="desc",
                    mailbox="INBOX",
                    seen=None,
                    flagged=None,
                    answered=None,
                )

    @pytest.mark.asyncio
    async def test_list_emails_metadata_with_mailbox(self):
        """Test list_emails_metadata MCP tool with custom mailbox."""
        now = datetime.now(timezone.utc)
        email_metadata = EmailMetadata(
            email_id="12345",
            subject="Sent Subject",
            sender="me@example.com",
            recipients=["recipient@example.com"],
            date=now,
            attachments=[],
        )

        email_metadata_page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[email_metadata],
            total=1,
        )

        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = email_metadata_page

        mock_settings = MagicMock()
        mock_settings.allowed_senders = []

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await list_emails_metadata(
                    account_name="test_account",
                    mailbox="Sent",
                )

                assert result == email_metadata_page
                mock_handler.get_emails_metadata.assert_called_once_with(
                    page=1,
                    page_size=10,
                    before=None,
                    since=None,
                    subject=None,
                    from_address=None,
                    to_address=None,
                    order="desc",
                    mailbox="Sent",
                    seen=None,
                    flagged=None,
                    answered=None,
                )

    @pytest.mark.asyncio
    async def test_get_emails_content_single(self):
        """Test get_emails_content MCP tool with single email."""
        # Create test data
        now = datetime.now(timezone.utc)
        email_body = EmailBodyResponse(
            email_id="12345",
            subject="Test Subject",
            sender="sender@example.com",
            recipients=["recipient@example.com"],
            date=now,
            body="This is the test email body content.",
            attachments=["attachment1.pdf"],
        )

        batch_response = EmailContentBatchResponse(
            emails=[email_body],
            requested_count=1,
            retrieved_count=1,
            failed_ids=[],
        )

        # Mock the dispatch_handler function
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch_response

        mock_settings = MagicMock()
        mock_settings.allowed_senders = []

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                # Call the function
                result = await get_emails_content(
                    account_name="test_account",
                    email_ids=["12345"],
                )

                # Verify the result
                assert result == batch_response
                assert result.requested_count == 1
                assert result.retrieved_count == 1
                assert len(result.failed_ids) == 0
                assert len(result.emails) == 1
                assert result.emails[0].email_id == "12345"
                assert result.emails[0].subject == "Test Subject"

                # Verify dispatch_handler and get_emails_content were called correctly
                mock_handler.get_emails_content.assert_called_once_with(["12345"], "INBOX")

    @pytest.mark.asyncio
    async def test_get_emails_content_batch(self):
        """Test get_emails_content MCP tool with multiple emails."""
        # Create test data
        now = datetime.now(timezone.utc)
        email1 = EmailBodyResponse(
            email_id="12345",
            subject="Test Subject 1",
            sender="sender1@example.com",
            recipients=["recipient@example.com"],
            date=now,
            body="This is the first test email body content.",
            attachments=[],
        )

        email2 = EmailBodyResponse(
            email_id="12346",
            subject="Test Subject 2",
            sender="sender2@example.com",
            recipients=["recipient@example.com"],
            date=now,
            body="This is the second test email body content.",
            attachments=["attachment1.pdf"],
        )

        batch_response = EmailContentBatchResponse(
            emails=[email1, email2],
            requested_count=3,
            retrieved_count=2,
            failed_ids=["12347"],
        )

        # Mock the dispatch_handler function
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch_response

        mock_settings = MagicMock()
        mock_settings.allowed_senders = []

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                # Call the function
                result = await get_emails_content(
                    account_name="test_account",
                    email_ids=["12345", "12346", "12347"],
                )

                # Verify the result
                assert result == batch_response
                assert result.requested_count == 3
                assert result.retrieved_count == 2
                assert len(result.failed_ids) == 1
                assert result.failed_ids[0] == "12347"
                assert len(result.emails) == 2
                assert result.emails[0].email_id == "12345"
                assert result.emails[1].email_id == "12346"

                # Verify dispatch_handler and get_emails_content were called correctly
                mock_handler.get_emails_content.assert_called_once_with(["12345", "12346", "12347"], "INBOX")

    @pytest.mark.asyncio
    async def test_get_emails_content_with_mailbox(self):
        """Test get_emails_content MCP tool with custom mailbox."""
        now = datetime.now(timezone.utc)
        email_body = EmailBodyResponse(
            email_id="12345",
            subject="Sent Subject",
            sender="me@example.com",
            recipients=["recipient@example.com"],
            date=now,
            body="This is a sent email.",
            attachments=[],
        )

        batch_response = EmailContentBatchResponse(
            emails=[email_body],
            requested_count=1,
            retrieved_count=1,
            failed_ids=[],
        )

        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch_response

        mock_settings = MagicMock()
        mock_settings.allowed_senders = []

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(
                    account_name="test_account",
                    email_ids=["12345"],
                    mailbox="Sent",
                )

                assert result == batch_response
                mock_handler.get_emails_content.assert_called_once_with(["12345"], "Sent")

    @pytest.mark.asyncio
    async def test_send_email(self):
        """Test send_email MCP tool."""
        # Mock the dispatch_handler function
        mock_handler = AsyncMock()

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            # Call the function
            result = await send_email(
                account_name="test_account",
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
                cc=["cc@example.com"],
                bcc=["bcc@example.com"],
            )

            # Verify the return value
            assert result == "Email sent successfully to recipient@example.com"

            # Verify send_email was called correctly
            mock_handler.send_email.assert_called_once_with(
                ["recipient@example.com"],
                "Test Subject",
                "Test Body",
                ["cc@example.com"],
                ["bcc@example.com"],
                False,
                None,
                None,  # in_reply_to
                None,  # references
            )

    @pytest.mark.asyncio
    async def test_delete_emails(self):
        """Test delete_emails MCP tool."""
        mock_handler = AsyncMock()
        mock_handler.delete_emails.return_value = (["12345", "12346"], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await delete_emails(
                account_name="test_account",
                email_ids=["12345", "12346"],
            )

            assert result == "Successfully deleted 2 email(s)"
            mock_handler.delete_emails.assert_called_once_with(["12345", "12346"], "INBOX")

    @pytest.mark.asyncio
    async def test_delete_emails_with_failures(self):
        """Test delete_emails MCP tool with some failures."""
        mock_handler = AsyncMock()
        mock_handler.delete_emails.return_value = (["12345"], ["12346", "12347"])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await delete_emails(
                account_name="test_account",
                email_ids=["12345", "12346", "12347"],
            )

            assert result == "Successfully deleted 1 email(s), failed to delete 2 email(s): 12346, 12347"
            mock_handler.delete_emails.assert_called_once_with(["12345", "12346", "12347"], "INBOX")

    @pytest.mark.asyncio
    async def test_delete_emails_with_mailbox(self):
        """Test delete_emails MCP tool with custom mailbox."""
        mock_handler = AsyncMock()
        mock_handler.delete_emails.return_value = (["12345"], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await delete_emails(
                account_name="test_account",
                email_ids=["12345"],
                mailbox="Trash",
            )

            assert result == "Successfully deleted 1 email(s)"
            mock_handler.delete_emails.assert_called_once_with(["12345"], "Trash")

    @pytest.mark.asyncio
    async def test_download_attachment_disabled(self):
        """Test download_attachment MCP tool when feature is disabled."""
        mock_settings = MagicMock()
        mock_settings.enable_attachment_download = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(PermissionError) as exc_info:
                await download_attachment(
                    account_name="test_account",
                    email_id="12345",
                    attachment_name="document.pdf",
                    save_path="/var/downloads/document.pdf",
                )

            assert "Attachment download is disabled" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_download_attachment_enabled(self):
        """Test download_attachment MCP tool when feature is enabled."""
        attachment_response = AttachmentDownloadResponse(
            email_id="12345",
            attachment_name="document.pdf",
            mime_type="application/pdf",
            size=1024,
            saved_path="/var/downloads/document.pdf",
        )

        mock_settings = MagicMock()
        mock_settings.enable_attachment_download = True

        mock_handler = AsyncMock()
        mock_handler.download_attachment.return_value = attachment_response

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await download_attachment(
                    account_name="test_account",
                    email_id="12345",
                    attachment_name="document.pdf",
                    save_path="/var/downloads/document.pdf",
                )

                assert result == attachment_response
                assert result.email_id == "12345"
                assert result.attachment_name == "document.pdf"
                assert result.mime_type == "application/pdf"
                assert result.size == 1024

                mock_handler.download_attachment.assert_called_once_with(
                    "12345", "document.pdf", "/var/downloads/document.pdf", "INBOX"
                )

    @pytest.mark.asyncio
    async def test_send_email_with_reply_headers(self):
        """Test send_email MCP tool with reply headers."""
        mock_handler = AsyncMock()
        mock_handler.send_email = AsyncMock()

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await send_email(
                account_name="test",
                recipients=["recipient@example.com"],
                subject="Re: Test",
                body="Reply body",
                in_reply_to="<original@example.com>",
                references="<original@example.com>",
            )

            mock_handler.send_email.assert_called_once()
            call_args = mock_handler.send_email.call_args
            # Verify in_reply_to and references were passed (positions 7 and 8 after cc, bcc, html, attachments)
            assert "<original@example.com>" in str(call_args)
            assert "recipient@example.com" in result

    @pytest.mark.asyncio
    async def test_get_emails_content_includes_message_id(self):
        """Test that get_emails_content returns message_id."""
        from datetime import datetime, timezone

        mock_handler = AsyncMock()
        mock_handler.get_emails_content = AsyncMock(
            return_value=EmailContentBatchResponse(
                emails=[
                    EmailBodyResponse(
                        email_id="123",
                        message_id="<test@example.com>",
                        subject="Test",
                        sender="sender@example.com",
                        recipients=["recipient@example.com"],
                        date=datetime.now(timezone.utc),
                        body="Test body",
                        attachments=[],
                    )
                ],
                requested_count=1,
                retrieved_count=1,
                failed_ids=[],
            )
        )

        mock_settings = MagicMock()
        mock_settings.allowed_senders = []

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(
                    account_name="test",
                    email_ids=["123"],
                )

                assert result.emails[0].message_id == "<test@example.com>"

    @pytest.mark.asyncio
    async def test_list_allowed_senders_empty(self):
        """Returns empty list when no sender allowlist is configured."""
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            result = await list_allowed_senders()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_allowed_senders_returns_patterns(self):
        """Returns configured patterns verbatim (including globs)."""
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de", "alice@example.com"]

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            result = await list_allowed_senders()

        assert result == ["*@glez.de", "alice@example.com"]

    @pytest.mark.asyncio
    async def test_list_emails_metadata_no_sender_allowlist(self):
        """All emails returned when sender allowlist is empty."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []
        page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[
                EmailMetadata(
                    email_id="1",
                    subject="Hi",
                    sender="anyone@evil.com",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
            ],
            total=1,
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = page

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await list_emails_metadata(account_name="test")

        assert len(result.emails) == 1

    @pytest.mark.asyncio
    async def test_list_emails_metadata_filters_blocked_sender(self):
        """Emails from unlisted senders are removed; allowed senders remain."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[
                EmailMetadata(
                    email_id="1",
                    subject="Good",
                    sender="friend@glez.de",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
                EmailMetadata(
                    email_id="2",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
            ],
            total=2,
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = page

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await list_emails_metadata(account_name="test")

        assert len(result.emails) == 1
        assert result.emails[0].email_id == "1"

    @pytest.mark.asyncio
    async def test_list_emails_metadata_total_unchanged_after_filter(self):
        """total reflects IMAP server count and is not adjusted post-filter."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[
                EmailMetadata(
                    email_id="1",
                    subject="Good",
                    sender="friend@glez.de",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
                EmailMetadata(
                    email_id="2",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
            ],
            total=50,  # large server-side total — left unchanged after filtering
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = page

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await list_emails_metadata(account_name="test")

        assert result.total == 50  # unchanged — reflects IMAP server count
        assert len(result.emails) == 1  # filtered in the response

    @pytest.mark.asyncio
    async def test_get_emails_content_no_sender_allowlist(self):
        """All emails returned when sender allowlist is empty."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []
        batch = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="1",
                    subject="Any",
                    sender="anyone@evil.com",
                    recipients=[],
                    date=now,
                    body="hello",
                    attachments=[],
                ),
            ],
            requested_count=1,
            retrieved_count=1,
            failed_ids=[],
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(account_name="test", email_ids=["1"])

        assert len(result.emails) == 1

    @pytest.mark.asyncio
    async def test_get_emails_content_filters_blocked_sender(self):
        """Emails from unlisted senders are silently dropped."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        batch = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="1",
                    subject="Good",
                    sender="friend@glez.de",
                    recipients=[],
                    date=now,
                    body="hi",
                    attachments=[],
                ),
                EmailBodyResponse(
                    email_id="2",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    body="buy now",
                    attachments=[],
                ),
            ],
            requested_count=2,
            retrieved_count=2,
            failed_ids=[],
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(account_name="test", email_ids=["1", "2"])

        assert len(result.emails) == 1
        assert result.emails[0].email_id == "1"

    @pytest.mark.asyncio
    async def test_get_emails_content_retrieved_count_adjusted(self):
        """retrieved_count is updated to reflect post-filter count."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        batch = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="1",
                    subject="Good",
                    sender="friend@glez.de",
                    recipients=[],
                    date=now,
                    body="hi",
                    attachments=[],
                ),
                EmailBodyResponse(
                    email_id="2",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    body="buy",
                    attachments=[],
                ),
            ],
            requested_count=2,
            retrieved_count=2,
            failed_ids=[],
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(account_name="test", email_ids=["1", "2"])

        assert result.retrieved_count == 1  # adjusted to post-filter count

    @pytest.mark.asyncio
    async def test_get_emails_content_blocked_not_in_failed_ids(self):
        """Blocked emails are silently dropped — NOT added to failed_ids.

        Adding a blocked email to failed_ids would reveal its existence to the AI,
        defeating the purpose of the allowlist.
        """
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        batch = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="1",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    body="buy",
                    attachments=[],
                ),
            ],
            requested_count=1,
            retrieved_count=1,
            failed_ids=[],
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(account_name="test", email_ids=["1"])

        assert result.failed_ids == []  # NOT populated with blocked email
        assert len(result.emails) == 0


class TestSenderAllowed:
    """Tests for the _sender_allowed pure helper function."""

    def test_empty_patterns_allows_all(self):
        """Empty allowlist always returns True (allow all)."""
        assert _sender_allowed("anyone@evil.com", []) is True

    def test_exact_match(self):
        """Exact address pattern matches correctly."""
        assert _sender_allowed("alice@example.com", ["alice@example.com"]) is True

    def test_glob_domain(self):
        """Wildcard domain pattern matches any address at that domain."""
        assert _sender_allowed("bob@glez.de", ["*@glez.de"]) is True

    def test_name_addr_format(self):
        """'Name <addr>' format: address portion extracted and matched."""
        assert _sender_allowed("Alice <alice@example.com>", ["alice@example.com"]) is True

    def test_case_insensitive(self):
        """Matching is case-insensitive (patterns pre-lowercased, sender lowercased at match time)."""
        assert _sender_allowed("Alice@EXAMPLE.COM", ["alice@example.com"]) is True

    def test_no_match(self):
        """Address not in allowlist returns False."""
        assert _sender_allowed("spam@evil.com", ["*@glez.de"]) is False

    def test_matches_second_pattern_in_list(self):
        """Sender matching the second pattern returns True (any() traversal)."""
        assert _sender_allowed("bob@example.com", ["alice@example.com", "bob@example.com"]) is True

    def test_unparseable_sender_blocked(self):
        """Malformed From header that parseaddr cannot parse is treated as not allowed."""
        # parseaddr("not-an-email-at-all") returns ('', '') so fallback is the raw string,
        # which will not match any normal pattern like *@example.com
        assert _sender_allowed("not-an-email-at-all", ["*@example.com"]) is False
