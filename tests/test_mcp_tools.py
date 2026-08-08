import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

from mcp_email_server import app as app_module
from mcp_email_server.app import (
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
    set_email_flags,
)
from mcp_email_server.application.accounts import AvailableAccount, EffectiveConfiguration
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    ArchiveMutationOutcome,
    BatchMutationOutcome,
    RecipientPolicyDeniedError,
    SendMutationOutcome,
    SentCopyMutationOutcome,
    SetEmailFlagsCommand,
    TargetMutationOutcome,
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


def _batch_outcome(
    succeeded: tuple[str, ...] = (),
    failed: tuple[str, ...] = (),
    unknown: tuple[str, ...] = (),
) -> BatchMutationOutcome:
    return BatchMutationOutcome((
        *(TargetMutationOutcome(target, "succeeded") for target in succeeded),
        *(TargetMutationOutcome(target, "failed") for target in failed),
        *(TargetMutationOutcome(target, "unknown") for target in unknown),
    ))


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

        with patch(
            "mcp_email_server.app.list_effective_accounts",
            return_value=[
                AvailableAccount(
                    account_name=email_settings.account_name,
                    account_type="email",
                    description=email_settings.description,
                    email_address=email_settings.email_address,
                    can_receive=True,
                    can_send=True,
                ),
                AvailableAccount(
                    account_name=provider_settings.account_name,
                    account_type="provider",
                    description=provider_settings.description,
                    can_receive=False,
                    can_send=False,
                ),
            ],
        ) as list_accounts:
            result = await list_available_accounts()

        assert [account.model_dump(mode="json") for account in result] == [
            {
                "account_name": "test_email",
                "account_type": "email",
                "description": "",
                "email_address": "test@example.com",
                "can_receive": True,
                "can_send": True,
            },
            {
                "account_name": "test_provider",
                "account_type": "provider",
                "description": "",
                "email_address": None,
                "can_receive": False,
                "can_send": False,
            },
        ]
        list_accounts.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_list_emails_metadata(self):
        """Test list_emails_metadata MCP tool."""
        # Create test data
        now = datetime.now(UTC)
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

        mock_query = AsyncMock(return_value=email_metadata_page)

        with patch("mcp_email_server.app.list_email_metadata", mock_query):
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

            mock_query.assert_awaited_once()
            query = mock_query.await_args.args[0]
            assert query.account_name == "test_account"
            assert query.page == 1
            assert query.page_size == 10
            assert query.before == now
            assert query.subject == "Test"
            assert query.from_address == "sender@example.com"
            assert query.mailbox == "INBOX"

    @pytest.mark.asyncio
    async def test_list_emails_metadata_with_mailbox(self):
        """Test list_emails_metadata MCP tool with custom mailbox."""
        now = datetime.now(UTC)
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

        mock_query = AsyncMock(return_value=email_metadata_page)

        with patch("mcp_email_server.app.list_email_metadata", mock_query):
            result = await list_emails_metadata(
                account_name="test_account",
                mailbox="Sent",
            )

            assert result == email_metadata_page
            mock_query.assert_awaited_once()
            assert mock_query.await_args.args[0].mailbox == "Sent"

    @pytest.mark.asyncio
    async def test_get_emails_content_single(self):
        """Test get_emails_content MCP tool with single email."""
        # Create test data
        now = datetime.now(UTC)
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

        query_handler = AsyncMock(return_value=batch_response)

        with patch("mcp_email_server.app.get_email_content_query", query_handler):
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

            query_handler.assert_awaited_once()
            query = query_handler.await_args.args[0]
            assert query.email_ids == ("12345",)
            assert query.mailbox == "INBOX"
            assert query.mark_as_read is False

    @pytest.mark.asyncio
    async def test_get_emails_content_batch(self):
        """Test get_emails_content MCP tool with multiple emails."""
        # Create test data
        now = datetime.now(UTC)
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

        query_handler = AsyncMock(return_value=batch_response)

        with patch("mcp_email_server.app.get_email_content_query", query_handler):
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

            query = query_handler.await_args.args[0]
            assert query.email_ids == ("12345", "12346", "12347")
            assert query.mailbox == "INBOX"

    @pytest.mark.asyncio
    async def test_get_emails_content_with_mailbox(self):
        """Test get_emails_content MCP tool with custom mailbox."""
        now = datetime.now(UTC)
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

        query_handler = AsyncMock(return_value=batch_response)

        with patch("mcp_email_server.app.get_email_content_query", query_handler):
            result = await get_emails_content(
                account_name="test_account",
                email_ids=["12345"],
                mailbox="Sent",
            )

            assert result == batch_response
            assert query_handler.await_args.args[0].mailbox == "Sent"

    @pytest.mark.asyncio
    async def test_complete_mcp_catalog_matches_exact_fixture(self):
        """Names, descriptions, schemas, annotations, prompts, and resources stay explicit."""
        actual = {
            "tools": [tool.model_dump(mode="json", exclude_none=True) for tool in await app_module.mcp.list_tools()],
            "resources": [
                resource.model_dump(mode="json", exclude_none=True)
                for resource in await app_module.mcp.list_resources()
            ],
            "resource_templates": [
                template.model_dump(mode="json", exclude_none=True)
                for template in await app_module.mcp.list_resource_templates()
            ],
            "prompts": [
                prompt.model_dump(mode="json", exclude_none=True) for prompt in await app_module.mcp.list_prompts()
            ],
        }
        expected = json.loads((Path(__file__).parent / "fixtures" / "mcp_catalog.json").read_text(encoding="utf-8"))

        assert actual == expected
        assert "add_email_account" not in {tool["name"] for tool in actual["tools"]}

    @pytest.mark.asyncio
    async def test_mcp_schemas_advertise_central_string_and_collection_limits(self):
        tools = {tool.name: tool.inputSchema["properties"] for tool in await app_module.mcp.list_tools()}
        for properties in tools.values():
            account_name = properties.get("account_name")
            if account_name is not None:
                assert account_name["maxLength"] == APPLICATION_LIMITS.account_name_bytes

        content_ids = tools["get_emails_content"]["email_ids"]
        assert content_ids["maxItems"] == APPLICATION_LIMITS.content_email_ids
        assert content_ids["items"]["maxLength"] == len(str(APPLICATION_LIMITS.maximum_imap_uid))
        for tool_name in (
            "delete_emails",
            "set_email_flags",
            "mark_emails_as_read",
            "move_emails",
            "archive_emails",
        ):
            ids = tools[tool_name]["email_ids"]
            assert ids["maxItems"] == APPLICATION_LIMITS.mutation_uids
            assert ids["items"]["maxLength"] == len(str(APPLICATION_LIMITS.maximum_imap_uid))

        send = tools["send_email"]
        assert send["recipients"]["maxItems"] == APPLICATION_LIMITS.recipients
        assert send["recipients"]["items"]["maxLength"] == APPLICATION_LIMITS.address_bytes
        assert send["attachments"]["anyOf"][0]["maxItems"] == APPLICATION_LIMITS.attachments
        assert send["attachments"]["anyOf"][0]["items"]["maxLength"] == APPLICATION_LIMITS.attachment_path_bytes
        assert send["subject"]["maxLength"] == APPLICATION_LIMITS.subject_bytes
        assert send["body"]["maxLength"] == APPLICATION_LIMITS.body_bytes
        flags = tools["save_to_mailbox"]["flags"]["anyOf"][0]
        assert flags["maxItems"] == APPLICATION_LIMITS.flags
        assert flags["items"]["maxLength"] == APPLICATION_LIMITS.flag_bytes
        mutable_flags = tools["set_email_flags"]["flags"]
        assert mutable_flags["minItems"] == 1
        assert mutable_flags["maxItems"] == 4
        assert mutable_flags["items"]["enum"] == [r"\Seen", r"\Flagged", r"\Answered", r"\Draft"]
        assert tools["set_email_flags"]["operation"]["enum"] == ["add", "remove"]

    @pytest.mark.asyncio
    async def test_send_email(self):
        """Test send_email MCP tool."""
        command_handler = AsyncMock(
            return_value=SendMutationOutcome(
                delivery=tuple(
                    TargetMutationOutcome(address, "succeeded")
                    for address in ("recipient@example.com", "cc@example.com", "bcc@example.com")
                ),
                sent_copy=SentCopyMutationOutcome("skipped"),
            )
        )
        with patch("mcp_email_server.app.send_email_command", command_handler):
            result = await send_email(
                account_name="test_account",
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
                cc=["cc@example.com"],
                bcc=["bcc@example.com"],
            )

        assert result == "Email sent successfully to recipient@example.com"
        command = command_handler.await_args.args[0]
        assert command.recipients == ("recipient@example.com",)
        assert command.cc == ("cc@example.com",)
        assert command.bcc == ("bcc@example.com",)

    @pytest.mark.asyncio
    async def test_delete_emails(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345", "12346")))
        with patch("mcp_email_server.app.delete_emails_command", command_handler):
            result = await delete_emails("test_account", ["12345", "12346"])
        assert result == "Successfully deleted 2 email(s)"
        assert command_handler.await_args.args[0].mailbox == "INBOX"

    @pytest.mark.asyncio
    async def test_delete_emails_with_failures(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345",), failed=("12346", "12347")))
        with patch("mcp_email_server.app.delete_emails_command", command_handler):
            result = await delete_emails("test_account", ["12345", "12346", "12347"])
        assert result == "Delete result [succeeded: 12345; failed: 12346, 12347]"

    @pytest.mark.asyncio
    async def test_delete_emails_with_mailbox(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345",)))
        with patch("mcp_email_server.app.delete_emails_command", command_handler):
            result = await delete_emails("test_account", ["12345"], "Trash")
        assert result == "Successfully deleted 1 email(s)"
        assert command_handler.await_args.args[0].mailbox == "Trash"

    @pytest.mark.asyncio
    async def test_mark_emails_as_read(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345", "12346")))
        with patch("mcp_email_server.app.mark_read_command", command_handler):
            result = await mark_emails_as_read("test_account", ["12345", "12346"])
        assert result == "Successfully marked 2 email(s) as read"

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_with_failures(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345",), failed=("12346",)))
        with patch("mcp_email_server.app.mark_read_command", command_handler):
            result = await mark_emails_as_read("test_account", ["12345", "12346"])
        assert result == "Mark-read result [succeeded: 12345; failed: 12346]"

    @pytest.mark.asyncio
    async def test_mark_emails_as_read_with_mailbox(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345",)))
        with patch("mcp_email_server.app.mark_read_command", command_handler):
            result = await mark_emails_as_read("test_account", ["12345"], "Sent")
        assert result == "Successfully marked 1 email(s) as read"
        assert command_handler.await_args.args[0].mailbox == "Sent"

    @pytest.mark.asyncio
    async def test_download_attachment_maps_application_command(self):
        attachment_response = AttachmentDownloadResponse(
            email_id="12345",
            attachment_name="document.pdf",
            mime_type="application/pdf",
            size=1024,
            saved_path="/var/downloads/document.pdf",
        )
        command_handler = AsyncMock(return_value=attachment_response)

        with patch("mcp_email_server.app.download_attachment_command", command_handler):
            result = await download_attachment(
                account_name="test_account",
                email_id="12345",
                attachment_name="document.pdf",
                save_path="/var/downloads/document.pdf",
            )

        assert result == attachment_response
        command = command_handler.await_args.args[0]
        assert command.email_id == "12345"
        assert command.attachment_name == "document.pdf"
        assert command.save_path == "/var/downloads/document.pdf"
        assert command.mailbox == "INBOX"

    @pytest.mark.asyncio
    async def test_download_attachment_allows_default_destination(self):
        attachment_response = AttachmentDownloadResponse(
            email_id="12345",
            attachment_name="document.pdf",
            mime_type="application/pdf",
            size=1024,
            saved_path="/home/user/Downloads/mcp-email-server/document-abcd.pdf",
        )
        command_handler = AsyncMock(return_value=attachment_response)

        with patch("mcp_email_server.app.download_attachment_command", command_handler):
            result = await download_attachment(
                account_name="test_account",
                email_id="12345",
                attachment_name="document.pdf",
            )

        assert result == attachment_response
        command = command_handler.await_args.args[0]
        assert command.save_path is None
        assert command.mailbox == "INBOX"

    @pytest.mark.asyncio
    async def test_send_email_with_reply_headers(self):
        command_handler = AsyncMock(
            return_value=SendMutationOutcome(
                (TargetMutationOutcome("recipient@example.com", "succeeded"),),
                SentCopyMutationOutcome("skipped"),
            )
        )
        with patch("mcp_email_server.app.send_email_command", command_handler):
            result = await send_email(
                account_name="test",
                recipients=["recipient@example.com"],
                subject="Re: Test",
                body="Reply body",
                in_reply_to="<original@example.com>",
                references="<original@example.com>",
            )

        command = command_handler.await_args.args[0]
        assert command.in_reply_to == "<original@example.com>"
        assert command.references == "<original@example.com>"
        assert "recipient@example.com" in result

    @pytest.mark.asyncio
    async def test_get_emails_content_maps_application_query_and_preserves_thread_headers(self):
        response = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="123",
                    message_id="<test@example.com>",
                    in_reply_to="<parent@example.com>",
                    references="<root@example.com> <parent@example.com>",
                    subject="Test",
                    sender="sender@example.com",
                    recipients=["recipient@example.com"],
                    date=datetime.now(UTC),
                    body="Test body",
                    attachments=[],
                )
            ],
            requested_count=1,
            retrieved_count=1,
            failed_ids=[],
        )
        query_handler = AsyncMock(return_value=response)

        with patch("mcp_email_server.app.get_email_content_query", query_handler):
            result = await get_emails_content(
                account_name="test",
                email_ids=["123"],
                mark_as_read=True,
            )

        assert result.emails[0].message_id == "<test@example.com>"
        assert result.emails[0].in_reply_to == "<parent@example.com>"
        assert result.emails[0].references == "<root@example.com> <parent@example.com>"
        query = query_handler.await_args.args[0]
        assert query.account_name == "test"
        assert query.email_ids == ("123",)
        assert query.mark_as_read is True
        assert query.mailbox == "INBOX"

    @pytest.mark.asyncio
    async def test_get_emails_content_adapter_preserves_large_compatible_batch(self):
        email_ids = [str(index) for index in range(1, 103)]
        response = EmailContentBatchResponse(
            emails=[], requested_count=len(email_ids), retrieved_count=0, failed_ids=email_ids
        )
        query_handler = AsyncMock(return_value=response)

        with patch("mcp_email_server.app.get_email_content_query", query_handler):
            await get_emails_content("test", email_ids, mark_as_read=True)

        assert query_handler.await_args.args[0].email_ids == tuple(email_ids)

    @pytest.mark.asyncio
    async def test_get_emails_content_body_offset_and_max_body_length(self):
        response = EmailContentBatchResponse(emails=[], requested_count=1, retrieved_count=0, failed_ids=["123"])
        query_handler = AsyncMock(return_value=response)

        with patch("mcp_email_server.app.get_email_content_query", query_handler):
            await get_emails_content(
                account_name="test",
                email_ids=["123"],
                body_offset=4000,
                max_body_length=2000,
            )

        query = query_handler.await_args.args[0]
        assert query.body_offset == 4000
        assert query.max_body_length == 2000

    @pytest.mark.asyncio
    async def test_move_emails(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345", "12346")))
        with patch("mcp_email_server.app.move_emails_command", command_handler):
            result = await move_emails("test_account", ["12345", "12346"], "Archive")
        assert result == "Successfully moved 2 email(s) to Archive"
        assert command_handler.await_args.args[0].source_mailbox == "INBOX"

    @pytest.mark.asyncio
    async def test_archive_emails(self):
        command_handler = AsyncMock(
            return_value=ArchiveMutationOutcome(_batch_outcome(succeeded=("12345", "12346")), "Archive")
        )
        with patch("mcp_email_server.app.archive_emails_command", command_handler):
            result = await archive_emails("test_account", ["12345", "12346"])
        assert result == "Successfully archived 2 email(s) to Archive"

    @pytest.mark.asyncio
    async def test_archive_emails_with_failures(self):
        command_handler = AsyncMock(
            return_value=ArchiveMutationOutcome(
                _batch_outcome(succeeded=("12345",), failed=("12346",)),
                "[Gmail]/All Mail",
            )
        )
        with patch("mcp_email_server.app.archive_emails_command", command_handler):
            result = await archive_emails("test_account", ["12345", "12346"])
        assert result == ("Archive result [succeeded: 12345; failed: 12346; mailbox: [Gmail]/All Mail]")

    @pytest.mark.asyncio
    async def test_move_emails_with_source_mailbox(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345",)))
        with patch("mcp_email_server.app.move_emails_command", command_handler):
            result = await move_emails("test_account", ["12345"], "INBOX", "Trash")
        assert result == "Successfully moved 1 email(s) to INBOX"
        assert command_handler.await_args.args[0].source_mailbox == "Trash"

    @pytest.mark.asyncio
    async def test_move_emails_with_failures(self):
        command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("12345",), failed=("12346", "12347")))
        with patch("mcp_email_server.app.move_emails_command", command_handler):
            result = await move_emails("test_account", ["12345", "12346", "12347"], "Archive")
        assert result == "Move result [succeeded: 12345; failed: 12346, 12347]"

    @pytest.mark.asyncio
    async def test_list_mailboxes(self):
        mailboxes = [
            MailboxInfo(name="INBOX", delimiter="/", flags=["\\HasChildren"]),
            MailboxInfo(name="Sent", delimiter="/", flags=["\\Sent", "\\HasNoChildren"]),
            MailboxInfo(name="Drafts", delimiter="/", flags=["\\Drafts", "\\HasNoChildren"]),
            MailboxInfo(name="Trash", delimiter="/", flags=["\\Trash", "\\HasNoChildren"]),
            MailboxInfo(name="Archive", delimiter="/", flags=["\\HasNoChildren"]),
        ]
        query_handler = AsyncMock(return_value=mailboxes)

        with patch("mcp_email_server.app.list_mailboxes_query", query_handler):
            result = await list_mailboxes(account_name="test_account")

        assert result == mailboxes
        query = query_handler.await_args.args[0]
        assert query.account_name == "test_account"
        assert query.pattern == "*"
        assert query.reference == ""

    @pytest.mark.asyncio
    async def test_list_mailboxes_with_pattern(self):
        mailboxes = [
            MailboxInfo(name="INBOX.Clients", delimiter=".", flags=["\\HasNoChildren"]),
            MailboxInfo(name="INBOX.Projects", delimiter=".", flags=["\\HasNoChildren"]),
        ]
        query_handler = AsyncMock(return_value=mailboxes)

        with patch("mcp_email_server.app.list_mailboxes_query", query_handler):
            result = await list_mailboxes(account_name="test_account", pattern="INBOX.*")

        assert result == mailboxes
        assert query_handler.await_args.args[0].pattern == "INBOX.*"

    @pytest.mark.asyncio
    async def test_list_allowed_recipients_returns_selected_authority_policy(self):
        configuration = EffectiveConfiguration(
            accounts=(),
            allowed_recipients=("alice@example.com", "bob@example.com"),
            allowed_senders=(),
        )
        with patch("mcp_email_server.app.effective_configuration", return_value=configuration):
            result = await list_allowed_recipients()
        assert result == ["alice@example.com", "bob@example.com"]

    @pytest.mark.asyncio
    async def test_send_email_no_allowlist_allows_any_recipient(self):
        command_handler = AsyncMock(
            return_value=SendMutationOutcome(
                (TargetMutationOutcome("anyone@example.com", "succeeded"),),
                SentCopyMutationOutcome("skipped"),
            )
        )
        with (
            patch("mcp_email_server.app.send_email_command", command_handler),
        ):
            result = await send_email(account_name="test", recipients=["anyone@example.com"], subject="S", body="B")
        assert "anyone@example.com" in result
        command_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_email_maps_application_recipient_denial(self):
        command_handler = AsyncMock(
            side_effect=RecipientPolicyDeniedError("recipient policy denied one or more addresses")
        )
        with patch("mcp_email_server.app.send_email_command", command_handler):
            with pytest.raises(ValueError, match="not in allowlist"):
                await send_email(account_name="test", recipients=["mallory@evil.com"], subject="S", body="B")
        command_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_email_does_not_mislabel_unrelated_permission_failure(self):
        command_handler = AsyncMock(side_effect=PermissionError("attachment access denied"))
        with patch("mcp_email_server.app.send_email_command", command_handler):
            with pytest.raises(PermissionError, match="attachment access denied"):
                await send_email(account_name="test", recipients=["allowed@example.com"], subject="S", body="B")

    @pytest.mark.asyncio
    async def test_send_email_maps_application_bcc_denial(self):
        command_handler = AsyncMock(
            side_effect=RecipientPolicyDeniedError("recipient policy denied one or more addresses")
        )
        with patch("mcp_email_server.app.send_email_command", command_handler):
            with pytest.raises(ValueError, match="not in allowlist"):
                await send_email(
                    account_name="test",
                    recipients=["alice@example.com"],
                    subject="S",
                    body="B",
                    bcc=["mallory@evil.com"],
                )
        command_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_email_allows_listed_recipient_with_display_name(self):
        command_handler = AsyncMock(
            return_value=SendMutationOutcome(
                (TargetMutationOutcome("Alice <Alice@Example.com>", "succeeded"),),
                SentCopyMutationOutcome("skipped"),
            )
        )
        with (
            patch("mcp_email_server.app.send_email_command", command_handler),
        ):
            await send_email(
                account_name="test",
                recipients=["Alice <Alice@Example.com>"],
                subject="S",
                body="B",
            )
        command_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_to_mailbox_maps_application_recipient_denial(self):
        command_handler = AsyncMock(
            side_effect=RecipientPolicyDeniedError("recipient policy denied one or more addresses")
        )
        with patch("mcp_email_server.app.save_to_mailbox_command", command_handler):
            with pytest.raises(ValueError, match="not in allowlist"):
                await save_to_mailbox(account_name="test", recipients=["mallory@evil.com"], subject="S", body="B")
        command_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_to_mailbox_allows_listed_recipient(self):
        command_handler = AsyncMock(
            return_value=AppendMutationOutcome("succeeded", "<mid@example.com>", uid="42", mailbox="Drafts")
        )
        with (
            patch("mcp_email_server.app.save_to_mailbox_command", command_handler),
        ):
            result = await save_to_mailbox(account_name="test", recipients=["alice@example.com"], subject="S", body="B")
        command_handler.assert_awaited_once()
        assert "saved" in result.lower()

    @pytest.mark.asyncio
    async def test_send_email_propagates_application_packed_address_validation(self):
        command_handler = AsyncMock(
            side_effect=ValueError("each recipient value must contain exactly one email address")
        )
        with patch("mcp_email_server.app.send_email_command", command_handler):
            with pytest.raises(ValueError, match="exactly one email address"):
                await send_email(
                    account_name="test",
                    recipients=["alice@example.com, mallory@evil.com"],
                    subject="S",
                    body="B",
                )
        command_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_to_mailbox_propagates_application_packed_address_validation(self):
        command_handler = AsyncMock(
            side_effect=ValueError("each recipient value must contain exactly one email address")
        )
        with patch("mcp_email_server.app.save_to_mailbox_command", command_handler):
            with pytest.raises(ValueError, match="exactly one email address"):
                await save_to_mailbox(
                    account_name="test",
                    recipients=["alice@example.com, mallory@evil.com"],
                    subject="S",
                    body="B",
                )
        command_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_allowed_senders_returns_selected_authority_policy(self):
        configuration = EffectiveConfiguration(
            accounts=(),
            allowed_recipients=(),
            allowed_senders=("*@example.com", "bob@example.com"),
        )
        with patch("mcp_email_server.app.effective_configuration", return_value=configuration):
            result = await list_allowed_senders()
        assert result == ["*@example.com", "bob@example.com"]


@pytest.mark.asyncio
async def test_set_email_flags_command_dispatches_to_runtime_service() -> None:
    outcome = _batch_outcome(succeeded=("1",))
    runtime = MagicMock()
    runtime.mutations.set_flags.execute = AsyncMock(return_value=outcome)
    command = SetEmailFlagsCommand("test", ("1",), "add", (r"\Seen",))

    with patch("mcp_email_server.app.get_application_runtime", return_value=runtime):
        assert await app_module.set_email_flags_command(command) is outcome

    runtime.mutations.set_flags.execute.assert_awaited_once_with(command)


@pytest.mark.asyncio
async def test_set_email_flags_maps_command_and_formats_success() -> None:
    command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("1", "2")))
    with patch("mcp_email_server.app.set_email_flags_command", command_handler):
        result = await set_email_flags(
            "test",
            ["1", "2"],
            "remove",
            [r"\Seen", r"\Flagged"],
            "Archive",
        )

    assert result == r"Successfully removed \Seen, \Flagged from 2 email(s)"
    command = command_handler.await_args.args[0]
    assert command.account_name == "test"
    assert command.email_ids == ("1", "2")
    assert command.operation == "remove"
    assert command.flags == (r"\Seen", r"\Flagged")
    assert command.mailbox == "Archive"


@pytest.mark.asyncio
async def test_set_email_flags_formats_add_success() -> None:
    command_handler = AsyncMock(return_value=_batch_outcome(succeeded=("1",)))
    with patch("mcp_email_server.app.set_email_flags_command", command_handler):
        result = await set_email_flags("test", ["1"], "add", [r"\Flagged"])

    assert result == r"Successfully added \Flagged to 1 email(s)"


@pytest.mark.asyncio
async def test_set_email_flags_formats_partial_and_ambiguous_outcomes() -> None:
    command_handler = AsyncMock(
        return_value=BatchMutationOutcome((
            TargetMutationOutcome("1", "succeeded"),
            TargetMutationOutcome("2", "unknown", "store-unknown"),
        ))
    )
    with patch("mcp_email_server.app.set_email_flags_command", command_handler):
        result = await set_email_flags("test", ["1", "2"], "add", [r"\Flagged"])

    assert result == "Set-flags result [succeeded: 1; unknown: 2 (store-unknown); warning: reconciliation needed]"


@pytest.mark.asyncio
async def test_mutation_tool_formats_unknown_and_reconciliation_status() -> None:
    command_handler = AsyncMock(
        return_value=BatchMutationOutcome(
            (
                TargetMutationOutcome("1", "succeeded"),
                TargetMutationOutcome("2", "unknown", "store"),
            ),
            reconciliation_needed=True,
        )
    )
    with patch("mcp_email_server.app.mark_read_command", command_handler):
        result = await mark_emails_as_read("test", ["1", "2"])

    assert result == "Mark-read result [succeeded: 1; unknown: 2 (store); warning: reconciliation needed]"


@pytest.mark.asyncio
async def test_mutation_tool_preserves_input_order_across_status_tags() -> None:
    command_handler = AsyncMock(
        return_value=BatchMutationOutcome((
            TargetMutationOutcome("1", "failed"),
            TargetMutationOutcome("2", "succeeded"),
            TargetMutationOutcome("3", "unknown", "store-unknown"),
        ))
    )
    with patch("mcp_email_server.app.mark_read_command", command_handler):
        result = await mark_emails_as_read("test", ["1", "2", "3"])

    assert result == (
        "Mark-read result [failed: 1; succeeded: 2; unknown: 3 (store-unknown); warning: reconciliation needed]"
    )


@pytest.mark.asyncio
async def test_send_tool_preserves_recipient_order_across_status_tags() -> None:
    command_handler = AsyncMock(
        return_value=SendMutationOutcome(
            (
                TargetMutationOutcome("first@example.test", "failed", "smtp-recipient-rejected"),
                TargetMutationOutcome("second@example.test", "succeeded"),
                TargetMutationOutcome("third@example.test", "unknown", "provider-timeout"),
            ),
            SentCopyMutationOutcome("skipped"),
        )
    )
    with patch("mcp_email_server.app.send_email_command", command_handler):
        result = await send_email(
            "test",
            ["first@example.test", "second@example.test", "third@example.test"],
            "Subject",
            "body",
        )

    assert result == (
        "Email delivery [failed: first@example.test (smtp-recipient-rejected); "
        "succeeded: second@example.test; unknown: third@example.test (provider-timeout); "
        "sent-copy: skipped; warning: reconciliation needed]"
    )


@pytest.mark.asyncio
async def test_send_tool_does_not_expose_unrecognized_provider_detail() -> None:
    command_handler = AsyncMock(
        return_value=SendMutationOutcome(
            (TargetMutationOutcome("recipient@example.test", "failed", "private provider response"),),
            SentCopyMutationOutcome("skipped"),
        )
    )
    with patch("mcp_email_server.app.send_email_command", command_handler):
        result = await send_email("test", ["recipient@example.test"], "Subject", "body")

    assert result == "Email delivery [failed: recipient@example.test; sent-copy: skipped]"
    assert "private provider response" not in result


@pytest.mark.asyncio
async def test_send_tool_keeps_delivery_success_when_sent_copy_fails() -> None:
    command_handler = AsyncMock(
        return_value=SendMutationOutcome(
            (TargetMutationOutcome("recipient@example.test", "succeeded"),),
            SentCopyMutationOutcome("failed", "Sent", "append"),
        )
    )
    with patch("mcp_email_server.app.send_email_command", command_handler):
        result = await send_email("test", ["recipient@example.test"], "Subject", "body")

    assert result == "Email delivery [succeeded: recipient@example.test; sent-copy: failed (Sent)]"


@pytest.mark.asyncio
async def test_send_tool_reports_safe_internationalized_sent_copy_failure() -> None:
    command_handler = AsyncMock(
        return_value=SendMutationOutcome(
            (TargetMutationOutcome("recipient@example.test", "succeeded"),),
            SentCopyMutationOutcome("failed", detail="utf8-append-unsupported"),
        )
    )
    with patch("mcp_email_server.app.send_email_command", command_handler):
        result = await send_email("test", ["recipient@example.test"], "Subject", "body")

    assert result == ("Email delivery [succeeded: recipient@example.test; sent-copy: failed (utf8-append-unsupported)]")


@pytest.mark.asyncio
async def test_save_tool_does_not_claim_success_for_ambiguous_append() -> None:
    command_handler = AsyncMock(
        return_value=AppendMutationOutcome(
            "unknown",
            "<draft@example.test>",
            mailbox="Drafts",
            detail="append-unknown",
        )
    )
    with patch("mcp_email_server.app.save_to_mailbox_command", command_handler):
        result = await save_to_mailbox("test", ["recipient@example.test"], "Draft", "body")

    assert result == (
        "Email save [unknown (append-unknown): Drafts; Message-Id: <draft@example.test>; warning: reconciliation needed]"
    )


@pytest.mark.asyncio
async def test_save_tool_hides_unrecognized_provider_detail() -> None:
    command_handler = AsyncMock(
        return_value=AppendMutationOutcome(
            "unknown",
            "<draft@example.test>",
            mailbox="Drafts",
            detail="private provider response",
        )
    )
    with patch("mcp_email_server.app.save_to_mailbox_command", command_handler):
        result = await save_to_mailbox("test", ["recipient@example.test"], "Draft", "body")

    assert result == ("Email save [unknown: Drafts; Message-Id: <draft@example.test>; warning: reconciliation needed]")
    assert "private provider response" not in result


@pytest.mark.asyncio
async def test_save_tool_reports_safe_internationalized_append_failure() -> None:
    command_handler = AsyncMock(
        return_value=AppendMutationOutcome(
            "failed",
            "<draft@example.test>",
            mailbox="Drafts",
            detail="utf8-append-unsupported",
        )
    )
    with patch("mcp_email_server.app.save_to_mailbox_command", command_handler):
        result = await save_to_mailbox("test", ["recipient@example.test"], "Draft", "body")

    assert result == ("Email save [failed (utf8-append-unsupported): Drafts; Message-Id: <draft@example.test>]")


@pytest.mark.asyncio
async def test_account_discovery_text_and_structured_content_are_consistent() -> None:
    accounts = [
        AvailableAccount(
            account_name="work",
            account_type="email",
            description="Primary account",
            email_address="user@example.test",
            can_receive=True,
            can_send=True,
        ),
        AvailableAccount(
            account_name="legacy-provider",
            account_type="provider",
            description="Unsupported provider",
            can_receive=False,
            can_send=False,
        ),
    ]
    with patch("mcp_email_server.app.list_effective_accounts", return_value=accounts):
        content, structured = await app_module.mcp.call_tool("list_available_accounts", {})

    assert structured == {"result": [account.model_dump(mode="json") for account in accounts]}
    assert all(isinstance(block, TextContent) for block in content)
    assert [json.loads(block.text) for block in content if isinstance(block, TextContent)] == structured["result"]


@pytest.mark.asyncio
async def test_tool_annotations_expose_agent_safety_and_retry_hints() -> None:
    tools = {tool.name: tool for tool in await app_module.mcp.list_tools()}

    assert tools["list_available_accounts"].annotations is not None
    assert tools["list_available_accounts"].annotations.model_dump(exclude_none=True) == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert tools["send_email"].annotations is not None
    assert tools["send_email"].annotations.model_dump(exclude_none=True) == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert tools["delete_emails"].annotations is not None
    assert tools["delete_emails"].annotations.model_dump(exclude_none=True) == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert all(tool.annotations is not None for tool in tools.values())
