"""Resource-exhaustion ceilings: page_size, email_ids batch, attachment size."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_email_server import app as app_module
from mcp_email_server.app import MAX_EMAIL_IDS_PER_CALL, MAX_PAGE_SIZE
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_COUNT,
    MAX_TOTAL_ATTACHMENT_BYTES,
    EmailClient,
)


class TestToolInputCeilings:
    @pytest.mark.asyncio
    async def test_page_size_over_limit_rejected(self):
        with pytest.raises(ToolError):
            await app_module.mcp.call_tool(
                "list_emails_metadata", {"account_name": "x", "page_size": MAX_PAGE_SIZE + 1}
            )

    @pytest.mark.asyncio
    async def test_page_zero_rejected(self):
        with pytest.raises(ToolError):
            await app_module.mcp.call_tool("list_emails_metadata", {"account_name": "x", "page": 0})

    @pytest.mark.asyncio
    async def test_email_ids_over_limit_rejected(self):
        ids = [str(i) for i in range(MAX_EMAIL_IDS_PER_CALL + 1)]
        with pytest.raises(ToolError):
            await app_module.mcp.call_tool("get_emails_content", {"account_name": "x", "email_ids": ids})

    @pytest.mark.asyncio
    async def test_empty_email_ids_rejected(self):
        with pytest.raises(ToolError):
            await app_module.mcp.call_tool("get_emails_content", {"account_name": "x", "email_ids": []})

    @pytest.mark.asyncio
    async def test_schema_advertises_bounds(self):
        # The published inputSchema must carry the constraints so clients see them.
        tools = await app_module.mcp.list_tools()
        by_name = {t.name: t for t in tools}
        meta = by_name["list_emails_metadata"].inputSchema["properties"]
        assert meta["page_size"]["maximum"] == MAX_PAGE_SIZE
        assert meta["page_size"]["minimum"] == 1
        assert meta["page"]["minimum"] == 1
        content = by_name["get_emails_content"].inputSchema["properties"]
        assert content["email_ids"]["maxItems"] == MAX_EMAIL_IDS_PER_CALL
        assert content["email_ids"]["minItems"] == 1


@pytest.fixture
def email_client():
    server = EmailServer(user_name="u", password="p", host="smtp.example.com", port=465, use_ssl=True)
    return EmailClient(server, sender="Test User <test@example.com>")


class TestAttachmentSizeCap:
    def test_oversize_attachment_rejected(self, email_client, tmp_path, monkeypatch):
        # Shrink the cap so we don't have to write 25 MB to disk.
        monkeypatch.setattr("mcp_email_server.emails.classic.MAX_ATTACHMENT_BYTES", 1024)
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 2048)
        with pytest.raises(ValueError, match="exceeding the"):
            email_client._validate_attachment(str(big))

    def test_within_limit_attachment_accepted(self, email_client, tmp_path):
        ok = tmp_path / "ok.bin"
        ok.write_bytes(b"x" * 16)
        assert email_client._validate_attachment(str(ok)).name == "ok.bin"

    def test_default_caps(self):
        assert MAX_ATTACHMENT_BYTES == 25 * 1024 * 1024
        assert MAX_TOTAL_ATTACHMENT_BYTES == 50 * 1024 * 1024
        assert MAX_ATTACHMENT_COUNT == 50


class TestAggregateAttachmentCaps:
    def test_too_many_attachments_rejected(self, email_client, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_email_server.emails.classic.MAX_ATTACHMENT_COUNT", 3)
        files = []
        for i in range(4):
            f = tmp_path / f"f{i}.bin"
            f.write_bytes(b"x")
            files.append(str(f))
        with pytest.raises(ValueError, match="Too many attachments"):
            email_client._create_message_with_attachments("body", False, files)

    def test_total_size_over_limit_rejected(self, email_client, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_email_server.emails.classic.MAX_TOTAL_ATTACHMENT_BYTES", 1000)
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.bin"
            f.write_bytes(b"x" * 600)  # 3 * 600 = 1800 > 1000
            files.append(str(f))
        with pytest.raises(ValueError, match="Total attachment size"):
            email_client._create_message_with_attachments("body", False, files)

    def test_within_aggregate_limits_ok(self, email_client, tmp_path):
        files = []
        for i in range(2):
            f = tmp_path / f"f{i}.bin"
            f.write_bytes(b"x" * 16)
            files.append(str(f))
        msg = email_client._create_message_with_attachments("body", False, files)
        # text part + 2 attachments
        assert len(msg.get_payload()) == 3
