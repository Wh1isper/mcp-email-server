from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient
from mcp_email_server.emails.models import EmailData, EmailPageResponse


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
        now = datetime.now()
        email_data = {
            "subject": "Test Subject",
            "from": "sender@example.com",
            "body": "Test Body",
            "date": now,
            "attachments": [],
        }

        # Mock the get_emails_stream method to yield our test data
        mock_stream = AsyncMock()
        mock_stream.__aiter__.return_value = [email_data]

        # Mock the get_email_count method
        mock_count = AsyncMock(return_value=1)

        # Apply the mocks
        with patch.object(classic_handler.incoming_client, "get_emails_stream", return_value=mock_stream):
            with patch.object(classic_handler.incoming_client, "get_email_count", mock_count):
                # Call the method
                result = await classic_handler.get_emails(
                    page=1,
                    page_size=10,
                    before=now,
                    since=None,
                    subject="Test",
                    body=None,
                    text=None,
                    from_address="sender@example.com",
                    to_address=None,
                )

                # Verify the result
                assert isinstance(result, EmailPageResponse)
                assert result.page == 1
                assert result.page_size == 10
                assert result.before == now
                assert result.since is None
                assert result.subject == "Test"
                assert result.body is None
                assert result.text is None
                assert len(result.emails) == 1
                assert isinstance(result.emails[0], EmailData)
                assert result.emails[0].subject == "Test Subject"
                assert result.emails[0].sender == "sender@example.com"
                assert result.emails[0].body == "Test Body"
                assert result.emails[0].date == now
                assert result.emails[0].attachments == []
                assert result.total == 1

                # Verify the client methods were called correctly
                classic_handler.incoming_client.get_emails_stream.assert_called_once_with(
                    1, 10, now, None, "Test", None, None, "sender@example.com", None, "desc"
                )
                mock_count.assert_called_once_with(now, None, "Test", None, None, "sender@example.com", None)

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
            )
