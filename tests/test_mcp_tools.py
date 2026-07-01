from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server import app as app_module
from mcp_email_server.app import (
    add_email_account,
    archive_emails,
    delete_emails,
    download_attachment,
    get_emails_content,
    list_allowed_recipients,
    list_allowed_senders,
    list_available_accounts,
    list_emails_metadata,
    list_mailboxes,
    mark_emails_as_read,
    move_emails,
    save_to_mailbox,
    send_email,
)
from mcp_email_server.config import EmailServer, EmailSettings, ProviderSettings
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailBodyResponse,
    EmailContentBatchResponse,
    EmailMetadata,
    EmailMetadataPageResponse,
    MailboxInfo,
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
                body=None,
                text=None,
                has_attachment=None,
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
                body=None,
                text=None,
                has_attachment=None,
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
            mock_handler.get_emails_content.assert_called_once_with(["12345"], "INBOX", False, 0, 20000)

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
            mock_handler.get_emails_content.assert_called_once_with(
                ["12345", "12346", "12347"], "INBOX", False, 0, 20000
            )

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

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await get_emails_content(
                account_name="test_account",
                email_ids=["12345"],
                mailbox="Sent",
            )

            assert result == batch_response
            mock_handler.get_emails_content.assert_called_once_with(["12345"], "Sent", False, 0, 20000)

    @pytest.mark.asyncio
    async def test_tool_visibility_hides_outbound_tools_for_read_only_accounts(self):
        """Read-only deployments should hide outbound tools from MCP clients."""
        read_only_account = EmailSettings(
            account_name="read_only",
            full_name="Read Only",
            email_address="read-only@example.com",
            incoming=EmailServer(
                user_name="reader",
                password="secret",
                host="imap.example.com",
                port=993,
                use_ssl=True,
            ),
        )
        mock_settings = MagicMock()
        mock_settings.get_accounts.return_value = [read_only_account]

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            tool_names = {tool.name for tool in await app_module.mcp.list_tools()}

        assert "send_email" not in tool_names
        assert "save_to_mailbox" not in tool_names
        assert "list_emails_metadata" in tool_names
        assert "get_emails_content" in tool_names

    @pytest.mark.asyncio
    async def test_tool_visibility_shows_outbound_tools_for_send_capable_accounts(self):
        """SMTP-configured deployments should expose outbound tools."""
        send_capable_account = EmailSettings(
            account_name="send_capable",
            full_name="Send Capable",
            email_address="sender@example.com",
            incoming=EmailServer(
                user_name="reader",
                password="secret",
                host="imap.example.com",
                port=993,
                use_ssl=True,
            ),
            outgoing=EmailServer(
                user_name="sender",
                password="secret",
                host="smtp.example.com",
                port=465,
                use_ssl=True,
            ),
        )
        mock_settings = MagicMock()
        mock_settings.get_accounts.return_value = [send_capable_account]

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            tool_names = {tool.name for tool in await app_module.mcp.list_tools()}

        assert "send_email" in tool_names
        assert "save_to_mailbox" in tool_names

    @pytest.mark.asyncio
    async def test_send_email(self):
        """Test send_email MCP tool."""
        # Mock the dispatch_handler function
        mock_handler = AsyncMock()

        mock_settings = MagicMock()
        mock_settings.allowed_recipients = []
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
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
                    None,  # reply_to
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
    async def test_mark_emails_as_read(self):
        """Test mark_emails_as_read MCP tool."""
        mock_handler = AsyncMock()
        mock_handler.mark_emails_as_read.return_value = (["12345", "12346"], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await mark_emails_as_read(
                account_name="test_account",
                email_ids=["12345", "12346"],
            )

            assert result == "Successfully marked 2 email(s) as read"
            mock_handler.mark_emails_as_read.assert_called_once_with(["12345", "12346"], "INBOX")

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_with_failures(self):
        """Test mark_emails_as_read MCP tool with some failures."""
        mock_handler = AsyncMock()
        mock_handler.mark_emails_as_read.return_value = (["12345"], ["12346"])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await mark_emails_as_read(
                account_name="test_account",
                email_ids=["12345", "12346"],
            )

            assert result == "Successfully marked 1 email(s) as read, failed to mark 1 email(s): 12346"

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_with_mailbox(self):
        """Test mark_emails_as_read MCP tool with custom mailbox."""
        mock_handler = AsyncMock()
        mock_handler.mark_emails_as_read.return_value = (["12345"], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await mark_emails_as_read(
                account_name="test_account",
                email_ids=["12345"],
                mailbox="Sent",
            )

            assert result == "Successfully marked 1 email(s) as read"
            mock_handler.mark_emails_as_read.assert_called_once_with(["12345"], "Sent")

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

        mock_settings = MagicMock()
        mock_settings.allowed_recipients = []
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
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

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await get_emails_content(
                account_name="test",
                email_ids=["123"],
            )

            assert result.emails[0].message_id == "<test@example.com>"

    @pytest.mark.asyncio
    async def test_get_emails_content_mark_as_read_true(self):
        """Test that mark_as_read=True is passed through to the handler."""
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = EmailContentBatchResponse(
            emails=[], requested_count=1, retrieved_count=0, failed_ids=["123"]
        )

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            await get_emails_content(
                account_name="test",
                email_ids=["123"],
                mark_as_read=True,
            )

            mock_handler.get_emails_content.assert_called_once_with(["123"], "INBOX", True, 0, 20000)

    @pytest.mark.asyncio
    async def test_get_emails_content_mark_as_read_default_false(self):
        """Test that mark_as_read defaults to False."""
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = EmailContentBatchResponse(
            emails=[], requested_count=1, retrieved_count=0, failed_ids=["123"]
        )

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            await get_emails_content(
                account_name="test",
                email_ids=["123"],
            )

            mock_handler.get_emails_content.assert_called_once_with(["123"], "INBOX", False, 0, 20000)

    @pytest.mark.asyncio
    async def test_get_emails_content_body_offset_and_max_body_length(self):
        """body_offset and max_body_length are passed through to the handler for paging."""
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = EmailContentBatchResponse(
            emails=[], requested_count=1, retrieved_count=0, failed_ids=["123"]
        )

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            await get_emails_content(
                account_name="test",
                email_ids=["123"],
                body_offset=4000,
                max_body_length=2000,
            )

            mock_handler.get_emails_content.assert_called_once_with(["123"], "INBOX", False, 4000, 2000)

    @pytest.mark.asyncio
    async def test_move_emails(self):
        """Test move_emails MCP tool."""
        mock_handler = AsyncMock()
        mock_handler.move_emails.return_value = (["12345", "12346"], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await move_emails(
                account_name="test_account",
                email_ids=["12345", "12346"],
                destination_mailbox="Archive",
            )

            assert result == "Successfully moved 2 email(s) to Archive"
            mock_handler.move_emails.assert_called_once_with(["12345", "12346"], "INBOX", "Archive")

    @pytest.mark.asyncio
    async def test_archive_emails(self):
        """Test archive_emails MCP tool."""
        mock_handler = AsyncMock()
        mock_handler.archive_emails.return_value = (["12345", "12346"], [], "Archive")

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await archive_emails(
                account_name="test_account",
                email_ids=["12345", "12346"],
            )

            assert result == "Successfully archived 2 email(s) to Archive"
            mock_handler.archive_emails.assert_called_once_with(["12345", "12346"], "INBOX")

    @pytest.mark.asyncio
    async def test_archive_emails_with_failures(self):
        """Test archive_emails MCP tool reports failures."""
        mock_handler = AsyncMock()
        mock_handler.archive_emails.return_value = (["12345"], ["12346"], "[Gmail]/All Mail")

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await archive_emails(account_name="test_account", email_ids=["12345", "12346"])

            assert result == "Successfully archived 1 email(s) to [Gmail]/All Mail, failed to archive 1 email(s): 12346"

    @pytest.mark.asyncio
    async def test_move_emails_with_source_mailbox(self):
        """Test move_emails MCP tool with custom source mailbox."""
        mock_handler = AsyncMock()
        mock_handler.move_emails.return_value = (["12345"], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await move_emails(
                account_name="test_account",
                email_ids=["12345"],
                source_mailbox="Trash",
                destination_mailbox="INBOX",
            )

            assert result == "Successfully moved 1 email(s) to INBOX"
            mock_handler.move_emails.assert_called_once_with(["12345"], "Trash", "INBOX")

    @pytest.mark.asyncio
    async def test_move_emails_with_failures(self):
        """Test move_emails MCP tool with some failures."""
        mock_handler = AsyncMock()
        mock_handler.move_emails.return_value = (["12345"], ["12346", "12347"])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await move_emails(
                account_name="test_account",
                email_ids=["12345", "12346", "12347"],
                destination_mailbox="Archive",
            )

            assert result == "Successfully moved 1 email(s) to Archive, failed to move 2 email(s): 12346, 12347"
            mock_handler.move_emails.assert_called_once_with(["12345", "12346", "12347"], "INBOX", "Archive")

    @pytest.mark.asyncio
    async def test_list_mailboxes(self):
        """Test list_mailboxes MCP tool."""
        mock_handler = AsyncMock()
        mock_handler.list_mailboxes.return_value = [
            MailboxInfo(name="INBOX", delimiter="/", flags=["\\HasChildren"]),
            MailboxInfo(name="Sent", delimiter="/", flags=["\\Sent", "\\HasNoChildren"]),
            MailboxInfo(name="Drafts", delimiter="/", flags=["\\Drafts", "\\HasNoChildren"]),
            MailboxInfo(name="Trash", delimiter="/", flags=["\\Trash", "\\HasNoChildren"]),
            MailboxInfo(name="Archive", delimiter="/", flags=["\\HasNoChildren"]),
        ]

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await list_mailboxes(account_name="test_account")

            assert len(result) == 5
            assert result[0].name == "INBOX"
            assert result[0].delimiter == "/"
            assert "\\HasChildren" in result[0].flags
            assert result[1].name == "Sent"
            assert "\\Sent" in result[1].flags
            mock_handler.list_mailboxes.assert_called_once_with("*", "")

    @pytest.mark.asyncio
    async def test_list_mailboxes_with_pattern(self):
        """Test list_mailboxes MCP tool with custom pattern."""
        mock_handler = AsyncMock()
        mock_handler.list_mailboxes.return_value = [
            MailboxInfo(name="INBOX.Clients", delimiter=".", flags=["\\HasNoChildren"]),
            MailboxInfo(name="INBOX.Projects", delimiter=".", flags=["\\HasNoChildren"]),
        ]

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await list_mailboxes(account_name="test_account", pattern="INBOX.*")

            assert len(result) == 2
            assert result[0].delimiter == "."
            mock_handler.list_mailboxes.assert_called_once_with("INBOX.*", "")

    @pytest.mark.asyncio
    async def test_list_allowed_recipients_hidden_when_unconfigured(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = []
        mock_settings.get_accounts.return_value = []
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            tool_names = {tool.name for tool in await app_module.mcp.list_tools()}
        assert "list_allowed_recipients" not in tool_names

    @pytest.mark.asyncio
    async def test_list_allowed_recipients_visible_when_configured(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_settings.get_accounts.return_value = []
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            tool_names = {tool.name for tool in await app_module.mcp.list_tools()}
        assert "list_allowed_recipients" in tool_names

    @pytest.mark.asyncio
    async def test_list_allowed_recipients_returns_list(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com", "bob@example.com"]
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            result = await list_allowed_recipients()
        assert result == ["alice@example.com", "bob@example.com"]

    @pytest.mark.asyncio
    async def test_send_email_no_allowlist_allows_any_recipient(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = []
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await send_email(account_name="test", recipients=["anyone@example.com"], subject="S", body="B")
        assert "anyone@example.com" in result
        mock_handler.send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_email_blocks_unlisted_recipient(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                with pytest.raises(ValueError, match="not in allowlist"):
                    await send_email(account_name="test", recipients=["mallory@evil.com"], subject="S", body="B")
        mock_handler.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_email_blocks_unlisted_bcc(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                with pytest.raises(ValueError, match=r"mallory@evil\.com"):
                    await send_email(
                        account_name="test",
                        recipients=["alice@example.com"],
                        subject="S",
                        body="B",
                        bcc=["mallory@evil.com"],
                    )
        mock_handler.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_email_allows_listed_recipient_with_display_name(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                await send_email(
                    account_name="test",
                    recipients=["Alice <Alice@Example.com>"],
                    subject="S",
                    body="B",
                )
        mock_handler.send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_to_mailbox_blocks_unlisted_recipient(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                with pytest.raises(ValueError, match="not in allowlist"):
                    await save_to_mailbox(account_name="test", recipients=["mallory@evil.com"], subject="S", body="B")
        mock_handler.save_to_mailbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_to_mailbox_allows_listed_recipient(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_handler = AsyncMock()
        mock_handler.save_to_mailbox.return_value = "<mid@example.com>|uid:42"
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await save_to_mailbox(
                    account_name="test", recipients=["alice@example.com"], subject="S", body="B"
                )
        mock_handler.save_to_mailbox.assert_called_once()
        assert "saved" in result.lower()

    @pytest.mark.asyncio
    async def test_send_email_blocks_packed_multi_address_recipient(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                with pytest.raises(ValueError, match=r"mallory@evil\.com"):
                    await send_email(
                        account_name="test",
                        recipients=["alice@example.com, mallory@evil.com"],
                        subject="S",
                        body="B",
                    )
        mock_handler.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_to_mailbox_blocks_packed_multi_address_recipient(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                with pytest.raises(ValueError, match=r"mallory@evil\.com"):
                    await save_to_mailbox(
                        account_name="test",
                        recipients=["alice@example.com, mallory@evil.com"],
                        subject="S",
                        body="B",
                    )
        mock_handler.save_to_mailbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_allowed_senders_hidden_when_unconfigured(self):
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []
        mock_settings.get_accounts.return_value = []
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            tool_names = {tool.name for tool in await app_module.mcp.list_tools()}
        assert "list_allowed_senders" not in tool_names

    @pytest.mark.asyncio
    async def test_list_allowed_senders_visible_when_configured(self):
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@example.com"]
        mock_settings.get_accounts.return_value = []
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            tool_names = {tool.name for tool in await app_module.mcp.list_tools()}
        assert "list_allowed_senders" in tool_names

    @pytest.mark.asyncio
    async def test_list_allowed_senders_returns_list(self):
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@example.com", "bob@example.com"]
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            result = await list_allowed_senders()
        assert result == ["*@example.com", "bob@example.com"]
