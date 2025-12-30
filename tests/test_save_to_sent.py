"""Tests for the save_to_sent feature."""

import asyncio
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient


@pytest.fixture
def email_settings_with_save_to_sent():
    """EmailSettings with save_to_sent enabled."""
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
        save_to_sent=True,
        sent_folder_name="INBOX.Sent",
    )


@pytest.fixture
def email_settings_without_save_to_sent():
    """EmailSettings with save_to_sent disabled."""
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
        save_to_sent=False,
    )


@pytest.fixture
def email_settings_default_sent_folder():
    """EmailSettings with default (auto-detect) sent folder."""
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
        save_to_sent=True,
        sent_folder_name=None,  # Auto-detect
    )


class TestEmailSettingsSaveToSent:
    """Tests for save_to_sent config options."""

    def test_save_to_sent_default_true(self):
        """Test that save_to_sent defaults to True."""
        settings = EmailSettings(
            account_name="test",
            full_name="Test",
            email_address="test@example.com",
            incoming=EmailServer(user_name="test", password="pass", host="imap.test.com", port=993),
            outgoing=EmailServer(user_name="test", password="pass", host="smtp.test.com", port=465),
        )
        assert settings.save_to_sent is True

    def test_sent_folder_name_default_none(self):
        """Test that sent_folder_name defaults to None."""
        settings = EmailSettings(
            account_name="test",
            full_name="Test",
            email_address="test@example.com",
            incoming=EmailServer(user_name="test", password="pass", host="imap.test.com", port=993),
            outgoing=EmailServer(user_name="test", password="pass", host="smtp.test.com", port=465),
        )
        assert settings.sent_folder_name is None

    def test_save_to_sent_can_be_disabled(self):
        """Test that save_to_sent can be set to False."""
        settings = EmailSettings(
            account_name="test",
            full_name="Test",
            email_address="test@example.com",
            incoming=EmailServer(user_name="test", password="pass", host="imap.test.com", port=993),
            outgoing=EmailServer(user_name="test", password="pass", host="smtp.test.com", port=465),
            save_to_sent=False,
        )
        assert settings.save_to_sent is False

    def test_sent_folder_name_can_be_set(self):
        """Test that sent_folder_name can be set."""
        settings = EmailSettings(
            account_name="test",
            full_name="Test",
            email_address="test@example.com",
            incoming=EmailServer(user_name="test", password="pass", host="imap.test.com", port=993),
            outgoing=EmailServer(user_name="test", password="pass", host="smtp.test.com", port=465),
            sent_folder_name="[Gmail]/Sent Mail",
        )
        assert settings.sent_folder_name == "[Gmail]/Sent Mail"


class TestEmailSettingsFromEnv:
    """Tests for save_to_sent environment variable configuration."""

    def test_save_to_sent_from_env_true(self, monkeypatch):
        """Test MCP_EMAIL_SERVER_SAVE_TO_SENT=true."""
        monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.test.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.test.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SAVE_TO_SENT", "true")

        result = EmailSettings.from_env()
        assert result is not None
        assert result.save_to_sent is True

    def test_save_to_sent_from_env_false(self, monkeypatch):
        """Test MCP_EMAIL_SERVER_SAVE_TO_SENT=false."""
        monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.test.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.test.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SAVE_TO_SENT", "false")

        result = EmailSettings.from_env()
        assert result is not None
        assert result.save_to_sent is False

    def test_sent_folder_name_from_env(self, monkeypatch):
        """Test MCP_EMAIL_SERVER_SENT_FOLDER_NAME."""
        monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.test.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.test.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SENT_FOLDER_NAME", "INBOX.Sent")

        result = EmailSettings.from_env()
        assert result is not None
        assert result.sent_folder_name == "INBOX.Sent"


class TestClassicEmailHandlerSaveToSent:
    """Tests for ClassicEmailHandler save_to_sent functionality."""

    def test_handler_init_with_save_to_sent(self, email_settings_with_save_to_sent):
        """Test handler initialization with save_to_sent enabled."""
        handler = ClassicEmailHandler(email_settings_with_save_to_sent)
        assert handler.save_to_sent is True
        assert handler.sent_folder_name == "INBOX.Sent"

    def test_handler_init_without_save_to_sent(self, email_settings_without_save_to_sent):
        """Test handler initialization with save_to_sent disabled."""
        handler = ClassicEmailHandler(email_settings_without_save_to_sent)
        assert handler.save_to_sent is False

    @pytest.mark.asyncio
    async def test_send_email_calls_append_to_sent(self, email_settings_with_save_to_sent):
        """Test that send_email calls append_to_sent when enabled."""
        handler = ClassicEmailHandler(email_settings_with_save_to_sent)

        mock_msg = MIMEText("Test body")
        mock_send = AsyncMock(return_value=mock_msg)
        mock_append = AsyncMock(return_value=True)

        with patch.object(handler.outgoing_client, "send_email", mock_send):
            with patch.object(handler.outgoing_client, "append_to_sent", mock_append):
                await handler.send_email(
                    recipients=["recipient@example.com"],
                    subject="Test",
                    body="Test body",
                )

                mock_send.assert_called_once()
                mock_append.assert_called_once_with(
                    mock_msg,
                    email_settings_with_save_to_sent.incoming,
                    "INBOX.Sent",
                )

    @pytest.mark.asyncio
    async def test_send_email_skips_append_when_disabled(self, email_settings_without_save_to_sent):
        """Test that send_email skips append_to_sent when disabled."""
        handler = ClassicEmailHandler(email_settings_without_save_to_sent)

        mock_msg = MIMEText("Test body")
        mock_send = AsyncMock(return_value=mock_msg)
        mock_append = AsyncMock()

        with patch.object(handler.outgoing_client, "send_email", mock_send):
            with patch.object(handler.outgoing_client, "append_to_sent", mock_append):
                await handler.send_email(
                    recipients=["recipient@example.com"],
                    subject="Test",
                    body="Test body",
                )

                mock_send.assert_called_once()
                mock_append.assert_not_called()


class TestEmailClientAppendToSent:
    """Tests for EmailClient.append_to_sent method."""

    @pytest.fixture
    def email_client(self):
        """Create an EmailClient for testing."""
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
        """Create an incoming EmailServer for testing."""
        return EmailServer(
            user_name="test_user",
            password="test_password",
            host="imap.example.com",
            port=993,
            use_ssl=True,
        )

    @pytest.fixture
    def mock_imap_for_append(self):
        """Create a mock IMAP client for append testing."""
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
    async def test_append_to_sent_success(self, email_client, incoming_server, mock_imap_for_append):
        """Test successful append to sent folder."""
        msg = MIMEText("Test body")
        msg["Subject"] = "Test"
        msg["From"] = "test@example.com"
        msg["To"] = "recipient@example.com"

        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib:
            mock_aioimaplib.IMAP4_SSL.return_value = mock_imap_for_append

            result = await email_client.append_to_sent(msg, incoming_server, "INBOX.Sent")

            assert result is True
            mock_imap_for_append.select.assert_called_with('"INBOX.Sent"')
            mock_imap_for_append.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_to_sent_auto_detect_folder(self, email_client, incoming_server, mock_imap_for_append):
        """Test auto-detection of sent folder."""
        msg = MIMEText("Test body")
        msg["Subject"] = "Test"

        # First folder fails, second succeeds
        mock_imap_for_append.select = AsyncMock(side_effect=[("NO", []), ("OK", [])])

        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib:
            mock_aioimaplib.IMAP4_SSL.return_value = mock_imap_for_append

            result = await email_client.append_to_sent(msg, incoming_server, None)

            assert result is True

    @pytest.mark.asyncio
    async def test_append_to_sent_no_valid_folder(self, email_client, incoming_server, mock_imap_for_append):
        """Test when no valid sent folder is found."""
        msg = MIMEText("Test body")

        # All folders fail
        mock_imap_for_append.select = AsyncMock(return_value=("NO", []))

        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib:
            mock_aioimaplib.IMAP4_SSL.return_value = mock_imap_for_append

            result = await email_client.append_to_sent(msg, incoming_server, None)

            assert result is False

    @pytest.mark.asyncio
    async def test_append_to_sent_append_fails(self, email_client, incoming_server, mock_imap_for_append):
        """Test when append command fails."""
        msg = MIMEText("Test body")

        mock_imap_for_append.append = AsyncMock(return_value=("NO", []))

        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib:
            mock_aioimaplib.IMAP4_SSL.return_value = mock_imap_for_append

            result = await email_client.append_to_sent(msg, incoming_server, "Sent")

            assert result is False

    @pytest.mark.asyncio
    async def test_append_to_sent_login_error(self, email_client, incoming_server, mock_imap_for_append):
        """Test when login fails."""
        msg = MIMEText("Test body")

        mock_imap_for_append.login = AsyncMock(side_effect=Exception("Login failed"))

        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib:
            mock_aioimaplib.IMAP4_SSL.return_value = mock_imap_for_append

            result = await email_client.append_to_sent(msg, incoming_server, "Sent")

            assert result is False

    @pytest.mark.asyncio
    async def test_append_to_sent_non_ssl(self, incoming_server, mock_imap_for_append):
        """Test append with non-SSL connection."""
        server = EmailServer(
            user_name="test_user",
            password="test_password",
            host="smtp.example.com",
            port=465,
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

        msg = MIMEText("Test body")

        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib:
            mock_aioimaplib.IMAP4.return_value = mock_imap_for_append

            result = await client.append_to_sent(msg, incoming_non_ssl, "Sent")

            assert result is True
            mock_aioimaplib.IMAP4.assert_called_once()


class TestEmailClientSendEmailReturnsMessage:
    """Tests for EmailClient.send_email returning the message."""

    @pytest.fixture
    def email_client(self):
        """Create an EmailClient for testing."""
        server = EmailServer(
            user_name="test_user",
            password="test_password",
            host="smtp.example.com",
            port=465,
            use_ssl=True,
        )
        return EmailClient(server, sender="Test <test@example.com>")

    @pytest.mark.asyncio
    async def test_send_email_returns_message(self, email_client):
        """Test that send_email returns the message object."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__ = AsyncMock(return_value=mock_smtp)
        mock_smtp.__aexit__ = AsyncMock(return_value=None)

        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=mock_smtp):
            result = await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
            )

            assert result is not None
            assert result["Subject"] == "Test Subject"
            assert "recipient@example.com" in result["To"]
