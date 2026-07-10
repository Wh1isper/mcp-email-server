"""Capability-scope permission model: config semantics, tool visibility, call-time enforcement."""

from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server import app as app_module
from mcp_email_server.app import (
    _is_drafts_mailbox,
    add_email_account,
    archive_emails,
    delete_emails,
    get_emails_content,
    mark_emails_as_read,
    move_emails,
    save_to_mailbox,
    send_email,
)
from mcp_email_server.config import EmailServer, EmailSettings, Settings

READ_TOOLS = {
    "list_available_accounts",
    "list_emails_metadata",
    "get_emails_content",
    "list_mailboxes",
    "download_attachment",
}
ORGANIZE_TOOLS = {"mark_emails_as_read", "move_emails", "archive_emails"}


def make_settings(monkeypatch, scopes=None, send_capable=False):
    """Real Settings with the given scopes and no env permissions override.

    Settings' only source is the TOML file (init kwargs are ignored), so fields
    are assigned post-construction; validate_assignment covers validation.
    """
    monkeypatch.delenv("MCP_EMAIL_SERVER_PERMISSIONS", raising=False)
    settings = Settings()
    if scopes is not None:
        settings.permissions = scopes
    if send_capable:
        server = EmailServer(
            user_name="u",
            password="p",
            host="mail.example.com",
            port=993,
            use_ssl=True,
        )
        settings.emails = [
            EmailSettings(
                account_name="acct",
                full_name="U",
                email_address="u@example.com",
                incoming=server,
                outgoing=server,
            )
        ]
    return settings


async def visible_tools(settings):
    with patch("mcp_email_server.app.get_settings", return_value=settings):
        return {tool.name for tool in await app_module.mcp.list_tools()}


class TestScopeConfig:
    def test_default_is_read_only(self, monkeypatch):
        settings = make_settings(monkeypatch)
        assert settings.permissions == ["read"]
        assert settings.has_scope("read")
        for scope in ("draft", "organize", "delete", "send", "manage"):
            assert not settings.has_scope(scope)

    def test_full_grants_everything(self, monkeypatch):
        settings = make_settings(monkeypatch, ["full"])
        for scope in ("read", "draft", "organize", "delete", "send", "manage"):
            assert settings.has_scope(scope)

    def test_explicit_scopes_only(self, monkeypatch):
        settings = make_settings(monkeypatch, ["draft", "send"])
        assert settings.has_scope("read")  # always implied
        assert settings.has_scope("draft")
        assert settings.has_scope("send")
        for scope in ("organize", "delete", "manage"):
            assert not settings.has_scope(scope)

    def test_unknown_scope_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="Unknown permission scope"):
            make_settings(monkeypatch, ["write"])

    def test_scopes_normalized(self, monkeypatch):
        settings = make_settings(monkeypatch, [" Send ", "DELETE", "send"])
        assert settings.permissions == ["send", "delete"]

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_PERMISSIONS", "send, ORGANIZE")
        settings = Settings()
        assert settings.effective_permissions == ["send", "organize"]
        assert settings.has_scope("send")
        assert settings.has_scope("organize")
        assert not settings.has_scope("delete")

    def test_env_empty_string_resets_to_read_only(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_PERMISSIONS", "")
        settings = Settings()
        settings.permissions = ["full"]  # env override still wins over the field
        assert settings.effective_permissions == ["read"]
        assert not settings.has_scope("send")

    def test_env_unknown_scope_rejected(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_PERMISSIONS", "admin")
        with pytest.raises(ValueError, match="Unknown permission scope"):
            Settings()

    def test_env_override_not_persisted_by_store(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MCP_EMAIL_SERVER_PERMISSIONS", "full")
        cfg = tmp_path / "config.toml"
        monkeypatch.setitem(Settings.model_config, "toml_file", cfg)
        settings = Settings()
        assert settings.has_scope("delete")  # env grants full at runtime
        settings.store()
        import tomllib

        stored = tomllib.loads(cfg.read_text())
        assert stored["permissions"] == ["read"]


class TestDraftsMailboxDetection:
    @pytest.mark.parametrize(
        "mailbox", ["Drafts", "drafts", "Draft", "INBOX.Drafts", "[Gmail]/Drafts", '"Drafts"', "INBOX/Drafts"]
    )
    def test_drafts_folders(self, mailbox):
        assert _is_drafts_mailbox(mailbox)

    @pytest.mark.parametrize("mailbox", ["INBOX", "Sent", "Drafts2", "Archive", "INBOX.Drafts.Old"])
    def test_non_drafts_folders(self, mailbox):
        assert not _is_drafts_mailbox(mailbox)


class TestToolVisibility:
    @pytest.mark.asyncio
    async def test_read_only_hides_all_mutation_tools(self, monkeypatch):
        tools = await visible_tools(make_settings(monkeypatch, send_capable=True))
        assert tools >= READ_TOOLS
        assert not (ORGANIZE_TOOLS & tools)
        for hidden in ("delete_emails", "send_email", "save_to_mailbox", "add_email_account"):
            assert hidden not in tools

    @pytest.mark.asyncio
    async def test_full_shows_everything(self, monkeypatch):
        tools = await visible_tools(make_settings(monkeypatch, ["full"], send_capable=True))
        assert tools >= READ_TOOLS | ORGANIZE_TOOLS
        for name in ("delete_emails", "send_email", "save_to_mailbox", "add_email_account"):
            assert name in tools

    @pytest.mark.asyncio
    async def test_organize_without_delete(self, monkeypatch):
        tools = await visible_tools(make_settings(monkeypatch, ["organize"]))
        assert tools >= ORGANIZE_TOOLS
        assert "delete_emails" not in tools

    @pytest.mark.asyncio
    async def test_send_without_delete(self, monkeypatch):
        tools = await visible_tools(make_settings(monkeypatch, ["send"], send_capable=True))
        assert "send_email" in tools
        assert "delete_emails" not in tools
        assert "save_to_mailbox" not in tools

    @pytest.mark.asyncio
    async def test_send_scope_still_requires_send_capable_account(self, monkeypatch):
        tools = await visible_tools(make_settings(monkeypatch, ["send", "draft"]))
        assert "send_email" not in tools
        assert "save_to_mailbox" not in tools

    @pytest.mark.asyncio
    async def test_manage_gates_add_email_account(self, monkeypatch):
        assert "add_email_account" in await visible_tools(make_settings(monkeypatch, ["manage"]))
        assert "add_email_account" not in await visible_tools(make_settings(monkeypatch, ["send", "delete"]))


class TestCallTimeEnforcement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool,kwargs",
        [
            (delete_emails, {"account_name": "a", "email_ids": ["1"]}),
            (mark_emails_as_read, {"account_name": "a", "email_ids": ["1"]}),
            (move_emails, {"account_name": "a", "email_ids": ["1"], "destination_mailbox": "X"}),
            (archive_emails, {"account_name": "a", "email_ids": ["1"]}),
            (send_email, {"account_name": "a", "recipients": ["x@example.com"], "subject": "s", "body": "b"}),
        ],
    )
    async def test_mutation_tools_blocked_read_only(self, monkeypatch, tool, kwargs):
        settings = make_settings(monkeypatch)
        mock_handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler),
        ):
            with pytest.raises(PermissionError, match="not granted"):
                await tool(**kwargs)
        assert not mock_handler.method_calls

    @pytest.mark.asyncio
    async def test_add_email_account_blocked_without_manage(self, monkeypatch, email_settings):
        settings = make_settings(monkeypatch, ["send", "delete"])
        with patch("mcp_email_server.app.get_settings", return_value=settings):
            with pytest.raises(PermissionError, match="'manage'"):
                await add_email_account(email_settings)

    @pytest.mark.asyncio
    async def test_get_emails_content_mark_as_read_needs_organize(self, monkeypatch):
        settings = make_settings(monkeypatch)
        mock_handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler),
        ):
            with pytest.raises(PermissionError, match="'organize'"):
                await get_emails_content(account_name="a", email_ids=["1"], mark_as_read=True)
        mock_handler.get_emails_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_emails_content_plain_read_allowed_read_only(self, monkeypatch):
        settings = make_settings(monkeypatch)
        mock_handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler),
        ):
            await get_emails_content(account_name="a", email_ids=["1"])
        mock_handler.get_emails_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_emails_content_mark_as_read_allowed_with_organize(self, monkeypatch):
        settings = make_settings(monkeypatch, ["organize"])
        mock_handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler),
        ):
            await get_emails_content(account_name="a", email_ids=["1"], mark_as_read=True)
        mock_handler.get_emails_content.assert_called_once()


class TestDraftScopeFolderRestriction:
    def _mock_handler(self):
        handler = AsyncMock()
        handler.save_to_mailbox.return_value = "<msg-id>|uid:7"
        return handler

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mailbox", ["Drafts", "INBOX.Drafts", "[Gmail]/Drafts"])
    async def test_draft_only_allows_drafts_folders(self, monkeypatch, mailbox):
        settings = make_settings(monkeypatch, ["draft"], send_capable=True)
        mock_handler = self._mock_handler()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler),
        ):
            result = await save_to_mailbox(
                account_name="a", recipients=["x@example.com"], subject="s", body="b", mailbox=mailbox
            )
        assert "saved" in result
        mock_handler.save_to_mailbox.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mailbox", ["INBOX", "Sent", "Archive"])
    async def test_draft_only_blocks_other_folders(self, monkeypatch, mailbox):
        settings = make_settings(monkeypatch, ["draft"], send_capable=True)
        mock_handler = self._mock_handler()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler),
        ):
            with pytest.raises(PermissionError, match="drafts-type"):
                await save_to_mailbox(
                    account_name="a", recipients=["x@example.com"], subject="s", body="b", mailbox=mailbox
                )
        mock_handler.save_to_mailbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_draft_plus_organize_allows_any_folder(self, monkeypatch):
        settings = make_settings(monkeypatch, ["draft", "organize"], send_capable=True)
        mock_handler = self._mock_handler()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler),
        ):
            result = await save_to_mailbox(
                account_name="a", recipients=["x@example.com"], subject="s", body="b", mailbox="Templates"
            )
        assert "saved" in result

    @pytest.mark.asyncio
    async def test_save_to_mailbox_blocked_without_draft_scope(self, monkeypatch):
        settings = make_settings(monkeypatch, ["organize"], send_capable=True)
        mock_handler = self._mock_handler()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler),
        ):
            with pytest.raises(PermissionError, match="'draft'"):
                await save_to_mailbox(account_name="a", recipients=["x@example.com"], subject="s", body="b")
        mock_handler.save_to_mailbox.assert_not_called()
