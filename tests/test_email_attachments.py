"""Test email attachment functionality."""

import re
from email import encoders
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from unittest.mock import AsyncMock, MagicMock, patch

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


def _build_apple_mail_inline_image(image_bytes: bytes = b"\x89PNG\r\n\x1a\n_fake_png_") -> bytes:
    """Build a multipart/mixed email mimicking Apple Mail (iOS) sending a photo.

    Apple Mail attaches images with ``Content-Disposition: inline`` plus a
    ``filename`` parameter — not ``attachment``. The strict
    ``"attachment" in content_disposition`` check used to miss these entirely.
    """
    msg = MIMEMultipart("mixed")
    msg["Subject"] = "Ausflug"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Message-ID"] = make_msgid(domain="example.com")
    msg["Date"] = "Fri, 8 May 2026 19:17:09 +0200"

    # Body part
    msg.attach(MIMEText("Mach einen passenden Termin im Familienkalender. Siehe Attachment", "plain", "utf-8"))

    # Inline-disposition image with filename — exactly how iOS Mail sends photos.
    image_part = MIMEBase("image", "png")
    image_part.set_payload(image_bytes)
    encoders.encode_base64(image_part)
    image_part.add_header("Content-Disposition", "inline", filename="ausflug.png")
    msg.attach(image_part)

    return msg.as_bytes()


def _build_email_with_explicit_attachment() -> bytes:
    """Build a multipart/mixed email with a Content-Disposition: attachment part."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = "Report"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Date"] = "Fri, 8 May 2026 19:17:09 +0200"
    msg.attach(MIMEText("Please see attached report.", "plain", "utf-8"))

    pdf_part = MIMEApplication(b"%PDF-1.4 fake pdf bytes", _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "attachment", filename="report.pdf")
    msg.attach(pdf_part)
    return msg.as_bytes()


def _build_email_with_no_filename_inline() -> bytes:
    """Inline part without filename (e.g. text/html body) must NOT count as attachment."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Hello"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Date"] = "Fri, 8 May 2026 19:17:09 +0200"
    msg.attach(MIMEText("Hello there", "plain", "utf-8"))
    msg.attach(MIMEText("<p>Hello there</p>", "html", "utf-8"))
    return msg.as_bytes()


class TestParseAttachmentsInBody:
    """Tests for attachment detection in ``_parse_email_data``.

    Regression for Apple Mail / iOS-style inline-disposition photos that the
    legacy strict ``Content-Disposition: attachment`` check was ignoring.
    """

    def test_inline_image_with_filename_is_detected(self, email_client):
        """Apple-Mail-style inline photo (Content-Disposition: inline) is reported."""
        raw_email = _build_apple_mail_inline_image()

        result = email_client._parse_email_data(raw_email, email_id="42")

        assert result["attachments"] == ["ausflug.png"]
        assert result["body"].startswith("Mach einen passenden Termin")
        assert result["message_id"] is not None

    def test_explicit_attachment_still_detected(self, email_client):
        """Backward compatibility: classic Content-Disposition: attachment still works."""
        raw_email = _build_email_with_explicit_attachment()

        result = email_client._parse_email_data(raw_email, email_id="43")

        assert result["attachments"] == ["report.pdf"]
        assert result["body"].startswith("Please see attached report")

    def test_alternative_parts_without_filename_are_not_attachments(self, email_client):
        """text/plain + text/html alternatives have no filenames and must not be reported."""
        raw_email = _build_email_with_no_filename_inline()

        result = email_client._parse_email_data(raw_email, email_id="44")

        assert result["attachments"] == []
        assert result["body"] == "Hello there"

    def test_is_attachment_part_helper(self, email_client):
        """Direct unit test of the new classifier helper."""
        attachment_email = _build_email_with_explicit_attachment()
        inline_email = _build_apple_mail_inline_image()
        plain_email = _build_email_with_no_filename_inline()

        from email.parser import BytesParser
        from email.policy import default

        for raw, expected_filenames in (
            (attachment_email, {"report.pdf"}),
            (inline_email, {"ausflug.png"}),
            (plain_email, set()),
        ):
            msg = BytesParser(policy=default).parsebytes(raw)
            found = {
                part.get_filename()
                for part in msg.walk()
                if email_client._is_attachment_part(part) and part.get_filename()
            }
            assert found == expected_filenames

    def test_is_attachment_part_ignores_non_string_filename(self, email_client):
        """Truthiness alone must not make a part look like an attachment."""
        part = MagicMock()
        part.get.return_value = ""
        part.get_filename.return_value = MagicMock()

        assert email_client._is_attachment_part(part) is False


class TestParseHeadersExposesMessageId:
    """``_parse_headers`` now surfaces Message-ID for use in metadata listings."""

    def test_message_id_is_included_when_present(self, email_client):
        raw_headers = (
            b"From: sender@example.com\r\n"
            b"To: recipient@example.com\r\n"
            b"Subject: With Message-ID\r\n"
            b"Date: Fri, 8 May 2026 19:17:09 +0200\r\n"
            b"Message-ID: <abc-123@example.com>\r\n"
            b"\r\n"
        )

        result = email_client._parse_headers("99", raw_headers)

        assert result is not None
        assert result["message_id"] == "<abc-123@example.com>"
        assert result["attachments"] == []

    def test_message_id_is_none_when_missing(self, email_client):
        raw_headers = b"Subject: No Message-ID\r\n\r\n"

        result = email_client._parse_headers("100", raw_headers)

        assert result is not None
        assert result["message_id"] is None


class TestDownloadInlineAttachment:
    """``download_attachment`` finds inline-disposition attachments by filename."""

    @staticmethod
    def _mock_imap():
        import asyncio

        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock(return_value=("OK", [b"1"]))
        mock_imap.logout = AsyncMock()
        return mock_imap

    @pytest.mark.asyncio
    async def test_download_inline_attachment_succeeds(self, email_client, tmp_path):
        """An iOS-style inline photo can be downloaded via download_attachment."""
        save_path = str(tmp_path / "ausflug.png")
        raw_email = _build_apple_mail_inline_image(b"\x89PNG\r\n\x1a\nactual_inline_png_bytes")

        mock_imap = self._mock_imap()

        async def _fake_fetch(_imap, _email_id):
            # Mimic ``_fetch_email_with_formats`` returning a list whose entry [1]
            # is a bytearray of the raw email body — the shape ``_extract_raw_email``
            # expects.
            return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

        with patch.object(email_client, "_fetch_email_with_formats", side_effect=_fake_fetch):
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                result = await email_client.download_attachment(
                    email_id="1",
                    attachment_name="ausflug.png",
                    save_path=save_path,
                )

        assert result["attachment_name"] == "ausflug.png"
        assert result["mime_type"] == "image/png"
        assert result["size"] > 0
        # The file was actually written to disk
        from pathlib import Path

        assert Path(save_path).exists()
        assert Path(save_path).read_bytes().startswith(b"\x89PNG")

    @pytest.mark.asyncio
    async def test_download_raises_when_attachment_name_does_not_match(self, email_client, tmp_path):
        """Attachment parts with other filenames are skipped."""
        raw_email = _build_email_with_explicit_attachment()

        mock_imap = self._mock_imap()

        async def _fake_fetch(_imap, _email_id):
            return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

        with patch.object(email_client, "_fetch_email_with_formats", side_effect=_fake_fetch):
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                with pytest.raises(ValueError, match=re.escape("Attachment 'missing.pdf' not found")):
                    await email_client.download_attachment(
                        email_id="1",
                        attachment_name="missing.pdf",
                        save_path=str(tmp_path / "missing.pdf"),
                    )

    @pytest.mark.asyncio
    async def test_download_raises_for_non_multipart_email(self, email_client, tmp_path):
        """Single-part emails have no attachment parts to download."""
        raw_email = MIMEText("No attachments here", "plain", "utf-8").as_bytes()

        mock_imap = self._mock_imap()

        async def _fake_fetch(_imap, _email_id):
            return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

        with patch.object(email_client, "_fetch_email_with_formats", side_effect=_fake_fetch):
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                with pytest.raises(ValueError, match=re.escape("Attachment 'missing.pdf' not found")):
                    await email_client.download_attachment(
                        email_id="1",
                        attachment_name="missing.pdf",
                        save_path=str(tmp_path / "missing.pdf"),
                    )
