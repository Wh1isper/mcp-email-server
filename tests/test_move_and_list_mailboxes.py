"""Tests for EmailClient.move_emails, EmailClient.list_mailboxes,
ClassicEmailHandler.move_emails, and ClassicEmailHandler.list_mailboxes.

Covers the new functionality introduced in PR #147.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aioimaplib import Response

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient
from mcp_email_server.emails.models import MailboxInfo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _make_mock_imap(**overrides):
    """Helper to build an AsyncMock IMAP client with sensible defaults."""
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock()
    mock.select = AsyncMock(return_value=("OK", []))
    mock.uid = AsyncMock(return_value=("OK", []))
    mock.expunge = AsyncMock(return_value=("OK", []))
    mock.logout = AsyncMock()
    mock.list = AsyncMock(return_value=("OK", []))
    for k, v in overrides.items():
        setattr(mock, k, v)
    return mock


# ===========================================================================
# EmailClient.move_emails
# ===========================================================================


class TestEmailClientMoveEmails:
    """Tests for the low-level EmailClient.move_emails method."""

    @pytest.mark.asyncio
    async def test_move_emails_copy_delete_fallback(self, email_client):
        """When MOVE capability is absent, should use COPY + STORE \\Deleted + EXPUNGE."""
        mock_imap = _make_mock_imap()
        # No MOVE capability
        del mock_imap.move  # ensure hasattr(imap, "move") is False

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100", "200"], "INBOX", "Archive")

        assert moved_ids == ["100", "200"]
        assert failed_ids == []

        # Verify IMAP login + select
        mock_imap.login.assert_called_once()
        mock_imap.select.assert_called_once_with('"INBOX"')

        # Verify COPY + STORE for each email
        assert mock_imap.uid.call_count == 4  # 2 copies + 2 stores
        calls = mock_imap.uid.call_args_list
        assert calls[0].args == ("copy", "100", '"Archive"')
        assert calls[1].args == ("store", "100", "+FLAGS", r"(\Deleted)")
        assert calls[2].args == ("copy", "200", '"Archive"')
        assert calls[3].args == ("store", "200", "+FLAGS", r"(\Deleted)")

        # EXPUNGE should be called because has_move is False and moved_ids is non-empty
        mock_imap.expunge.assert_called_once()
        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_move_emails_with_move_capability(self, email_client):
        """When MOVE capability is present, should use UID MOVE directly."""
        mock_imap = _make_mock_imap()
        mock_imap.move = AsyncMock()  # has "move" attribute
        mock_imap.capabilities = ("IMAP4rev1", "MOVE", "IDLE")  # MOVE in capabilities

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100"], "INBOX", "Trash")

        assert moved_ids == ["100"]
        assert failed_ids == []

        # Should use uid("move", ...) instead of copy+store
        mock_imap.uid.assert_called_once_with("move", "100", '"Trash"')
        # EXPUNGE should NOT be called when using MOVE
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_emails_with_failures(self, email_client):
        """Emails that fail to move should be collected in failed_ids."""
        mock_imap = _make_mock_imap()
        del mock_imap.move  # no MOVE capability

        # First email succeeds (copy OK, store OK), second email fails on copy
        side_effects = [
            Response("OK", [b"copied"]),  # copy "100" succeeds
            Response("OK", [b"stored"]),  # store "100" succeeds
            Exception("IMAP error"),  # copy "200" fails
        ]
        mock_imap.uid = AsyncMock(side_effect=side_effects)

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100", "200"], "INBOX", "Archive")

        assert moved_ids == ["100"]
        assert failed_ids == ["200"]
        # EXPUNGE still called because there are moved_ids
        mock_imap.expunge.assert_called_once()

    @pytest.mark.asyncio
    async def test_move_emails_all_fail_no_expunge(self, email_client):
        """When all emails fail, EXPUNGE should not be called (no moved_ids)."""
        mock_imap = _make_mock_imap()
        del mock_imap.move

        mock_imap.uid = AsyncMock(side_effect=Exception("IMAP error"))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100"], "INBOX", "Archive")

        assert moved_ids == []
        assert failed_ids == ["100"]
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_emails_logout_error_handled(self, email_client):
        """Logout errors in the finally block should not propagate."""
        mock_imap = _make_mock_imap()
        del mock_imap.move
        mock_imap.logout = AsyncMock(side_effect=Exception("Logout failed"))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100"], "INBOX", "Archive")

        # Should still return results despite logout error
        assert moved_ids == ["100"]
        assert failed_ids == []

    @pytest.mark.asyncio
    async def test_move_emails_empty_list(self, email_client):
        """Moving an empty list should work without errors."""
        mock_imap = _make_mock_imap()
        del mock_imap.move

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails([], "INBOX", "Archive")

        assert moved_ids == []
        assert failed_ids == []
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_emails_move_capability_with_failure(self, email_client):
        """Test failure when using native MOVE command."""
        mock_imap = _make_mock_imap()
        mock_imap.move = AsyncMock()
        mock_imap.capabilities = ("IMAP4rev1", "MOVE")

        mock_imap.uid = AsyncMock(side_effect=Exception("MOVE failed"))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100"], "INBOX", "Trash")

        assert moved_ids == []
        assert failed_ids == ["100"]

    @pytest.mark.asyncio
    async def test_move_emails_copy_no_response_does_not_delete_source(self, email_client):
        """A COPY NO response should fail the email before STORE \\Deleted is sent."""
        mock_imap = _make_mock_imap()
        del mock_imap.move
        mock_imap.uid = AsyncMock(return_value=Response("NO", [b"[TRYCREATE] mailbox does not exist"]))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100"], "INBOX", "Missing")

        assert moved_ids == []
        assert failed_ids == ["100"]
        mock_imap.uid.assert_called_once_with("copy", "100", '"Missing"')
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_emails_store_no_response_marks_failed(self, email_client):
        """A STORE NO response should fail the email and skip EXPUNGE."""
        mock_imap = _make_mock_imap()
        del mock_imap.move
        mock_imap.uid = AsyncMock(
            side_effect=[
                Response("OK", [b"copied"]),
                Response("NO", [b"store failed"]),
            ]
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100"], "INBOX", "Archive")

        assert moved_ids == []
        assert failed_ids == ["100"]
        assert mock_imap.uid.call_count == 2
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_emails_move_no_response_marks_failed(self, email_client):
        """A native MOVE NO response should be reported as a failed move."""
        mock_imap = _make_mock_imap()
        mock_imap.move = AsyncMock()
        mock_imap.capabilities = ("IMAP4rev1", "MOVE")
        mock_imap.uid = AsyncMock(return_value=Response("NO", [b"move failed"]))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100"], "INBOX", "Trash")

        assert moved_ids == []
        assert failed_ids == ["100"]
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_emails_select_no_response_raises(self, email_client):
        """A source mailbox SELECT NO response should stop before any move commands."""
        mock_imap = _make_mock_imap()
        del mock_imap.move
        mock_imap.select = AsyncMock(return_value=Response("NO", [b"source missing"]))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with pytest.raises(RuntimeError, match="SELECT source mailbox Missing"):
                await email_client.move_emails(["100"], "Missing", "Archive")

        mock_imap.uid.assert_not_called()
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_emails_expunge_no_response_marks_moved_ids_failed(self, email_client):
        """An EXPUNGE NO response should report copied and flagged emails as failed."""
        mock_imap = _make_mock_imap()
        del mock_imap.move
        mock_imap.uid = AsyncMock(
            side_effect=[
                Response("OK", [b"copied"]),
                Response("OK", [b"stored"]),
            ]
        )
        mock_imap.expunge = AsyncMock(return_value=Response("NO", [b"expunge failed"]))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            moved_ids, failed_ids = await email_client.move_emails(["100"], "INBOX", "Archive")

        assert moved_ids == []
        assert failed_ids == ["100"]
        mock_imap.expunge.assert_called_once()


# ===========================================================================
# EmailClient.list_mailboxes
# ===========================================================================


class TestEmailClientListMailboxes:
    """Tests for the low-level EmailClient.list_mailboxes method."""

    @pytest.mark.asyncio
    async def test_list_mailboxes_parses_standard_response(self, email_client):
        """Standard IMAP LIST responses should be parsed into MailboxInfo objects."""
        mock_imap = _make_mock_imap()
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [
                    b'(\\HasChildren) "/" "INBOX"',
                    b'(\\Sent \\HasNoChildren) "/" "Sent"',
                    b'(\\Drafts \\HasNoChildren) "/" "Drafts"',
                    b'(\\Trash \\HasNoChildren) "/" "Trash"',
                ],
            )
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes()

        assert len(result) == 4
        assert all(isinstance(m, MailboxInfo) for m in result)

        assert result[0].name == "INBOX"
        assert result[0].delimiter == "/"
        assert "\\HasChildren" in result[0].flags

        assert result[1].name == "Sent"
        assert "\\Sent" in result[1].flags
        assert "\\HasNoChildren" in result[1].flags

        assert result[2].name == "Drafts"
        assert result[3].name == "Trash"

        # Verify IMAP list was called with correct args
        mock_imap.list.assert_called_once_with('""', "*")
        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_mailboxes_with_dot_delimiter(self, email_client):
        """Mailboxes with dot delimiters (e.g., Dovecot) should parse correctly."""
        mock_imap = _make_mock_imap()
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [
                    b'(\\HasChildren) "." "INBOX"',
                    b'(\\HasNoChildren) "." "INBOX.Clients"',
                    b'(\\HasNoChildren) "." "INBOX.Projects"',
                ],
            )
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes()

        assert len(result) == 3
        assert result[0].delimiter == "."
        assert result[1].name == "INBOX.Clients"
        assert result[2].name == "INBOX.Projects"

    @pytest.mark.asyncio
    async def test_list_mailboxes_skips_empty_items(self, email_client):
        """Empty bytes items in the IMAP response should be silently skipped."""
        mock_imap = _make_mock_imap()
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    b"",
                    b'(\\HasNoChildren) "/" "Sent"',
                ],
            )
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes()

        assert len(result) == 2
        assert result[0].name == "INBOX"
        assert result[1].name == "Sent"

    @pytest.mark.asyncio
    async def test_list_mailboxes_with_pattern(self, email_client):
        """A custom pattern should be passed through to IMAP LIST."""
        mock_imap = _make_mock_imap()
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [
                    b'(\\HasNoChildren) "/" "INBOX.Sub1"',
                ],
            )
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes(pattern="INBOX.*")

        assert len(result) == 1
        mock_imap.list.assert_called_once_with('""', "INBOX.*")

    @pytest.mark.asyncio
    async def test_list_mailboxes_with_reference(self, email_client):
        """A custom reference should be quoted and passed through."""
        mock_imap = _make_mock_imap()
        mock_imap.list = AsyncMock(return_value=("OK", []))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes(reference="INBOX")

        assert result == []
        mock_imap.list.assert_called_once_with('"INBOX"', "*")

    @pytest.mark.asyncio
    async def test_list_mailboxes_empty_response(self, email_client):
        """An empty LIST response should return an empty list."""
        mock_imap = _make_mock_imap()
        mock_imap.list = AsyncMock(return_value=("OK", []))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_mailboxes_logout_error_handled(self, email_client):
        """Logout errors should not prevent the result from being returned."""
        mock_imap = _make_mock_imap()
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [b'(\\HasNoChildren) "/" "INBOX"'],
            )
        )
        mock_imap.logout = AsyncMock(side_effect=Exception("Logout failed"))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes()

        assert len(result) == 1
        assert result[0].name == "INBOX"

    @pytest.mark.asyncio
    async def test_list_mailboxes_no_flags(self, email_client):
        """Items without parenthesized flags should produce an empty flags list."""
        mock_imap = _make_mock_imap()
        # Unusual but valid: no flags section
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [b'"/" "SomeFolder"'],
            )
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes()

        # Should still parse delimiter and name from the quoted parts
        assert len(result) == 1
        assert result[0].name == "SomeFolder"
        assert result[0].delimiter == "/"
        assert result[0].flags == []

    @pytest.mark.asyncio
    async def test_list_mailboxes_string_item(self, email_client):
        """Non-bytes items in the response should be handled via str()."""
        mock_imap = _make_mock_imap()
        # Some IMAP libs may return strings instead of bytes
        mock_imap.list = AsyncMock(
            return_value=(
                "OK",
                [
                    '(\\HasNoChildren) "/" "Junk"',  # already a str
                ],
            )
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.list_mailboxes()

        assert len(result) == 1
        assert result[0].name == "Junk"
        assert "\\HasNoChildren" in result[0].flags

    @pytest.mark.asyncio
    async def test_list_mailboxes_no_response_raises(self, email_client):
        """A LIST NO response should raise a clear error."""
        mock_imap = _make_mock_imap()
        mock_imap.list = AsyncMock(return_value=Response("NO", [b"LIST failed"]))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with pytest.raises(RuntimeError, match="LIST mailboxes with pattern"):
                await email_client.list_mailboxes()

        mock_imap.logout.assert_called_once()


# ===========================================================================
# ClassicEmailHandler.move_emails / list_mailboxes (delegation tests)
# ===========================================================================


class TestClassicHandlerMoveEmails:
    """Tests for ClassicEmailHandler.move_emails delegation."""

    @pytest.mark.asyncio
    async def test_move_emails_delegates(self, classic_handler):
        """ClassicEmailHandler.move_emails should delegate to incoming_client."""
        mock_move = AsyncMock(return_value=(["100", "200"], []))

        with patch.object(classic_handler.incoming_client, "move_emails", mock_move):
            moved, failed = await classic_handler.move_emails(
                email_ids=["100", "200"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

        assert moved == ["100", "200"]
        assert failed == []
        mock_move.assert_called_once_with(["100", "200"], "INBOX", "Archive")

    @pytest.mark.asyncio
    async def test_move_emails_with_failures(self, classic_handler):
        """Partial failures should be propagated correctly."""
        mock_move = AsyncMock(return_value=(["100"], ["200"]))

        with patch.object(classic_handler.incoming_client, "move_emails", mock_move):
            moved, failed = await classic_handler.move_emails(
                email_ids=["100", "200"],
                source_mailbox="INBOX",
                destination_mailbox="Trash",
            )

        assert moved == ["100"]
        assert failed == ["200"]
        mock_move.assert_called_once_with(["100", "200"], "INBOX", "Trash")

    @pytest.mark.asyncio
    async def test_move_emails_custom_source(self, classic_handler):
        """A custom source mailbox should be passed through."""
        mock_move = AsyncMock(return_value=(["300"], []))

        with patch.object(classic_handler.incoming_client, "move_emails", mock_move):
            moved, _failed = await classic_handler.move_emails(
                email_ids=["300"],
                source_mailbox="Trash",
                destination_mailbox="INBOX",
            )

        assert moved == ["300"]
        mock_move.assert_called_once_with(["300"], "Trash", "INBOX")


class TestClassicHandlerListMailboxes:
    """Tests for ClassicEmailHandler.list_mailboxes delegation."""

    @pytest.mark.asyncio
    async def test_list_mailboxes_delegates(self, classic_handler):
        """ClassicEmailHandler.list_mailboxes should delegate to incoming_client."""
        expected = [
            MailboxInfo(name="INBOX", delimiter="/", flags=["\\HasChildren"]),
            MailboxInfo(name="Sent", delimiter="/", flags=["\\Sent"]),
        ]
        mock_list = AsyncMock(return_value=expected)

        with patch.object(classic_handler.incoming_client, "list_mailboxes", mock_list):
            result = await classic_handler.list_mailboxes()

        assert result == expected
        mock_list.assert_called_once_with("*", "")

    @pytest.mark.asyncio
    async def test_list_mailboxes_with_pattern(self, classic_handler):
        """Custom pattern should be forwarded."""
        expected = [MailboxInfo(name="INBOX.Sub", delimiter=".", flags=[])]
        mock_list = AsyncMock(return_value=expected)

        with patch.object(classic_handler.incoming_client, "list_mailboxes", mock_list):
            result = await classic_handler.list_mailboxes(pattern="INBOX.*")

        assert result == expected
        mock_list.assert_called_once_with("INBOX.*", "")

    @pytest.mark.asyncio
    async def test_list_mailboxes_with_reference(self, classic_handler):
        """Custom reference should be forwarded."""
        mock_list = AsyncMock(return_value=[])

        with patch.object(classic_handler.incoming_client, "list_mailboxes", mock_list):
            result = await classic_handler.list_mailboxes(pattern="*", reference="ns")

        assert result == []
        mock_list.assert_called_once_with("*", "ns")

    @pytest.mark.asyncio
    async def test_list_mailboxes_empty(self, classic_handler):
        """An account with no discoverable mailboxes should return an empty list."""
        mock_list = AsyncMock(return_value=[])

        with patch.object(classic_handler.incoming_client, "list_mailboxes", mock_list):
            result = await classic_handler.list_mailboxes()

        assert result == []
