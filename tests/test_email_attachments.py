"""Test email attachment functionality."""

import asyncio
import base64
import re
import unicodedata
from email import encoders
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.policy import SMTP as SMTP_POLICY
from email.policy import SMTPUTF8 as SMTPUTF8_POLICY
from email.policy import default
from email.utils import make_msgid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosmtplib.email import flatten_message

from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import (
    EmailClient,
    _as_modern_smtp_message,
    _format_forwarded_text,
    _message_requires_smtputf8,
    _serialize_message_for_imap_append,
    normalize_forwarded_part,
)


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
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
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
    async def test_download_attachment_raises_on_select_failure(self, email_client, tmp_path):
        """Test download stops when mailbox selection fails."""
        import asyncio

        save_path = str(tmp_path / "attachment.pdf")

        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("NO", [b"[NONEXISTENT] Unknown Mailbox: Archive"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_fetch_email_with_formats", return_value=None) as mock_fetch:
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                with pytest.raises(RuntimeError) as exc_info:
                    await email_client.download_attachment(
                        email_id="123",
                        attachment_name="document.pdf",
                        save_path=save_path,
                        mailbox="Archive",
                    )

        message = str(exc_info.value)
        assert "SELECT mailbox Archive failed" in message
        assert "NO" in message
        assert "[NONEXISTENT] Unknown Mailbox: Archive" not in message
        mock_fetch.assert_not_called()
        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_attachment_custom_mailbox(self, email_client, tmp_path):
        """Test download_attachment with custom mailbox parameter."""
        import asyncio

        save_path = str(tmp_path / "attachment.pdf")

        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
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
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
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


def _build_email_with_unicode_attachment(filename: str, payload: bytes = b"xlsx bytes") -> bytes:
    """Build a multipart/mixed email with a non-ASCII attachment filename."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = "Unicode filename"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Date"] = "Fri, 8 May 2026 19:17:09 +0200"
    msg.attach(MIMEText("Please see attached spreadsheet.", "plain", "utf-8"))

    xlsx_part = MIMEApplication(payload, _subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    xlsx_part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(xlsx_part)
    return msg.as_bytes()


def _build_email_with_related_inline_pdf(payload: bytes = b"%PDF-1.4 fake pdf bytes") -> bytes:
    """Build a multipart/related email with an inline PDF attachment."""
    msg = MIMEMultipart("related")
    msg["Subject"] = "Inline PDF"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Date"] = "Fri, 8 May 2026 19:17:09 +0200"
    msg.attach(MIMEText("See the inline object.", "plain", "utf-8"))

    pdf_part = MIMEApplication(payload, _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "inline", filename="0421.pdf")
    pdf_part.add_header("Content-ID", "<pdf-0421@example.com>")
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

    def test_unicode_attachment_filename_is_decoded(self, email_client):
        """Non-ASCII attachment filenames are exposed as decoded strings."""
        filename = "Actividades operacionales bienal MC rev 1 2 - con Análisis 1.xlsx"
        raw_email = _build_email_with_unicode_attachment(filename)

        result = email_client._parse_email_data(raw_email, email_id="44")

        assert result["attachments"] == [filename]

    def test_multipart_related_inline_pdf_with_filename_is_detected(self, email_client):
        """multipart/related inline binary parts with filenames are exposed."""
        raw_email = _build_email_with_related_inline_pdf()

        result = email_client._parse_email_data(raw_email, email_id="45")

        assert result["attachments"] == ["0421.pdf"]
        assert result["body"] == "See the inline object."

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


@pytest.mark.asyncio
async def test_fetch_attachment_is_not_bounded_by_download_ceiling(email_client) -> None:
    raw_email = _build_email_with_explicit_attachment()
    mock_imap = AsyncMock()
    mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    mock_imap.select = AsyncMock(return_value=("OK", [b"1"]))
    mock_imap.logout = AsyncMock()

    async def fake_fetch(_imap, _email_id):
        return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

    with (
        patch.object(email_client, "_connect_imap", AsyncMock(return_value=mock_imap)),
        patch.object(email_client, "_fetch_email_with_formats", side_effect=fake_fetch),
        patch("mcp_email_server.emails.classic.MAX_ATTACHMENT_BYTES", 4),
    ):
        result = await email_client.fetch_attachment("1", "report.pdf")

    assert result["content"] == b"%PDF-1.4 fake pdf bytes"


@pytest.mark.asyncio
async def test_download_attachment_retains_download_ceiling(email_client, tmp_path) -> None:
    destination = tmp_path / "report.pdf"
    with (
        patch.object(
            email_client,
            "fetch_attachment",
            AsyncMock(
                return_value={
                    "email_id": "1",
                    "attachment_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "content": b"12345",
                }
            ),
        ),
        patch("mcp_email_server.emails.classic.MAX_ATTACHMENT_BYTES", 4),
        pytest.raises(ValueError, match="attachment exceeds 4 bytes"),
    ):
        await email_client.download_attachment("1", "report.pdf", str(destination))

    assert not destination.exists()


class TestDownloadInlineAttachment:
    """``download_attachment`` finds inline-disposition attachments by filename."""

    @staticmethod
    def _mock_imap():
        import asyncio

        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
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
    async def test_download_unicode_attachment_with_nfd_request_name_succeeds(self, email_client, tmp_path):
        """download_attachment normalizes Unicode filenames before matching."""
        filename = "Actividades operacionales bienal MC rev 1 2 - con Análisis 1.xlsx"
        raw_email = _build_email_with_unicode_attachment(filename, payload=b"spreadsheet bytes")
        save_path = tmp_path / "analysis.xlsx"

        mock_imap = self._mock_imap()

        async def _fake_fetch(_imap, _email_id):
            return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

        with patch.object(email_client, "_fetch_email_with_formats", side_effect=_fake_fetch):
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                result = await email_client.download_attachment(
                    email_id="1",
                    attachment_name=unicodedata.normalize("NFD", filename),
                    save_path=str(save_path),
                )

        assert result["attachment_name"] == unicodedata.normalize("NFD", filename)
        assert result["mime_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert save_path.read_bytes() == b"spreadsheet bytes"

    @pytest.mark.asyncio
    async def test_download_multipart_related_inline_pdf_succeeds(self, email_client, tmp_path):
        """download_attachment retrieves multipart/related inline binary parts."""
        raw_email = _build_email_with_related_inline_pdf(payload=b"%PDF inline bytes")
        save_path = tmp_path / "0421.pdf"

        mock_imap = self._mock_imap()

        async def _fake_fetch(_imap, _email_id):
            return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

        with patch.object(email_client, "_fetch_email_with_formats", side_effect=_fake_fetch):
            with patch.object(email_client, "imap_class", return_value=mock_imap):
                result = await email_client.download_attachment(
                    email_id="1",
                    attachment_name="0421.pdf",
                    save_path=str(save_path),
                )

        assert result["attachment_name"] == "0421.pdf"
        assert result["mime_type"] == "application/pdf"
        assert save_path.read_bytes() == b"%PDF inline bytes"

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


class TestDownloadAttachmentSenderAllowlist:
    """download_attachment must honor the sender allowlist (read-path protection)."""

    @staticmethod
    def _mock_imap():
        import asyncio

        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", [b"1"]))
        mock_imap.logout = AsyncMock()
        return mock_imap

    @pytest.mark.asyncio
    async def test_blocked_sender_never_fetches_body(self, email_client, tmp_path):
        """A blocked sender's attachment download never reads the full message body."""
        mock_imap = self._mock_imap()
        with (
            patch.object(
                email_client, "_batch_fetch_senders", AsyncMock(return_value={"1": "evil@blocked.com"})
            ) as mock_senders,
            patch.object(email_client, "_fetch_email_with_formats", AsyncMock()) as mock_fetch,
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            with pytest.raises(ValueError, match=re.escape("Failed to fetch email with UID 1")):
                await email_client.download_attachment(
                    email_id="1",
                    attachment_name="out.png",
                    save_path=str(tmp_path / "out.png"),
                    allowed_senders=["*@allowed.com"],
                )
        mock_senders.assert_awaited_once()
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_indistinguishable_from_missing(self, email_client, tmp_path):
        """A blocked-existing UID and a nonexistent UID raise the identical error (no oracle)."""
        save_path = str(tmp_path / "out.png")

        async def _run(senders):
            mock_imap = self._mock_imap()
            with (
                patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
                patch.object(email_client, "_fetch_email_with_formats", AsyncMock(return_value=None)) as mock_fetch,
                patch.object(email_client, "imap_class", return_value=mock_imap),
            ):
                with pytest.raises(ValueError) as exc:
                    await email_client.download_attachment(
                        email_id="1",
                        attachment_name="out.png",
                        save_path=save_path,
                        allowed_senders=["*@allowed.com"],
                    )
                mock_fetch.assert_not_called()
            return str(exc.value)

        blocked_msg = await _run({"1": "evil@blocked.com"})  # exists but blocked
        missing_msg = await _run({})  # does not exist
        assert blocked_msg == missing_msg == "Failed to fetch email with UID 1"

    @pytest.mark.asyncio
    async def test_allowed_sender_downloads(self, email_client, tmp_path):
        """An allowed sender's attachment is fetched and saved."""
        raw_email = _build_apple_mail_inline_image(b"\x89PNG\r\n\x1a\ninline")
        mock_imap = self._mock_imap()

        async def _fake_fetch(_imap, _email_id):
            return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value={"1": "ok@allowed.com"})),
            patch.object(email_client, "_fetch_email_with_formats", side_effect=_fake_fetch) as mock_fetch,
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            result = await email_client.download_attachment(
                email_id="1",
                attachment_name="ausflug.png",
                save_path=str(tmp_path / "ausflug.png"),
                allowed_senders=["*@allowed.com"],
            )
        assert result["attachment_name"] == "ausflug.png"
        mock_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_allowlist_skips_sender_check(self, email_client, tmp_path):
        """With no allowlist, download_attachment does no sender fetch (backwards-compatible)."""
        raw_email = _build_apple_mail_inline_image(b"\x89PNG\r\n\x1a\ninline")
        mock_imap = self._mock_imap()

        async def _fake_fetch(_imap, _email_id):
            return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock()) as mock_senders,
            patch.object(email_client, "_fetch_email_with_formats", side_effect=_fake_fetch),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            result = await email_client.download_attachment(
                email_id="1",
                attachment_name="ausflug.png",
                save_path=str(tmp_path / "ausflug.png"),
                allowed_senders=[],
            )
        assert result["attachment_name"] == "ausflug.png"
        mock_senders.assert_not_called()


# ---------------------------------------------------------------------------
# forward_email source fixtures
# ---------------------------------------------------------------------------
#
# These are raw wire bytes rather than ``MIMEMultipart`` builders on purpose.
# The RFC 2231 regression they guard only reproduces when the source header
# arrives on ONE unfolded line long enough to need refolding; a pre-folded
# fixture passes even against a broken implementation.

_FORWARD_LONG_FILENAME = "Quartalsbericht Übersicht München Grüße 2026 Jahresabschluss Anlage.xlsx"

_FORWARD_RFC2231_DISPOSITION = b"Content-Disposition: attachment; filename*=utf-8''" + _FORWARD_LONG_FILENAME.replace(
    " ", "%20"
).replace("Ü", "%C3%9C").replace("ü", "%C3%BC").replace("ß", "%C3%9F").encode("ascii")

_FORWARD_INNER_MESSAGE = (
    b"Message-ID: <inner-capsule@example.com>\r\n"
    b"From: original@example.com\r\n"
    b"To: nested@example.com\r\n"
    b"Subject: Inner capsule\r\n"
    b"Content-Type: text/plain; charset=us-ascii\r\n"
    b"\r\n"
    b"inner capsule body\r\n"
)

_FORWARD_INLINE_PNG = b"iVBORw0KGgoAAAANSUhEUg=="


def _build_forward_source_email() -> bytes:
    """Build a source message carrying all six forwardable part shapes."""
    return (
        b"MIME-Version: 1.0\r\n"
        b"From: Sender Name <sender@example.com>\r\n"
        b"To: rcpt@example.com\r\n"
        b"Cc: cc@example.com\r\n"
        b"Subject: Quarterly package\r\n"
        b"Date: Fri, 8 May 2026 19:17:09 +0200\r\n"
        b"Message-ID: <src@example.com>\r\n"
        b'Content-Type: multipart/mixed; boundary="OUTER"\r\n'
        b"\r\n"
        # Body leaf, never re-attached.
        b"--OUTER\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"SGFsbG8gV2VsdA==\r\n"
        # 1. RFC 2231 filename on one unfolded line.
        b"--OUTER\r\n"
        b'Content-Type: application/vnd.ms-excel; name="sheet.xlsx"\r\n'
        b"Content-Transfer-Encoding: base64\r\n" + _FORWARD_RFC2231_DISPOSITION + b"\r\n"
        b"\r\n"
        b"c3ByZWFkc2hlZXQ=\r\n"
        # 2. Zero-byte attachment.
        b"--OUTER\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="empty.bin"\r\n'
        b"\r\n"
        b"\r\n"
        # 3. Encapsulated message.
        b"--OUTER\r\n"
        b"Content-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="capsule.eml"\r\n'
        b"\r\n" + _FORWARD_INNER_MESSAGE +
        # 4. Nested multipart/related subtree carried as one attachment root.
        b"--OUTER\r\n"
        b'Content-Type: multipart/related; boundary="INNERREL"; type="text/html"\r\n'
        b'Content-Disposition: attachment; filename="gallery.mhtml"\r\n'
        b"\r\n"
        b"--INNERREL\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 7bit\r\n"
        b"\r\n"
        b'<p><img src="cid:pic@example.com"></p>\r\n'
        b"--INNERREL\r\n"
        b'Content-Type: image/png; name="pic.png"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-ID: <pic@example.com>\r\n"
        b'Content-Disposition: inline; filename="pic.png"\r\n'
        b"\r\n" + _FORWARD_INLINE_PNG + b"\r\n"
        b"--INNERREL--\r\n"
        # 5. Apple-Mail style inline image at the top level.
        b"--OUTER\r\n"
        b'Content-Type: image/png; name="inline.png"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-ID: <inline-pic@example.com>\r\n"
        b'Content-Disposition: inline; filename="inline.png"\r\n'
        b"\r\n" + _FORWARD_INLINE_PNG + b"\r\n"
        # 6. Ordinary explicit attachment.
        b"--OUTER\r\n"
        b'Content-Type: application/pdf; name="report.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="report.pdf"\r\n'
        b"\r\n"
        b"JVBERi0xLjQ=\r\n"
        b"--OUTER--\r\n"
    )


def _build_malformed_forward_source() -> bytes:
    """Build a source message whose parts are all non-conformant in some way."""
    return (
        b"MIME-Version: 1.0\r\n"
        b"From: sender@example.com\r\n"
        b"To: rcpt@example.com\r\n"
        b"Subject: Malformed package\r\n"
        b"Date: Fri, 8 May 2026 19:17:09 +0200\r\n"
        b'Content-Type: multipart/mixed; boundary="M"\r\n'
        b"\r\n"
        b"--M\r\n"
        b"Content-Type: text/plain; charset=us-ascii\r\n"
        b"\r\n"
        b"body\r\n"
        # Raw latin-1 octets in an unencoded header plus a raw UTF-8 quoted filename.
        b"--M\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"X-Legacy-Note: caf\xe9 r\xe9sum\xe9\r\n"
        b'Content-Disposition: attachment; filename="Gr\xc3\xbc\xc3\x9fe.xlsx"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"YWJj\r\n"
        # RFC 2047 encoded-word where RFC 2231 is required.
        b"--M\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?B?w5xiZXJzaWNodC5wZGY=?="\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"JVBERg==\r\n"
        # Continued RFC 2231 parameter.
        b"--M\r\n"
        b"Content-Type: application/zip\r\n"
        b"Content-Disposition: attachment; filename*0*=utf-8''Sehr%20langer;\r\n"
        b" filename*1*=%20Name%20mit%20Fortsetzung.zip\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"UEsD\r\n"
        # Corrupt base64 payload.
        b"--M\r\n"
        b"Content-Type: image/jpeg\r\n"
        b'Content-Disposition: attachment; filename="broken.jpg"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"!!!not base64!!!\r\n"
        # No Content-Type at all.
        b"--M\r\n"
        b'Content-Disposition: attachment; filename="typeless.dat"\r\n'
        b"\r\n"
        b"plain octets\r\n"
        b"--M--\r\n"
    )


def _forward_source_parts(email_client) -> tuple[list, list]:
    """Return (source attachment roots, normalized copies) for the six-part fixture."""
    source = BytesParser(policy=default).parsebytes(_build_forward_source_email())
    roots = [part for part, is_attachment in email_client._iter_content_parts(source) if is_attachment]
    return roots, [normalize_forwarded_part(part) for part in roots]


def _content_disposition_lines(wire: bytes) -> list[bytes]:
    """Return every Content-Disposition header of a flattened message, folds included."""
    return re.findall(rb"Content-Disposition:[^\r\n]*(?:\r?\n[ \t][^\r\n]*)*", wire)


def _serializations(message) -> dict[str, bytes]:
    """Flatten one composed message through every send path the provider uses."""
    return {
        # send_email_with_outcome, ASCII envelope (classic.py:2262 policy selection)
        "smtp": message.as_bytes(policy=SMTP_POLICY),
        # send_email_with_outcome, RFC 6532 envelope
        "smtputf8": message.as_bytes(policy=SMTPUTF8_POLICY),
        # send_email -> aiosmtplib.send_message re-flatten (classic.py:2466)
        "aiosmtplib": flatten_message(_as_modern_smtp_message(message), utf8=False, cte_type="8bit"),
        # IMAP APPEND of the Sent copy
        "imap_append": _serialize_message_for_imap_append(message),
    }


class TestNormalizeForwardedPart:
    """One source part survives the compat32 round trip with its MIME identity intact."""

    def test_all_six_part_shapes_round_trip(self, email_client):
        """Type, params, CTE, Content-ID, disposition and filename all match the source."""
        roots, normalized = _forward_source_parts(email_client)
        assert [part.get_content_type() for part in roots] == [
            "application/vnd.ms-excel",
            "application/octet-stream",
            "message/rfc822",
            "multipart/related",
            "image/png",
            "application/pdf",
        ]

        for source_part, copied in zip(roots, normalized, strict=True):
            assert copied.get_content_type() == source_part.get_content_type()
            # Full parameter list, so charset/name/boundary loss is caught too.
            assert copied.get_params() == source_part.get_params()
            assert copied.get("Content-Transfer-Encoding") == source_part.get("Content-Transfer-Encoding")
            assert copied.get("Content-ID") == source_part.get("Content-ID")
            assert copied.get_content_disposition() == source_part.get_content_disposition()
            assert copied.get_filename() == source_part.get_filename()

    def test_zero_byte_attachment_is_carried_not_dropped(self, email_client):
        """A zero-byte part decodes to b"" — a falsy value a truthiness check would drop."""
        _, normalized = _forward_source_parts(email_client)
        empty = normalized[1]
        assert empty.get_filename() == "empty.bin"
        assert empty.get_payload(decode=True) == b""

    def test_message_rfc822_keeps_inner_message_and_gains_no_encoding(self, email_client):
        """An encapsulated message decodes to None and must not be re-encoded."""
        _, normalized = _forward_source_parts(email_client)
        capsule = normalized[2]
        assert capsule.get_content_type() == "message/rfc822"
        assert capsule.get_payload(decode=True) is None
        assert capsule.get("Content-Transfer-Encoding") is None

        inner = capsule.get_payload(0)
        assert inner["Message-ID"] == "<inner-capsule@example.com>"
        assert inner.get_payload(decode=True) == b"inner capsule body"

    def test_nested_multipart_related_subtree_is_preserved(self, email_client):
        """The whole related subtree travels, not just its flattened leaves."""
        _, normalized = _forward_source_parts(email_client)
        gallery = normalized[3]
        assert gallery.get_content_type() == "multipart/related"
        assert gallery.get_param("type") == "text/html"

        children = gallery.get_payload()
        assert [child.get_content_type() for child in children] == ["text/html", "image/png"]
        image = children[1]
        assert image.get("Content-ID") == "<pic@example.com>"
        assert image.get_payload(decode=True) == base64.b64decode(_FORWARD_INLINE_PNG)

    def test_attachment_roots_exclude_body_leaves(self, email_client):
        """The source's own text body is not re-attached as a part."""
        _, normalized = _forward_source_parts(email_client)
        assert all(part.get_content_type() != "text/plain" for part in normalized)


class TestForwardedPartSerialization:
    """Re-attached parts must reach the wire identically on every send path."""

    def test_rfc2231_filename_bytes_are_identical_across_send_paths(self, email_client):
        """The unfolded RFC 2231 disposition refolds the same way everywhere."""
        # Guard the guard: a pre-folded fixture would pass even when broken.
        assert len(_FORWARD_RFC2231_DISPOSITION) >= 86
        assert b"\n" not in _FORWARD_RFC2231_DISPOSITION

        _, normalized = _forward_source_parts(email_client)
        message = email_client.compose_message(
            ["dest@example.com"], "Fwd: Quarterly package", "note", extra_parts=normalized
        )
        wires = _serializations(message)

        dispositions = {name: _content_disposition_lines(wire) for name, wire in wires.items()}
        assert len({tuple(lines) for lines in dispositions.values()}) == 1, dispositions

        for name, wire in wires.items():
            # The source labelled the filename utf-8, so no path may relabel it.
            assert b"unknown-8bit" not in wire, name
            assert b"utf-8''Quartalsbericht" in wire, name

    def test_smtputf8_requirement_is_unchanged_by_forwarded_parts(self, email_client):
        """_message_requires_smtputf8 reads outer headers only."""
        _, normalized = _forward_source_parts(email_client)
        without = email_client.compose_message(["dest@example.com"], "Fwd: Quarterly package", "note")
        with_parts = email_client.compose_message(
            ["dest@example.com"], "Fwd: Quarterly package", "note", extra_parts=normalized
        )
        assert _message_requires_smtputf8(with_parts) == _message_requires_smtputf8(without)

    def test_forwarded_payloads_survive_the_smtp_flatten(self, email_client):
        """Every re-attached payload is byte-identical after a real serialization round trip."""
        _, normalized = _forward_source_parts(email_client)
        message = email_client.compose_message(
            ["dest@example.com"], "Fwd: Quarterly package", "note", extra_parts=normalized
        )
        delivered = BytesParser(policy=default).parsebytes(message.as_bytes(policy=SMTP_POLICY))
        delivered_parts = [part for part, is_attachment in email_client._iter_content_parts(delivered) if is_attachment]

        assert [part.get_content_type() for part in delivered_parts] == [part.get_content_type() for part in normalized]
        assert delivered_parts[1].get_payload(decode=True) == b""
        assert delivered_parts[4].get("Content-ID") == "<inline-pic@example.com>"
        assert delivered_parts[4].get_payload(decode=True) == base64.b64decode(_FORWARD_INLINE_PNG)
        assert delivered_parts[5].get_payload(decode=True) == base64.b64decode(b"JVBERi0xLjQ=")


class TestMalformedForwardSource:
    """Non-conformant source parts must not raise and must not be silently rewritten."""

    def test_malformed_parts_round_trip_without_raising(self, email_client):
        source = BytesParser(policy=default).parsebytes(_build_malformed_forward_source())
        roots = [part for part, is_attachment in email_client._iter_content_parts(source) if is_attachment]
        normalized = [normalize_forwarded_part(part) for part in roots]

        assert [part.get_content_type() for part in normalized] == [
            "application/octet-stream",
            "application/pdf",
            "application/zip",
            "image/jpeg",
            "text/plain",
        ]
        for source_part, copied in zip(roots, normalized, strict=True):
            assert copied.get_params() == source_part.get_params()
            assert copied.get_content_disposition() == source_part.get_content_disposition()

        # A corrupt base64 payload decodes to whatever the stdlib salvages, but it
        # must not blow up the forward.
        assert normalized[3].get_payload(decode=True) is not None
        assert normalized[2].get_filename() == "Sehr langer Name mit Fortsetzung.zip"

    def test_malformed_dispositions_agree_across_send_paths(self, email_client):
        """An unlabelled 8-bit filename is relabelled consistently, not differently per path."""
        source = BytesParser(policy=default).parsebytes(_build_malformed_forward_source())
        normalized = [
            normalize_forwarded_part(part)
            for part, is_attachment in email_client._iter_content_parts(source)
            if is_attachment
        ]
        message = email_client.compose_message(
            ["dest@example.com"], "Fwd: Malformed package", "note", extra_parts=normalized
        )
        dispositions = {name: _content_disposition_lines(wire) for name, wire in _serializations(message).items()}
        assert len({tuple(lines) for lines in dispositions.values()}) == 1, dispositions


class TestFormatForwardedText:
    """The quoted block reports the source headers with a neutral recipient label."""

    def test_block_shape(self):
        block = _format_forwarded_text(
            "Sender Name <sender@example.com>",
            ["rcpt@example.com", "cc@example.com"],
            "Fri, 8 May 2026 19:17:09 +0200",
            "Quarterly package",
            "Hallo Welt",
        )
        assert block == (
            "---------- Forwarded message ----------\n"
            "From: Sender Name <sender@example.com>\n"
            "Recipients: rcpt@example.com, cc@example.com\n"
            "Date: Fri, 8 May 2026 19:17:09 +0200\n"
            "Subject: Quarterly package\n"
            "\n"
            "Hallo Welt"
        )

    def test_recipients_label_is_used_because_the_list_folds_in_cc(self):
        """ "To:" would misreport a list that already merged Cc entries."""
        block = _format_forwarded_text("a@example.com", ["b@example.com"], "date", "subject", "body")
        assert "Recipients: b@example.com" in block
        assert "\nTo:" not in block

    def test_empty_recipients_render_as_an_empty_field(self):
        block = _format_forwarded_text("a@example.com", [], "date", "subject", "body")
        assert "Recipients: \n" in block


class TestFetchForwardSource:
    """Reading a forward's source is single-session, allowlisted, and never degrades."""

    @staticmethod
    def _mock_imap():
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", [b"1"]))
        mock_imap.logout = AsyncMock()
        return mock_imap

    @staticmethod
    def _fetch(raw_email: bytes):
        async def _fake_fetch(_imap, _email_id):
            return [b"1 FETCH (BODY[] {%d}" % len(raw_email), bytearray(raw_email), b")"]

        return _fake_fetch

    async def _run(self, email_client, raw_email: bytes, **kwargs):
        mock_imap = self._mock_imap()
        with (
            patch.object(email_client, "_fetch_email_with_formats", side_effect=self._fetch(raw_email)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            return await email_client.fetch_forward_source("1", **kwargs)

    @pytest.mark.asyncio
    async def test_returns_headers_body_and_parts(self, email_client):
        result = await self._run(email_client, _build_forward_source_email())

        assert set(result) == {"subject", "from", "recipients", "date", "body", "parts"}
        assert result["subject"] == "Quarterly package"
        assert result["from"] == "Sender Name <sender@example.com>"
        assert result["recipients"] == ["rcpt@example.com", "cc@example.com"]
        # The parsed Date header is re-rendered canonically ("8" -> "08") but keeps
        # the source's own offset, unlike the UTC-normalized datetime in the parse result.
        assert result["date"] == "Fri, 08 May 2026 19:17:09 +0200"
        assert result["body"].startswith("---------- Forwarded message ----------\n")
        assert "Recipients: rcpt@example.com, cc@example.com" in result["body"]
        assert result["body"].endswith("Hallo Welt")
        assert [part.get_content_type() for part in result["parts"]] == [
            "application/vnd.ms-excel",
            "application/octet-stream",
            "message/rfc822",
            "multipart/related",
            "image/png",
            "application/pdf",
        ]

    @pytest.mark.asyncio
    async def test_include_attachments_false_keeps_the_body(self, email_client):
        result = await self._run(email_client, _build_forward_source_email(), include_attachments=False)

        assert result["parts"] == []
        assert "Recipients: rcpt@example.com, cc@example.com" in result["body"]
        assert result["subject"] == "Quarterly package"

    @pytest.mark.asyncio
    async def test_long_body_is_returned_in_full_not_display_truncated(self, email_client):
        """The read path's 20k display window must never decide what a forward carries."""
        long_body = "x" * 30_000
        raw_email = (
            b"From: sender@example.com\r\n"
            b"To: rcpt@example.com\r\n"
            b"Subject: Long\r\n"
            b"Date: Fri, 8 May 2026 19:17:09 +0200\r\n"
            b"Content-Type: text/plain; charset=us-ascii\r\n"
            b"\r\n" + long_body.encode("ascii")
        )
        result = await self._run(email_client, raw_email)

        assert "...[TRUNCATED]" not in result["body"]
        assert result["body"].endswith(long_body)

    @pytest.mark.asyncio
    async def test_parser_truncation_always_leaves_an_over_limit_body(self, email_client):
        """A source body past the forward window must surface as over-limit, never shortened.

        One character encodes to at least one UTF-8 byte, so the surviving window
        still exceeds ``APPLICATION_LIMITS.body_bytes`` and application validation
        rejects the forward instead of sending silently truncated content.
        """
        raw_email = (
            b"From: sender@example.com\r\n"
            b"Subject: Big\r\n"
            b"Content-Type: text/plain; charset=us-ascii\r\n"
            b"\r\n" + b"y" * (APPLICATION_LIMITS.body_bytes + 10)
        )
        result = await self._run(email_client, raw_email)

        assert len(result["body"].encode("utf-8")) > APPLICATION_LIMITS.body_bytes

    @pytest.mark.asyncio
    async def test_missing_date_header_omits_the_date_line(self, email_client):
        """No fabricated provenance: absent source Date means no Date line at all."""
        raw_email = MIMEText("no date here", "plain", "utf-8").as_bytes()
        result = await self._run(email_client, raw_email)

        assert result["date"] == ""
        assert "Date:" not in result["body"]

    @pytest.mark.asyncio
    async def test_root_attachment_is_stripped_to_content_headers(self, email_client):
        """A single-part source whose root IS the attachment must not leak its envelope."""
        raw_email = (
            b"Received: from mx.example.com by mail.example.test; Fri, 8 May 2026 19:17:09 +0200\r\n"
            b"From: sender@example.com\r\n"
            b"To: rcpt@example.com\r\n"
            b"Bcc: hidden@example.com\r\n"
            b"Subject: Scan\r\n"
            b"Date: Fri, 8 May 2026 19:17:09 +0200\r\n"
            b"Message-ID: <root-attach@example.com>\r\n"
            b"Content-Type: application/pdf\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: attachment; filename="scan.pdf"\r\n'
            b"\r\n"
            b"c2Nhbg==\r\n"
        )
        result = await self._run(email_client, raw_email)

        assert len(result["parts"]) == 1
        part_bytes = result["parts"][0].as_bytes()
        for leaked in (b"Received:", b"From:", b"To:", b"Bcc:", b"Subject:", b"Message-ID:"):
            assert leaked not in part_bytes
        assert b'filename="scan.pdf"' in part_bytes
        assert b"c2Nhbg==" in part_bytes

    @pytest.mark.asyncio
    async def test_blocked_sender_raises_before_the_body_is_fetched(self, email_client):
        mock_imap = self._mock_imap()
        with (
            patch.object(
                email_client, "_batch_fetch_senders", AsyncMock(return_value={"1": "evil@blocked.com"})
            ) as mock_senders,
            patch.object(email_client, "_fetch_email_with_formats", AsyncMock()) as mock_fetch,
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            with pytest.raises(ValueError, match=re.escape("Failed to fetch email with UID 1")):
                await email_client.fetch_forward_source("1", allowed_senders=["*@allowed.com"])

        mock_senders.assert_awaited_once()
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_is_indistinguishable_from_missing(self, email_client):
        async def _run(senders):
            mock_imap = self._mock_imap()
            with (
                patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
                patch.object(email_client, "_fetch_email_with_formats", AsyncMock(return_value=None)),
                patch.object(email_client, "imap_class", return_value=mock_imap),
            ):
                with pytest.raises(ValueError) as exc:
                    await email_client.fetch_forward_source("1", allowed_senders=["*@allowed.com"])
            return str(exc.value)

        assert await _run({"1": "evil@blocked.com"}) == await _run({}) == "Failed to fetch email with UID 1"

    @pytest.mark.asyncio
    async def test_missing_message_raises_instead_of_returning_empty_parts(self, email_client):
        """A failed fetch must never look like "the source had no attachments"."""
        mock_imap = self._mock_imap()
        with (
            patch.object(email_client, "_fetch_email_with_formats", AsyncMock(return_value=None)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            with pytest.raises(ValueError, match=re.escape("Failed to fetch email with UID 1")):
                await email_client.fetch_forward_source("1")

    @pytest.mark.asyncio
    async def test_unreadable_literal_raises(self, email_client):
        """A FETCH whose literal cannot be located is a failure, not an empty forward."""
        mock_imap = self._mock_imap()
        with (
            patch.object(email_client, "_fetch_email_with_formats", AsyncMock(return_value=[b"1 FETCH (FLAGS ())"])),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            with pytest.raises(ValueError, match=re.escape("Could not find email data for email ID: 1")):
                await email_client.fetch_forward_source("1")

    @pytest.mark.asyncio
    async def test_imap_select_failure_raises_and_never_fetches(self, email_client):
        mock_imap = self._mock_imap()
        mock_imap.select = AsyncMock(return_value=("NO", [b"no such mailbox"]))
        with (
            patch.object(email_client, "_fetch_email_with_formats", AsyncMock()) as mock_fetch,
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            with pytest.raises(RuntimeError, match="SELECT mailbox Archive failed"):
                await email_client.fetch_forward_source("1", mailbox="Archive")

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_parse_failure_raises(self, email_client):
        mock_imap = self._mock_imap()
        raw_email = _build_forward_source_email()
        with (
            patch.object(email_client, "_fetch_email_with_formats", side_effect=self._fetch(raw_email)),
            patch.object(email_client, "_parse_email_data", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad")),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            with pytest.raises(ValueError, match="Could not parse email 1 for forwarding"):
                await email_client.fetch_forward_source("1")

    @pytest.mark.asyncio
    async def test_oversized_source_raises(self, email_client):
        mock_imap = self._mock_imap()
        raw_email = _build_forward_source_email()
        with (
            patch("mcp_email_server.emails.classic.MAX_RAW_EMAIL_BYTES", 16),
            patch.object(email_client, "_fetch_email_with_formats", side_effect=self._fetch(raw_email)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            with pytest.raises(ValueError, match="Email exceeds the raw message size limit"):
                await email_client.fetch_forward_source("1")

    @pytest.mark.asyncio
    async def test_malformed_uid_raises_before_connecting(self, email_client):
        with patch.object(email_client, "imap_class") as mock_class:
            with pytest.raises(ValueError, match="email_id must be a canonical positive decimal IMAP UID"):
                await email_client.fetch_forward_source("1:*")
        mock_class.assert_not_called()

    @pytest.mark.asyncio
    async def test_source_is_read_in_one_session(self, email_client):
        """Allowlist check and body fetch share one SELECTed session — no TOCTOU window."""
        mock_imap = self._mock_imap()
        raw_email = _build_forward_source_email()
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value={"1": "ok@allowed.com"})),
            patch.object(email_client, "_fetch_email_with_formats", side_effect=self._fetch(raw_email)) as mock_fetch,
            patch.object(email_client, "imap_class", return_value=mock_imap) as mock_class,
        ):
            await email_client.fetch_forward_source("1", allowed_senders=["*@allowed.com"])

        assert mock_class.call_count == 1
        assert mock_imap.select.await_count == 1
        assert mock_fetch.await_args.args[0] is mock_imap
        mock_imap.logout.assert_awaited_once()


class TestComposeWithExtraParts:
    """extra_parts is keyword-only and shares the container with file attachments."""

    def test_extra_parts_alone_force_a_multipart_container(self, email_client):
        _, normalized = _forward_source_parts(email_client)
        message = email_client.compose_message(["dest@example.com"], "Fwd: t", "note", extra_parts=normalized[:1])

        assert message.is_multipart()
        payload = message.get_payload()
        assert [item.get_content_type() for item in payload] == ["text/plain", "application/vnd.ms-excel"]

    def test_extra_parts_follow_file_attachments(self, email_client, tmp_path):
        upload = tmp_path / "note.txt"
        upload.write_text("local file")
        _, normalized = _forward_source_parts(email_client)
        message = email_client.compose_message(
            ["dest@example.com"], "Fwd: t", "note", attachments=[str(upload)], extra_parts=normalized[:1]
        )

        payload = message.get_payload()
        assert [item.get_filename() for item in payload[1:]] == ["note.txt", _FORWARD_LONG_FILENAME]

    def test_no_extra_parts_leaves_a_simple_message(self, email_client):
        message = email_client.compose_message(["dest@example.com"], "Fwd: t", "note", extra_parts=[])
        assert not message.is_multipart()


class TestForwardEightBitTransport:
    """A forwarded part is the only way raw 8-bit octets can reach compose output.

    Transport classification itself is shared ``send_email_with_outcome`` behavior;
    these tests pin how re-attached 8-bit parts interact with it: the composed
    container is labeled ``8bit`` so a correctly labeled source part rides
    ``BODY=8BITMIME`` instead of being refused as a mislabeled composite.
    """

    @staticmethod
    def _smtp(*, extensions: tuple[str, ...] = ()):
        smtp = AsyncMock()
        smtp.__aenter__.return_value = smtp
        smtp.__aexit__.return_value = False
        smtp.login.return_value = None
        smtp.supports_extension = MagicMock(side_effect=lambda name: name.lower() in extensions)
        return smtp

    @staticmethod
    def _eight_bit_part():
        raw = (
            b"MIME-Version: 1.0\r\n"
            b"From: sender@example.com\r\n"
            b"To: rcpt@example.com\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n'
            b"\r\n"
            b"--B\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n"
            b'Content-Disposition: attachment; filename="note.txt"\r\n'
            b"\r\n"
            b"Gr\xc3\xbc\xc3\x9fe aus M\xc3\xbcnchen\r\n"
            b"--B--\r\n"
        )
        source = BytesParser(policy=default).parsebytes(raw)
        return normalize_forwarded_part(next(iter(source.iter_parts())))

    @pytest.mark.asyncio
    async def test_raw_8bit_part_is_refused_without_8bitmime(self, email_client):
        """Rejected before MAIL, so no 8-bit octets are offered on a 7-bit channel."""
        smtp = self._smtp()
        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
            outcome = await email_client.send_email_with_outcome(
                ["dest@example.com"], "Fwd: t", "note", extra_parts=[self._eight_bit_part()]
            )

        assert [(item.status, item.detail) for item in outcome.outcomes] == [("failed", "smtp-8bitmime-required")]
        assert outcome.sent_message is None
        smtp.mail.assert_not_called()
        smtp.data.assert_not_called()

    @pytest.mark.asyncio
    async def test_raw_8bit_part_is_delivered_when_8bitmime_is_advertised(self, email_client):
        smtp = self._smtp(extensions=("8bitmime",))
        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
            outcome = await email_client.send_email_with_outcome(
                ["dest@example.com"], "Fwd: t", "note", extra_parts=[self._eight_bit_part()]
            )

        assert [item.status for item in outcome.outcomes] == ["succeeded"]
        assert "BODY=8BITMIME" in smtp.mail.await_args.kwargs["options"]
        assert b"Gr\xc3\xbc\xc3\x9fe aus M\xc3\xbcnchen" in smtp.data.await_args.args[0]

    @pytest.mark.asyncio
    async def test_seven_bit_clean_forward_is_delivered_without_8bitmime(self, email_client):
        """The six-part fixture is entirely base64/7bit, so no 8bit label or option appears."""
        _, normalized = _forward_source_parts(email_client)
        smtp = self._smtp()
        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
            outcome = await email_client.send_email_with_outcome(
                ["dest@example.com"], "Fwd: Quarterly package", "note", extra_parts=normalized
            )

        assert [item.status for item in outcome.outcomes] == ["succeeded"]
        assert smtp.data.await_args.args[0].isascii()

    @pytest.mark.asyncio
    async def test_ordinary_send_is_unaffected_by_forward_labeling(self, email_client):
        """Without extra_parts everything composed is base64/QP, so nothing regresses."""
        smtp = self._smtp()
        with patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp):
            outcome = await email_client.send_email_with_outcome(["dest@example.com"], "Grüße", "Grüße aus München")

        assert [item.status for item in outcome.outcomes] == ["succeeded"]
        smtp.data.assert_awaited_once()
