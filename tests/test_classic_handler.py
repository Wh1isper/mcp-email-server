from datetime import datetime, timezone
from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_init_read_only_account(self):
        """Read-only accounts initialize without an outgoing SMTP client."""
        email_settings = EmailSettings(
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

        handler = ClassicEmailHandler(email_settings)

        assert isinstance(handler.incoming_client, EmailClient)
        assert handler.outgoing_client is None

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

        # Mock get_emails_metadata to return (total, email_dicts)
        mock_get_metadata = AsyncMock(return_value=(1, [email_data]))

        # Apply the mock
        with patch("mcp_email_server.emails.classic.get_settings", return_value=MagicMock(allowed_senders=[])):
            with patch.object(classic_handler.incoming_client, "get_emails_metadata", mock_get_metadata):
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

                # Verify the client method was called correctly
                mock_get_metadata.assert_called_once_with(
                    1,
                    10,
                    now,
                    None,
                    "Test",
                    "sender@example.com",
                    None,
                    "desc",
                    "INBOX",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    allowed_senders=[],
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

        mock_get_metadata = AsyncMock(return_value=(1, [email_data]))

        with patch("mcp_email_server.emails.classic.get_settings", return_value=MagicMock(allowed_senders=[])):
            with patch.object(classic_handler.incoming_client, "get_emails_metadata", mock_get_metadata):
                result = await classic_handler.get_emails_metadata(
                    page=1,
                    page_size=10,
                    mailbox="Sent",
                )

                assert isinstance(result, EmailMetadataPageResponse)
                assert len(result.emails) == 1

                # Verify mailbox parameter was passed correctly
                mock_get_metadata.assert_called_once_with(
                    1,
                    10,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "desc",
                    "Sent",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    allowed_senders=[],
                )

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
                None,
            )

    @pytest.mark.asyncio
    async def test_read_only_account_rejects_send_email(self):
        """Read-only accounts cannot send email."""
        email_settings = EmailSettings(
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
        handler = ClassicEmailHandler(email_settings)

        with pytest.raises(RuntimeError, match="SMTP is not configured"):
            await handler.send_email(
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
            )

    @pytest.mark.asyncio
    async def test_read_only_account_rejects_save_to_mailbox(self):
        """Read-only accounts cannot compose and save outbound drafts."""
        email_settings = EmailSettings(
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
        handler = ClassicEmailHandler(email_settings)

        with pytest.raises(RuntimeError, match="SMTP is not configured"):
            await handler.save_to_mailbox(
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
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
            mock_delete.assert_called_once_with(
                ["123", "456"], "INBOX", allowed_senders=[], report_blocked_mutations=False
            )

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
            mock_delete.assert_called_once_with(
                ["123", "456"], "Trash", allowed_senders=[], report_blocked_mutations=False
            )

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
            mock_delete.assert_called_once_with(["789"], "Archive", allowed_senders=[], report_blocked_mutations=False)

    @pytest.mark.asyncio
    async def test_mark_emails_as_read(self, classic_handler):
        """Test mark_emails_as_read method."""
        mock_mark = AsyncMock(return_value=(["123", "456"], []))

        with patch.object(classic_handler.incoming_client, "mark_emails_as_read", mock_mark):
            marked_ids, failed_ids = await classic_handler.mark_emails_as_read(
                email_ids=["123", "456"],
                mailbox="INBOX",
            )

            assert marked_ids == ["123", "456"]
            assert failed_ids == []
            mock_mark.assert_called_once_with(
                ["123", "456"], "INBOX", allowed_senders=[], report_blocked_mutations=False
            )

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_with_failures(self, classic_handler):
        """Test mark_emails_as_read method with some failures."""
        mock_mark = AsyncMock(return_value=(["123"], ["456"]))

        with patch.object(classic_handler.incoming_client, "mark_emails_as_read", mock_mark):
            marked_ids, failed_ids = await classic_handler.mark_emails_as_read(
                email_ids=["123", "456"],
                mailbox="INBOX",
            )

            assert marked_ids == ["123"]
            assert failed_ids == ["456"]
            mock_mark.assert_called_once_with(
                ["123", "456"], "INBOX", allowed_senders=[], report_blocked_mutations=False
            )

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_custom_mailbox(self, classic_handler):
        """Test mark_emails_as_read method with custom mailbox."""
        mock_mark = AsyncMock(return_value=(["789"], []))

        with patch.object(classic_handler.incoming_client, "mark_emails_as_read", mock_mark):
            marked_ids, failed_ids = await classic_handler.mark_emails_as_read(
                email_ids=["789"],
                mailbox="Archive",
            )

            assert marked_ids == ["789"]
            assert failed_ids == []
            mock_mark.assert_called_once_with(["789"], "Archive", allowed_senders=[], report_blocked_mutations=False)

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

            mock_download.assert_called_once_with("123", "document.pdf", save_path, "INBOX", allowed_senders=[])

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
    async def test_send_email_with_reply_to(self, classic_handler):
        """Test sending email with Reply-To header."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await classic_handler.send_email(
                recipients=["recipient@example.com"],
                subject="Test",
                body="Body",
                reply_to="replyhere@example.com",
            )

            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert msg["Reply-To"] == "replyhere@example.com"

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
            mock_get_body.assert_called_once_with(
                "123", "INBOX", False, allowed_senders=[], body_offset=0, max_body_length=20000
            )

    @pytest.mark.asyncio
    async def test_get_emails_content_mark_as_read_true(self, classic_handler):
        """Test that get_emails_content passes mark_as_read=True to the underlying client."""
        now = datetime.now(timezone.utc)
        email_data = {
            "email_id": "123",
            "message_id": "<test@example.com>",
            "subject": "Test Subject",
            "from": "sender@example.com",
            "to": ["recipient@example.com"],
            "date": now,
            "body": "Test body",
            "attachments": [],
        }

        mock_get_body = AsyncMock(return_value=email_data)

        with patch.object(classic_handler.incoming_client, "get_email_body_by_id", mock_get_body):
            result = await classic_handler.get_emails_content(
                email_ids=["123"],
                mailbox="INBOX",
                mark_as_read=True,
            )

            assert len(result.emails) == 1
            mock_get_body.assert_called_once_with(
                "123", "INBOX", True, allowed_senders=[], body_offset=0, max_body_length=20000
            )

    @pytest.mark.asyncio
    async def test_get_emails_content_mark_as_read_default_false(self, classic_handler):
        """Test that get_emails_content defaults mark_as_read to False."""
        now = datetime.now(timezone.utc)
        email_data = {
            "email_id": "456",
            "message_id": None,
            "subject": "No Mark",
            "from": "a@example.com",
            "to": ["b@example.com"],
            "date": now,
            "body": "body",
            "attachments": [],
        }

        mock_get_body = AsyncMock(return_value=email_data)

        with patch.object(classic_handler.incoming_client, "get_email_body_by_id", mock_get_body):
            await classic_handler.get_emails_content(email_ids=["456"])

            mock_get_body.assert_called_once_with(
                "456", "INBOX", False, allowed_senders=[], body_offset=0, max_body_length=20000
            )

    @pytest.mark.asyncio
    async def test_get_emails_metadata_passes_allowlist_to_client(self, classic_handler):
        mock_get = AsyncMock(return_value=(0, []))
        with patch(
            "mcp_email_server.emails.classic.get_settings", return_value=MagicMock(allowed_senders=["*@example.com"])
        ):
            with patch.object(classic_handler.incoming_client, "get_emails_metadata", mock_get):
                await classic_handler.get_emails_metadata(page=1, page_size=10)
        assert mock_get.call_args.kwargs.get("allowed_senders") == ["*@example.com"]

    @pytest.mark.asyncio
    async def test_download_attachment_passes_allowlist_to_client(self, classic_handler):
        mock_dl = AsyncMock(
            return_value={
                "email_id": "1",
                "attachment_name": "a.png",
                "mime_type": "image/png",
                "size": 3,
                "saved_path": "/tmp/a.png",  # noqa: S108
            }
        )
        with patch(
            "mcp_email_server.emails.classic.get_settings",
            return_value=MagicMock(allowed_senders=["*@example.com"]),
        ):
            with patch.object(classic_handler.incoming_client, "download_attachment", mock_dl):
                await classic_handler.download_attachment("1", "a.png", "/tmp/a.png")  # noqa: S108
        assert mock_dl.call_args.kwargs.get("allowed_senders") == ["*@example.com"]

    @pytest.mark.asyncio
    async def test_get_emails_content_passes_allowed_senders_to_client(self, classic_handler):
        """The handler forwards the configured allowlist to the read path, which enforces it."""
        now = datetime.now(timezone.utc)
        email_data = {
            "email_id": "1",
            "message_id": None,
            "subject": "a",
            "from": "alice@example.com",
            "to": [],
            "date": now,
            "body": "x",
            "attachments": [],
        }
        mock_get = AsyncMock(return_value=email_data)
        with patch(
            "mcp_email_server.emails.classic.get_settings",
            return_value=MagicMock(allowed_senders=["*@example.com"]),
        ):
            with patch.object(classic_handler.incoming_client, "get_email_body_by_id", mock_get):
                await classic_handler.get_emails_content(["1"])
        assert mock_get.call_args.kwargs.get("allowed_senders") == ["*@example.com"]

    @pytest.mark.asyncio
    async def test_get_emails_content_no_allowlist_passes_mark_through(self, classic_handler):
        now = datetime.now(timezone.utc)
        alice = {
            "email_id": "1",
            "message_id": None,
            "subject": "a",
            "from": "alice@example.com",
            "to": [],
            "date": now,
            "body": "x",
            "attachments": [],
        }
        mock_get = AsyncMock(return_value=alice)
        mock_mark = AsyncMock()
        with patch("mcp_email_server.emails.classic.get_settings", return_value=MagicMock(allowed_senders=[])):
            with patch.object(classic_handler.incoming_client, "get_email_body_by_id", mock_get):
                with patch.object(classic_handler.incoming_client, "mark_emails_as_read", mock_mark):
                    result = await classic_handler.get_emails_content(["1"], mark_as_read=True)
        assert result.retrieved_count == 1
        assert mock_get.call_args.args[2] is True  # mark passed through to fetch
        mock_mark.assert_not_called()  # no deferred batch-mark when not filtering

    @pytest.mark.asyncio
    async def test_delete_emails_passes_allowlist_and_report_flag(self, classic_handler):
        mock_delete = AsyncMock(return_value=(["1"], []))
        with patch(
            "mcp_email_server.emails.classic.get_settings",
            return_value=MagicMock(allowed_senders=["*@example.com"], report_blocked_mutations=True),
        ):
            with patch.object(classic_handler.incoming_client, "delete_emails", mock_delete):
                await classic_handler.delete_emails(["1"], "INBOX")
        assert mock_delete.call_args.kwargs.get("allowed_senders") == ["*@example.com"]
        assert mock_delete.call_args.kwargs.get("report_blocked_mutations") is True

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_passes_allowlist_and_report_flag(self, classic_handler):
        mock_mark = AsyncMock(return_value=(["1"], []))
        with patch(
            "mcp_email_server.emails.classic.get_settings",
            return_value=MagicMock(allowed_senders=["*@example.com"], report_blocked_mutations=False),
        ):
            with patch.object(classic_handler.incoming_client, "mark_emails_as_read", mock_mark):
                await classic_handler.mark_emails_as_read(["1"], "INBOX")
        assert mock_mark.call_args.kwargs.get("allowed_senders") == ["*@example.com"]
        assert mock_mark.call_args.kwargs.get("report_blocked_mutations") is False

    @pytest.mark.asyncio
    async def test_move_emails_passes_allowlist_and_report_flag(self, classic_handler):
        mock_move = AsyncMock(return_value=(["1"], []))
        with patch(
            "mcp_email_server.emails.classic.get_settings",
            return_value=MagicMock(allowed_senders=["*@example.com"], report_blocked_mutations=True),
        ):
            with patch.object(classic_handler.incoming_client, "move_emails", mock_move):
                await classic_handler.move_emails(["1"], "INBOX", "Archive")
        assert mock_move.call_args.kwargs.get("allowed_senders") == ["*@example.com"]
        assert mock_move.call_args.kwargs.get("report_blocked_mutations") is True

    @pytest.mark.asyncio
    async def test_archive_emails_passes_allowlist_and_report_flag(self, classic_handler):
        mock_move = AsyncMock(return_value=(["1"], []))
        with patch(
            "mcp_email_server.emails.classic.get_settings",
            return_value=MagicMock(allowed_senders=["*@example.com"], report_blocked_mutations=True),
        ):
            with patch.object(classic_handler, "_find_archive_folder", AsyncMock(return_value="Archive")):
                with patch.object(classic_handler.incoming_client, "move_emails", mock_move):
                    await classic_handler.archive_emails(["1"], "INBOX")
        assert mock_move.call_args.kwargs.get("allowed_senders") == ["*@example.com"]
        assert mock_move.call_args.kwargs.get("report_blocked_mutations") is True


class TestEmailClientGetEmailBodyById:
    """Test EmailClient.get_email_body_by_id read-state behavior."""

    @pytest.fixture
    def email_client(self, email_settings):
        return EmailClient(email_settings.incoming)

    @staticmethod
    def _raw_email() -> bytes:
        msg = EmailMessage()
        msg["Subject"] = "Test Subject"
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Date"] = "Tue, 26 May 2026 04:30:00 +0000"
        msg["Message-ID"] = "<test@example.com>"
        msg.set_content("Test body")
        return msg.as_bytes()

    @pytest.mark.asyncio
    async def test_get_email_body_by_id_uses_peek_fetch_by_default(self, email_client, mock_imap):
        """Test default retrieval uses non-mutating PEEK fetch and does not STORE \\Seen."""
        mock_imap.uid = AsyncMock(return_value=("OK", [b"FETCH BODY[]", bytearray(self._raw_email())]))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            result = await email_client.get_email_body_by_id("123")

        assert result is not None
        assert result["email_id"] == "123"
        mock_imap.uid.assert_called_once_with("fetch", "123", "BODY.PEEK[]")

    @pytest.mark.asyncio
    async def test_get_email_body_by_id_marks_as_read_after_successful_parse(self, email_client, mock_imap):
        """Test mark_as_read=True stores \\Seen after a successful parse."""
        mock_imap.uid = AsyncMock(
            side_effect=[
                ("OK", [b"FETCH BODY[]", bytearray(self._raw_email())]),
                ("OK", [b"STORE +FLAGS (\\Seen)"]),
            ]
        )

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            result = await email_client.get_email_body_by_id("123", mark_as_read=True)

        assert result is not None
        assert mock_imap.uid.call_args_list[0].args == ("fetch", "123", "BODY.PEEK[]")
        assert mock_imap.uid.call_args_list[1].args == ("store", "123", "+FLAGS", r"(\Seen)")

    @pytest.mark.asyncio
    async def test_get_email_body_by_id_does_not_mark_as_read_when_parse_fails(self, email_client, mock_imap):
        """Test failed parsing skips the \\Seen STORE side effect."""
        mock_imap.uid = AsyncMock(return_value=("OK", [b"FETCH BODY[]", bytearray(self._raw_email())]))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            with patch.object(email_client, "_parse_email_data", side_effect=ValueError("parse failed")):
                result = await email_client.get_email_body_by_id("123", mark_as_read=True)

        assert result is None
        mock_imap.uid.assert_called_once_with("fetch", "123", "BODY.PEEK[]")

    @pytest.mark.asyncio
    async def test_get_email_body_by_id_continues_when_mark_as_read_store_fails(self, email_client, mock_imap):
        """Test STORE failure is logged while retrieval still succeeds."""
        mock_imap.uid = AsyncMock(
            side_effect=[
                ("OK", [b"FETCH BODY[]", bytearray(self._raw_email())]),
                ("NO", [b"STORE failed"]),
            ]
        )

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            result = await email_client.get_email_body_by_id("123", mark_as_read=True)

        assert result is not None
        assert result["email_id"] == "123"
        assert mock_imap.uid.call_count == 2

    @pytest.mark.asyncio
    async def test_get_email_body_by_id_blocked_sender_returns_none_without_reading_body(self, email_client, mock_imap):
        """A blocked sender returns None and the body is never fetched (From is checked first)."""
        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_senders", return_value={"123": "bob@evil.com"}):
                with patch.object(email_client, "_fetch_email_with_formats", new=AsyncMock()) as mock_body:
                    result = await email_client.get_email_body_by_id("123", allowed_senders=["alice@example.com"])

        assert result is None
        mock_body.assert_not_called()  # blocked body is never read or parsed

    @pytest.mark.asyncio
    async def test_get_email_body_by_id_allowed_sender_reads_body(self, email_client, mock_imap):
        """An allowed sender passes the From check and the body is fetched."""
        mock_imap.uid = AsyncMock(return_value=("OK", [b"FETCH BODY[]", bytearray(self._raw_email())]))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_senders", return_value={"123": "sender@example.com"}):
                result = await email_client.get_email_body_by_id("123", allowed_senders=["*@example.com"])

        assert result is not None
        assert result["email_id"] == "123"

    @pytest.mark.asyncio
    async def test_get_email_body_by_id_no_allowlist_skips_sender_check(self, email_client, mock_imap):
        """With no allowlist configured, the From header is not pre-fetched (zero overhead)."""
        mock_imap.uid = AsyncMock(return_value=("OK", [b"FETCH BODY[]", bytearray(self._raw_email())]))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_senders") as mock_senders:
                result = await email_client.get_email_body_by_id("123")

        assert result is not None
        mock_senders.assert_not_called()


class TestEmailClientMarkAsRead:
    """Test EmailClient.mark_emails_as_read with mock IMAP."""

    @pytest.fixture
    def email_client(self, email_settings):
        return EmailClient(email_settings.incoming)

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_success(self, email_client, mock_imap):
        """Test marking emails as read sets \\Seen flag via IMAP STORE."""
        mock_imap.uid = AsyncMock(return_value=("OK", [b"1 STORE +FLAGS (\\Seen)"]))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            marked_ids, failed_ids = await email_client.mark_emails_as_read(["123", "456"])

        assert marked_ids == ["123", "456"]
        assert failed_ids == []
        assert mock_imap.uid.call_count == 2
        mock_imap.uid.assert_any_call("store", "123", "+FLAGS", r"(\Seen)")
        mock_imap.uid.assert_any_call("store", "456", "+FLAGS", r"(\Seen)")
        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_partial_failure(self, email_client, mock_imap):
        """Test marking emails handles partial failures gracefully."""
        mock_imap.uid = AsyncMock(side_effect=[("OK", []), Exception("IMAP error")])

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            marked_ids, failed_ids = await email_client.mark_emails_as_read(["123", "456"])

        assert marked_ids == ["123"]
        assert failed_ids == ["456"]

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_treats_no_response_as_failure(self, email_client, mock_imap):
        """Test IMAP NO STORE responses are reported as failed ids."""
        mock_imap.uid = AsyncMock(return_value=("NO", [b"STORE failed"]))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            marked_ids, failed_ids = await email_client.mark_emails_as_read(["123"])

        assert marked_ids == []
        assert failed_ids == ["123"]

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_raises_on_select_failure(self, email_client, mock_imap):
        """Test mailbox selection failures are surfaced."""
        mock_imap.select = AsyncMock(return_value=("NO", [b"unknown mailbox"]))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            with pytest.raises(RuntimeError, match="SELECT mailbox Archive failed"):
                await email_client.mark_emails_as_read(["123"], mailbox="Archive")

        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_custom_mailbox(self, email_client, mock_imap):
        """Test marking emails in a custom mailbox."""
        mock_imap.uid = AsyncMock(return_value=("OK", []))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            await email_client.mark_emails_as_read(["123"], mailbox="Archive")

        mock_imap.select.assert_called_once()
        # Verify the mailbox was quoted and passed to select
        select_arg = mock_imap.select.call_args[0][0]
        assert "Archive" in select_arg

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_logout_on_error(self, email_client, mock_imap):
        """Test that IMAP logout is called even when errors occur."""
        mock_imap.login = AsyncMock(side_effect=Exception("auth failed"))

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            with pytest.raises(Exception, match="auth failed"):
                await email_client.mark_emails_as_read(["123"])

        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_empty_list(self, email_client, mock_imap):
        """Test marking empty list returns empty results."""
        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            marked_ids, failed_ids = await email_client.mark_emails_as_read([])

        assert marked_ids == []
        assert failed_ids == []


class TestEmailClientBatchMethods:
    """Test batch fetch methods for performance optimization."""

    @pytest.fixture
    def email_client(self, email_settings):
        return EmailClient(email_settings.incoming)

    def test_parse_headers(self, email_client):
        """Test _parse_headers method parses email headers correctly."""
        raw_headers = b"""From: sender@example.com
To: recipient@example.com
Cc: cc@example.com
Subject: Test Subject
Date: Mon, 20 Jan 2025 10:30:00 +0000

"""
        result = email_client._parse_headers("123", raw_headers)

        assert result is not None
        assert result["email_id"] == "123"
        assert result["subject"] == "Test Subject"
        assert result["from"] == "sender@example.com"
        assert "recipient@example.com" in result["to"]
        assert "cc@example.com" in result["to"]
        assert result["attachments"] == []

    def test_parse_headers_with_invalid_data(self, email_client):
        """Test _parse_headers handles malformed headers gracefully."""
        # Completely broken data that can't be parsed
        raw_headers = b"\xff\xfe\x00\x00"
        result = email_client._parse_headers("123", raw_headers)

        # Should return None or a valid dict with fallback values
        # The implementation catches exceptions and returns None
        assert result is None or isinstance(result, dict)

    def test_parse_headers_missing_date(self, email_client):
        """Test _parse_headers handles missing date with fallback."""
        raw_headers = b"""From: sender@example.com
To: recipient@example.com
Subject: No Date Email

"""
        result = email_client._parse_headers("123", raw_headers)

        assert result is not None
        assert result["email_id"] == "123"
        assert result["date"] is not None  # Should have fallback to now()

    @pytest.mark.asyncio
    async def test_batch_fetch_dates_empty_list(self, email_client):
        """Test _batch_fetch_dates with empty list returns empty dict."""
        mock_imap = AsyncMock()
        result = await email_client._batch_fetch_dates(mock_imap, [])

        assert result == {}
        mock_imap.uid.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_fetch_headers_empty_list(self, email_client):
        """Test _batch_fetch_headers with empty list returns empty dict."""
        mock_imap = AsyncMock()
        result = await email_client._batch_fetch_headers(mock_imap, [])

        assert result == {}
        mock_imap.uid.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_fetch_dates_parses_response(self, email_client):
        """Test _batch_fetch_dates correctly parses IMAP INTERNALDATE response."""
        mock_imap = AsyncMock()
        # Simulate IMAP response format for INTERNALDATE
        mock_imap.uid.return_value = (
            "OK",
            [
                b'1 FETCH (UID 100 INTERNALDATE "20-Jan-2025 10:30:00 +0000")',
                b'2 FETCH (UID 101 INTERNALDATE "21-Jan-2025 11:00:00 +0000")',
            ],
        )

        result = await email_client._batch_fetch_dates(mock_imap, [b"100", b"101"])

        assert "100" in result
        assert "101" in result
        assert result["100"].day == 20
        assert result["101"].day == 21

    @pytest.mark.asyncio
    async def test_batch_fetch_headers_parses_response(self, email_client):
        """Test _batch_fetch_headers correctly parses IMAP BODY[HEADER] response."""
        mock_imap = AsyncMock()
        # Simulate IMAP response format for BODY[HEADER]
        mock_imap.uid.return_value = (
            "OK",
            [
                b"1 FETCH (UID 100 BODY[HEADER] {100}",
                bytearray(b"From: sender@example.com\r\nTo: recipient@example.com\r\nSubject: Test\r\n\r\n"),
                b")",
            ],
        )

        result = await email_client._batch_fetch_headers(mock_imap, ["100"])

        assert "100" in result
        assert result["100"]["subject"] == "Test"
        assert result["100"]["from"] == "sender@example.com"


class TestGetEmailsContentEdgeCases:
    @pytest.mark.asyncio
    async def test_none_email_data_goes_to_failed_ids(self, classic_handler):
        """A fetch that returns None is reported as a genuine failure."""
        mock_get = AsyncMock(return_value=None)
        with patch("mcp_email_server.emails.classic.get_settings", return_value=MagicMock(allowed_senders=[])):
            with patch.object(classic_handler.incoming_client, "get_email_body_by_id", mock_get):
                result = await classic_handler.get_emails_content(["1"])
        assert result.emails == []
        assert result.failed_ids == ["1"]
        assert result.retrieved_count == 0

    @pytest.mark.asyncio
    async def test_fetch_exception_goes_to_failed_ids(self, classic_handler):
        """An exception during fetch is caught and reported as a failure, not raised."""
        mock_get = AsyncMock(side_effect=Exception("boom"))
        with patch("mcp_email_server.emails.classic.get_settings", return_value=MagicMock(allowed_senders=[])):
            with patch.object(classic_handler.incoming_client, "get_email_body_by_id", mock_get):
                result = await classic_handler.get_emails_content(["1"])
        assert result.failed_ids == ["1"]
        assert result.retrieved_count == 0

    @pytest.mark.asyncio
    async def test_get_emails_content_passes_mark_as_read(self, classic_handler):
        """mark_as_read is forwarded to the read path (which marks only allowed messages)."""
        now = datetime.now(timezone.utc)
        email_data = {
            "email_id": "1",
            "message_id": None,
            "subject": "a",
            "from": "alice@example.com",
            "to": [],
            "date": now,
            "body": "x",
            "attachments": [],
        }
        mock_get = AsyncMock(return_value=email_data)
        with patch("mcp_email_server.emails.classic.get_settings", return_value=MagicMock(allowed_senders=[])):
            with patch.object(classic_handler.incoming_client, "get_email_body_by_id", mock_get):
                result = await classic_handler.get_emails_content(["1"], mark_as_read=True)
        assert result.retrieved_count == 1
        assert mock_get.call_args.args == ("1", "INBOX", True)
