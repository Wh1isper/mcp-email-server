from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.app import (
    add_email_account,
    get_emails_content,
    list_available_accounts,
    list_emails_metadata,
    send_email,
)
from mcp_email_server.config import EmailServer, EmailSettings, ProviderSettings
from mcp_email_server.emails.models import (
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
        now = datetime.now()
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
            )

    @pytest.mark.asyncio
    async def test_get_emails_content_single(self):
        """Test get_emails_content MCP tool with single email."""
        # Create test data
        now = datetime.now()
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
            mock_handler.get_emails_content.assert_called_once_with(["12345"])

    @pytest.mark.asyncio
    async def test_get_emails_content_batch(self):
        """Test get_emails_content MCP tool with multiple emails."""
        # Create test data
        now = datetime.now()
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
            mock_handler.get_emails_content.assert_called_once_with(["12345", "12346", "12347"])

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
            )
