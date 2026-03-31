"""Tests for folder management functionality."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.app import (
    copy_emails,
    create_folder,
    delete_folder,
    list_folders,
    move_emails,
    rename_folder,
)
from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient
from mcp_email_server.emails.models import (
    EmailMoveResponse,
    Folder,
    FolderListResponse,
    FolderOperationResponse,
)

# ============================================================================
# MCP Tool Tests - Permission Checks
# ============================================================================


class TestFolderManagementDisabled:
    """Test that folder management tools raise PermissionError when disabled."""

    @pytest.mark.asyncio
    async def test_list_folders_disabled(self):
        """Test list_folders raises PermissionError when disabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(PermissionError) as exc_info:
                await list_folders(account_name="test_account")

            assert "Folder management is disabled" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_move_emails_disabled(self):
        """Test move_emails raises PermissionError when disabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(PermissionError) as exc_info:
                await move_emails(
                    account_name="test_account",
                    email_ids=["123"],
                    destination_folder="Archive",
                )

            assert "Folder management is disabled" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_copy_emails_disabled(self):
        """Test copy_emails raises PermissionError when disabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(PermissionError) as exc_info:
                await copy_emails(
                    account_name="test_account",
                    email_ids=["123"],
                    destination_folder="Archive",
                )

            assert "Folder management is disabled" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_folder_disabled(self):
        """Test create_folder raises PermissionError when disabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(PermissionError) as exc_info:
                await create_folder(account_name="test_account", folder_name="NewFolder")

            assert "Folder management is disabled" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_delete_folder_disabled(self):
        """Test delete_folder raises PermissionError when disabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(PermissionError) as exc_info:
                await delete_folder(account_name="test_account", folder_name="OldFolder")

            assert "Folder management is disabled" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_rename_folder_disabled(self):
        """Test rename_folder raises PermissionError when disabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(PermissionError) as exc_info:
                await rename_folder(
                    account_name="test_account",
                    old_name="OldName",
                    new_name="NewName",
                )

            assert "Folder management is disabled" in str(exc_info.value)


class TestFolderManagementEnabled:
    """Test that folder management tools work when enabled."""

    @pytest.mark.asyncio
    async def test_list_folders_enabled(self):
        """Test list_folders works when enabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = True

        folder_response = FolderListResponse(
            folders=[
                Folder(name="INBOX", delimiter="/", flags=["\\HasNoChildren"]),
                Folder(name="Sent", delimiter="/", flags=["\\HasNoChildren", "\\Sent"]),
                Folder(name="Archive", delimiter="/", flags=["\\HasNoChildren"]),
            ],
            total=3,
        )

        mock_handler = AsyncMock()
        mock_handler.list_folders.return_value = folder_response

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await list_folders(account_name="test_account")

                assert result == folder_response
                assert len(result.folders) == 3
                assert result.folders[0].name == "INBOX"
                mock_handler.list_folders.assert_called_once()

    @pytest.mark.asyncio
    async def test_move_emails_enabled(self):
        """Test move_emails works when enabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = True

        move_response = EmailMoveResponse(
            success=True,
            moved_ids=["123", "456"],
            failed_ids=[],
            source_mailbox="INBOX",
            destination_folder="Archive",
        )

        mock_handler = AsyncMock()
        mock_handler.move_emails.return_value = move_response

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await move_emails(
                    account_name="test_account",
                    email_ids=["123", "456"],
                    destination_folder="Archive",
                )

                assert result == move_response
                assert result.success is True
                assert result.moved_ids == ["123", "456"]
                mock_handler.move_emails.assert_called_once_with(["123", "456"], "Archive", "INBOX")

    @pytest.mark.asyncio
    async def test_copy_emails_enabled(self):
        """Test copy_emails works when enabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = True

        copy_response = EmailMoveResponse(
            success=True,
            moved_ids=["123"],
            failed_ids=[],
            source_mailbox="INBOX",
            destination_folder="Labels/Important",
        )

        mock_handler = AsyncMock()
        mock_handler.copy_emails.return_value = copy_response

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await copy_emails(
                    account_name="test_account",
                    email_ids=["123"],
                    destination_folder="Labels/Important",
                )

                assert result == copy_response
                assert result.success is True
                mock_handler.copy_emails.assert_called_once_with(["123"], "Labels/Important", "INBOX")

    @pytest.mark.asyncio
    async def test_create_folder_enabled(self):
        """Test create_folder works when enabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = True

        create_response = FolderOperationResponse(
            success=True,
            folder_name="Projects/2024",
            message="Folder 'Projects/2024' created successfully",
        )

        mock_handler = AsyncMock()
        mock_handler.create_folder.return_value = create_response

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await create_folder(
                    account_name="test_account",
                    folder_name="Projects/2024",
                )

                assert result == create_response
                assert result.success is True
                mock_handler.create_folder.assert_called_once_with("Projects/2024")

    @pytest.mark.asyncio
    async def test_delete_folder_enabled(self):
        """Test delete_folder works when enabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = True

        delete_response = FolderOperationResponse(
            success=True,
            folder_name="OldFolder",
            message="Folder 'OldFolder' deleted successfully",
        )

        mock_handler = AsyncMock()
        mock_handler.delete_folder.return_value = delete_response

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await delete_folder(
                    account_name="test_account",
                    folder_name="OldFolder",
                )

                assert result == delete_response
                assert result.success is True
                mock_handler.delete_folder.assert_called_once_with("OldFolder")

    @pytest.mark.asyncio
    async def test_rename_folder_enabled(self):
        """Test rename_folder works when enabled."""
        mock_settings = MagicMock()
        mock_settings.enable_folder_management = True

        rename_response = FolderOperationResponse(
            success=True,
            folder_name="NewName",
            message="Folder renamed from 'OldName' to 'NewName'",
        )

        mock_handler = AsyncMock()
        mock_handler.rename_folder.return_value = rename_response

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await rename_folder(
                    account_name="test_account",
                    old_name="OldName",
                    new_name="NewName",
                )

                assert result == rename_response
                assert result.success is True
                mock_handler.rename_folder.assert_called_once_with("OldName", "NewName")


# ============================================================================
# Handler Tests
# ============================================================================


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


class TestClassicEmailHandlerFolders:
    """Test ClassicEmailHandler folder operations."""

    @pytest.mark.asyncio
    async def test_list_folders(self, classic_handler):
        """Test list_folders handler method."""
        mock_folders = [
            Folder(name="INBOX", delimiter="/", flags=["\\HasNoChildren"]),
            Folder(name="Sent", delimiter="/", flags=["\\Sent"]),
        ]

        mock_list = AsyncMock(return_value=mock_folders)

        with patch.object(classic_handler.incoming_client, "list_folders", mock_list):
            result = await classic_handler.list_folders()

            assert isinstance(result, FolderListResponse)
            assert len(result.folders) == 2
            assert result.total == 2
            mock_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_move_emails(self, classic_handler):
        """Test move_emails handler method."""
        mock_move = AsyncMock(return_value=(["123"], []))

        with patch.object(classic_handler.incoming_client, "move_emails", mock_move):
            result = await classic_handler.move_emails(
                email_ids=["123"],
                destination_folder="Archive",
                source_mailbox="INBOX",
            )

            assert isinstance(result, EmailMoveResponse)
            assert result.success is True
            assert result.moved_ids == ["123"]
            assert result.failed_ids == []
            mock_move.assert_called_once_with(["123"], "Archive", "INBOX")

    @pytest.mark.asyncio
    async def test_copy_emails(self, classic_handler):
        """Test copy_emails handler method."""
        mock_copy = AsyncMock(return_value=(["123"], []))

        with patch.object(classic_handler.incoming_client, "copy_emails", mock_copy):
            result = await classic_handler.copy_emails(
                email_ids=["123"],
                destination_folder="Backup",
                source_mailbox="INBOX",
            )

            assert isinstance(result, EmailMoveResponse)
            assert result.success is True
            assert result.moved_ids == ["123"]
            mock_copy.assert_called_once_with(["123"], "Backup", "INBOX")

    @pytest.mark.asyncio
    async def test_create_folder(self, classic_handler):
        """Test create_folder handler method."""
        mock_create = AsyncMock(return_value=(True, "Folder created"))

        with patch.object(classic_handler.incoming_client, "create_folder", mock_create):
            result = await classic_handler.create_folder("NewFolder")

            assert isinstance(result, FolderOperationResponse)
            assert result.success is True
            assert result.folder_name == "NewFolder"
            mock_create.assert_called_once_with("NewFolder")

    @pytest.mark.asyncio
    async def test_delete_folder(self, classic_handler):
        """Test delete_folder handler method."""
        mock_delete = AsyncMock(return_value=(True, "Folder deleted"))

        with patch.object(classic_handler.incoming_client, "delete_folder", mock_delete):
            result = await classic_handler.delete_folder("OldFolder")

            assert isinstance(result, FolderOperationResponse)
            assert result.success is True
            assert result.folder_name == "OldFolder"
            mock_delete.assert_called_once_with("OldFolder")

    @pytest.mark.asyncio
    async def test_rename_folder(self, classic_handler):
        """Test rename_folder handler method."""
        mock_rename = AsyncMock(return_value=(True, "Folder renamed"))

        with patch.object(classic_handler.incoming_client, "rename_folder", mock_rename):
            result = await classic_handler.rename_folder("OldName", "NewName")

            assert isinstance(result, FolderOperationResponse)
            assert result.success is True
            assert result.folder_name == "NewName"
            mock_rename.assert_called_once_with("OldName", "NewName")


# ============================================================================
# EmailClient Tests
# ============================================================================


@pytest.fixture
def email_server():
    return EmailServer(
        user_name="test_user",
        password="test_password",
        host="imap.example.com",
        port=993,
        use_ssl=True,
    )


@pytest.fixture
def email_client(email_server):
    return EmailClient(email_server, sender="Test User <test@example.com>")


class TestEmailClientFolders:
    """Test EmailClient folder operations."""

    @pytest.mark.asyncio
    async def test_list_folders(self, email_client):
        """Test list_folders IMAP operation."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    b'(\\HasNoChildren \\Sent) "/" "Sent"',
                    b'(\\HasChildren) "/" "Folders"',
                ],
            )
        )
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            result = await email_client.list_folders()

            assert isinstance(result, list)
            assert len(result) == 3
            assert result[0].name == "INBOX"
            assert result[1].name == "Sent"
            assert "\\Sent" in result[1].flags
            mock_imap.list.assert_called_once_with('""', "*")

    @pytest.mark.asyncio
    async def test_copy_emails(self, email_client):
        """Test copy_emails IMAP operation."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock()
        mock_imap.uid = AsyncMock(return_value=("OK", [b"[COPYUID 1234 1:2 100:101]"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            copied_ids, failed_ids = await email_client.copy_emails(["123", "456"], "Archive", "INBOX")

            assert copied_ids == ["123", "456"]
            assert failed_ids == []
            mock_imap.select.assert_called_once_with('"INBOX"')

    @pytest.mark.asyncio
    async def test_move_emails_with_move_command(self, email_client):
        """Test move_emails using MOVE command."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock()
        mock_imap.uid = AsyncMock(return_value=("OK", [b"OK"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["123"], "Archive", "INBOX")

            assert moved_ids == ["123"]
            assert failed_ids == []

    @pytest.mark.asyncio
    async def test_create_folder(self, email_client):
        """Test create_folder IMAP operation."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.create = AsyncMock(return_value=("OK", [b"CREATE completed"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            success, message = await email_client.create_folder("NewFolder")

            assert success is True
            assert "NewFolder" in message
            mock_imap.create.assert_called_once_with('"NewFolder"')

    @pytest.mark.asyncio
    async def test_delete_folder(self, email_client):
        """Test delete_folder IMAP operation."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.delete = AsyncMock(return_value=("OK", [b"DELETE completed"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            success, message = await email_client.delete_folder("OldFolder")

            assert success is True
            assert "OldFolder" in message
            mock_imap.delete.assert_called_once_with('"OldFolder"')

    @pytest.mark.asyncio
    async def test_rename_folder(self, email_client):
        """Test rename_folder IMAP operation."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.rename = AsyncMock(return_value=("OK", [b"RENAME completed"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            success, _message = await email_client.rename_folder("OldName", "NewName")

            assert success is True
            mock_imap.rename.assert_called_once_with('"OldName"', '"NewName"')


class TestEmailClientFolderEdgeCases:
    """Test edge cases for EmailClient folder operations."""

    @pytest.mark.asyncio
    async def test_copy_emails_partial_failure(self, email_client):
        """Test copy_emails with partial failures."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock()
        mock_imap.uid = AsyncMock(side_effect=[("OK", []), ("NO", [b"Message not found"])])
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            copied_ids, failed_ids = await email_client.copy_emails(["123", "456"], "Archive", "INBOX")

            assert copied_ids == ["123"]
            assert failed_ids == ["456"]

    @pytest.mark.asyncio
    async def test_create_folder_failure(self, email_client):
        """Test create_folder when it fails."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.create = AsyncMock(return_value=("NO", [b"Folder already exists"]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            success, _message = await email_client.create_folder("ExistingFolder")

            assert success is False

    @pytest.mark.asyncio
    async def test_list_folders_special_characters(self, email_client):
        """Test list_folders with special characters in folder names."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    b'(\\HasNoChildren) "/" "Folders/My Folder"',
                    b'(\\HasNoChildren) "/" "[Gmail]/Sent Mail"',
                ],
            )
        )
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            result = await email_client.list_folders()

            assert len(result) == 3
            assert result[1].name == "Folders/My Folder"
            assert result[2].name == "[Gmail]/Sent Mail"

    @pytest.mark.asyncio
    async def test_move_emails_move_returns_non_ok(self, email_client):
        """Test move_emails fallback when MOVE returns non-OK status."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock()
        mock_imap.expunge = AsyncMock()
        mock_imap.logout = AsyncMock()

        # MOVE returns non-OK status (not exception), then COPY succeeds
        mock_imap.uid = AsyncMock(
            side_effect=[
                ("NO", [b"MOVE not supported"]),  # MOVE returns NO status
                ("OK", [b"[COPYUID 1234 1 100]"]),  # COPY succeeds
                ("OK", []),  # STORE \\Deleted succeeds
            ]
        )

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["123"], "Archive", "INBOX")

            assert moved_ids == ["123"]
            assert failed_ids == []
            # Should have called uid 3 times: move (NO), copy, store
            assert mock_imap.uid.call_count == 3
            mock_imap.expunge.assert_called_once()

    @pytest.mark.asyncio
    async def test_move_emails_fallback_to_copy_delete(self, email_client):
        """Test move_emails falls back to COPY+DELETE when MOVE fails."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock()
        mock_imap.select = AsyncMock()
        mock_imap.expunge = AsyncMock()
        mock_imap.logout = AsyncMock()

        # First call (MOVE) fails with exception, second call (COPY) succeeds
        mock_imap.uid = AsyncMock(
            side_effect=[
                Exception("MOVE not supported"),  # MOVE fails
                ("OK", [b"[COPYUID 1234 1 100]"]),  # COPY succeeds
                ("OK", []),  # STORE \\Deleted succeeds
            ]
        )

        with patch.object(email_client, "_imap_connect", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["123"], "Archive", "INBOX")

            assert moved_ids == ["123"]
            assert failed_ids == []
            # Should have called uid 3 times: move, copy, store
            assert mock_imap.uid.call_count == 3
