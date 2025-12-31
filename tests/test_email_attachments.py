"""Test email attachment functionality."""

from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient


@pytest.fixture
def email_server():
    return EmailServer(
        user_name="test_user",
        password="test_password",
        host="smtp.example.com",
        port=465,
        use_ssl=True,
    )


@pytest.fixture
def email_client(email_server):
    return EmailClient(email_server, sender="Test User <test@example.com>")


class TestEmailAttachments:
    @pytest.mark.asyncio
    async def test_send_email_with_single_attachment(self, email_client, tmp_path):
        """Test sending email with a single attachment."""
        # Create a test file
        test_file = tmp_path / "document.pdf"
        test_file.write_bytes(b"PDF content here")

        # Mock SMTP
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__ = AsyncMock(return_value=mock_smtp)
        mock_smtp.__aexit__ = AsyncMock()

        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test with attachment",
                body="Please see attached file",
                attachments=[str(test_file)],
            )

            # Verify SMTP methods were called
            mock_smtp.login.assert_called_once()
            mock_smtp.send_message.assert_called_once()

            # Get the message that was sent
            call_args = mock_smtp.send_message.call_args
            message = call_args[0][0]

            # Verify message is multipart (required for attachments)
            assert message.is_multipart()
            assert "document.pdf" in str(message)

    @pytest.mark.asyncio
    async def test_send_email_with_multiple_attachments(self, email_client, tmp_path):
        """Test sending email with multiple attachments."""
        # Create multiple test files
        file1 = tmp_path / "document1.pdf"
        file1.write_bytes(b"PDF content 1")

        file2 = tmp_path / "image.png"
        file2.write_bytes(b"PNG content")

        file3 = tmp_path / "data.csv"
        file3.write_text("col1,col2\nval1,val2")

        # Mock SMTP
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__ = AsyncMock(return_value=mock_smtp)
        mock_smtp.__aexit__ = AsyncMock()

        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test with multiple attachments",
                body="Please see attached files",
                attachments=[str(file1), str(file2), str(file3)],
            )

            mock_smtp.send_message.assert_called_once()
            message = mock_smtp.send_message.call_args[0][0]

            assert message.is_multipart()
            message_str = str(message)
            assert "document1.pdf" in message_str
            assert "image.png" in message_str
            assert "data.csv" in message_str

    @pytest.mark.asyncio
    async def test_send_email_without_attachments(self, email_client):
        """Test sending email without attachments (backward compatibility)."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__ = AsyncMock(return_value=mock_smtp)
        mock_smtp.__aexit__ = AsyncMock()

        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test without attachment",
                body="Simple email",
            )

            mock_smtp.send_message.assert_called_once()
            message = mock_smtp.send_message.call_args[0][0]

            # Without attachments, message should not be multipart
            assert not message.is_multipart()

    @pytest.mark.asyncio
    async def test_send_email_attachment_file_not_found(self, email_client):
        """Test error handling when attachment file doesn't exist."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__ = AsyncMock(return_value=mock_smtp)
        mock_smtp.__aexit__ = AsyncMock()

        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=mock_smtp):
            with pytest.raises(FileNotFoundError, match="Attachment file not found"):
                await email_client.send_email(
                    recipients=["recipient@example.com"],
                    subject="Test",
                    body="Test",
                    attachments=["/nonexistent/file.pdf"],
                )

    @pytest.mark.asyncio
    async def test_send_email_attachment_is_directory(self, email_client, tmp_path):
        """Test error handling when attachment path is a directory."""
        # Create a directory
        test_dir = tmp_path / "test_directory"
        test_dir.mkdir()

        mock_smtp = AsyncMock()
        mock_smtp.__aenter__ = AsyncMock(return_value=mock_smtp)
        mock_smtp.__aexit__ = AsyncMock()

        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=mock_smtp):
            with pytest.raises(ValueError, match="Attachment path is not a file"):
                await email_client.send_email(
                    recipients=["recipient@example.com"],
                    subject="Test",
                    body="Test",
                    attachments=[str(test_dir)],
                )

    @pytest.mark.asyncio
    async def test_send_email_html_with_attachments(self, email_client, tmp_path):
        """Test sending HTML email with attachments."""
        test_file = tmp_path / "report.pdf"
        test_file.write_bytes(b"Report content")

        mock_smtp = AsyncMock()
        mock_smtp.__aenter__ = AsyncMock(return_value=mock_smtp)
        mock_smtp.__aexit__ = AsyncMock()

        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="HTML email with attachment",
                body="<h1>Report</h1><p>See attached</p>",
                html=True,
                attachments=[str(test_file)],
            )

            mock_smtp.send_message.assert_called_once()
            message = mock_smtp.send_message.call_args[0][0]

            assert message.is_multipart()
            assert "report.pdf" in str(message)

    @pytest.mark.asyncio
    async def test_mime_type_detection(self, email_client, tmp_path):
        """Test MIME type detection for different file types."""
        # Create files with different extensions
        files = {
            "document.pdf": b"PDF",
            "image.jpg": b"JPEG",
            "data.json": b'{"key": "value"}',
            "archive.zip": b"ZIP",
            "text.txt": b"Text",
        }

        test_files = []
        for filename, content in files.items():
            file_path = tmp_path / filename
            file_path.write_bytes(content)
            test_files.append(str(file_path))

        mock_smtp = AsyncMock()
        mock_smtp.__aenter__ = AsyncMock(return_value=mock_smtp)
        mock_smtp.__aexit__ = AsyncMock()

        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test MIME types",
                body="Various file types",
                attachments=test_files,
            )

            mock_smtp.send_message.assert_called_once()
            message = mock_smtp.send_message.call_args[0][0]

            # Verify all files are in the message
            message_str = str(message)
            for filename in files:
                assert filename in message_str


class TestDownloadAttachmentMailboxParam:
    """Tests for download_attachment mailbox parameter."""

    @pytest.mark.asyncio
    async def test_download_attachment_default_mailbox(self, email_client, tmp_path):
        """Test download_attachment uses INBOX by default."""
        import asyncio

        save_path = str(tmp_path / "attachment.pdf")

        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock(return_value=("OK", [b"1"]))
        mock_imap.logout = AsyncMock()

        # Mock _fetch_email_with_formats to return None (will raise ValueError)
        with patch.object(email_client, "_fetch_email_with_formats", return_value=None):
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                with pytest.raises(ValueError):
                    await email_client.download_attachment(
                        email_id="123",
                        attachment_name="document.pdf",
                        save_path=save_path,
                    )

                # Verify select was called with quoted INBOX
                mock_imap.select.assert_called_once_with('"INBOX"')

    @pytest.mark.asyncio
    async def test_download_attachment_custom_mailbox(self, email_client, tmp_path):
        """Test download_attachment with custom mailbox parameter."""
        import asyncio

        save_path = str(tmp_path / "attachment.pdf")

        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock(return_value=("OK", [b"1"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_fetch_email_with_formats", return_value=None):
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                with pytest.raises(ValueError):
                    await email_client.download_attachment(
                        email_id="123",
                        attachment_name="document.pdf",
                        save_path=save_path,
                        mailbox="All Mail",
                    )

                # Verify select was called with quoted custom mailbox
                mock_imap.select.assert_called_once_with('"All Mail"')

    @pytest.mark.asyncio
    async def test_download_attachment_special_folder(self, email_client, tmp_path):
        """Test download_attachment with special folder like [Gmail]/Sent Mail."""
        import asyncio

        save_path = str(tmp_path / "attachment.pdf")

        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock(return_value=("OK", [b"1"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_fetch_email_with_formats", return_value=None):
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                with pytest.raises(ValueError):
                    await email_client.download_attachment(
                        email_id="123",
                        attachment_name="document.pdf",
                        save_path=save_path,
                        mailbox="[Gmail]/Sent Mail",
                    )

                # Verify select was called with quoted special folder
                mock_imap.select.assert_called_once_with('"[Gmail]/Sent Mail"')
