"""Tests for the Scher Extensions: mark_seen, mark_unseen, ensure_folder.

Uses example.com / fake credentials throughout — never real production data.
The diag tool is tested separately in tests/test_diag.py since it spans
networking, DNS, IMAP, and SMTP.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aioimaplib import Response

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient


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
    """AsyncMock IMAP client with sensible defaults."""
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock()
    mock.select = AsyncMock(return_value=("OK", []))
    mock.uid = AsyncMock(return_value=("OK", []))
    mock.create = AsyncMock(return_value=("OK", []))
    mock.list = AsyncMock(return_value=("OK", []))
    mock.logout = AsyncMock()
    for k, v in overrides.items():
        setattr(mock, k, v)
    return mock


# ===========================================================================
# EmailClient.mark_seen / mark_unseen
# ===========================================================================


class TestMarkSeenUnseen:
    """Tests for the low-level EmailClient flag operations."""

    @pytest.mark.asyncio
    async def test_mark_seen_happy_path(self, email_client):
        """A successful STORE +FLAGS (\\Seen) returns the UIDs and never expunges."""
        mock_imap = _make_mock_imap()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            ok, failed = await email_client.mark_seen(["100", "200"], "INBOX")

        assert ok == ["100", "200"]
        assert failed == []

        mock_imap.login.assert_called_once()
        mock_imap.select.assert_called_once_with('"INBOX"')
        assert mock_imap.uid.call_count == 2
        assert mock_imap.uid.call_args_list[0].args == ("store", "100", "+FLAGS", r"(\Seen)")
        assert mock_imap.uid.call_args_list[1].args == ("store", "200", "+FLAGS", r"(\Seen)")
        # Never expunge — that would lose data on a triage path.
        assert not hasattr(mock_imap, "expunge") or not mock_imap.expunge.called

    @pytest.mark.asyncio
    async def test_mark_unseen_uses_minus_flags(self, email_client):
        """mark_unseen sends ``-FLAGS`` to drop the \\Seen bit."""
        mock_imap = _make_mock_imap()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            ok, failed = await email_client.mark_unseen(["300"], "INBOX")

        assert ok == ["300"]
        assert failed == []
        mock_imap.uid.assert_called_once_with("store", "300", "-FLAGS", r"(\Seen)")

    @pytest.mark.asyncio
    async def test_mark_seen_per_uid_failure_isolated(self, email_client):
        """A failure on one UID should not poison the others."""
        mock_imap = _make_mock_imap()
        mock_imap.uid = AsyncMock(
            side_effect=[
                Response("OK", [b"stored"]),
                Exception("transient error"),
                Response("OK", [b"stored"]),
            ]
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            ok, failed = await email_client.mark_seen(["1", "2", "3"], "INBOX")

        assert ok == ["1", "3"]
        assert failed == ["2"]

    @pytest.mark.asyncio
    async def test_mark_seen_no_response_marks_failed(self, email_client):
        """A STORE NO response is treated as a failure, not as success."""
        mock_imap = _make_mock_imap()
        mock_imap.uid = AsyncMock(return_value=Response("NO", [b"can't update"]))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            ok, failed = await email_client.mark_seen(["100"], "INBOX")

        assert ok == []
        assert failed == ["100"]

    @pytest.mark.asyncio
    async def test_mark_seen_empty_list(self, email_client):
        """Empty input → empty output, no IMAP traffic for STORE."""
        mock_imap = _make_mock_imap()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            ok, failed = await email_client.mark_seen([], "INBOX")

        assert ok == []
        assert failed == []
        assert mock_imap.uid.call_count == 0

    @pytest.mark.asyncio
    async def test_mark_seen_select_failure_raises(self, email_client):
        """A SELECT NO response should abort before any STORE is sent."""
        mock_imap = _make_mock_imap()
        mock_imap.select = AsyncMock(return_value=Response("NO", [b"no such folder"]))

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with pytest.raises(RuntimeError, match="SELECT mailbox Missing"):
                await email_client.mark_seen(["100"], "Missing")

        mock_imap.uid.assert_not_called()


# ===========================================================================
# EmailClient.ensure_folder
# ===========================================================================


class TestEnsureFolder:
    """Tests for the idempotent CREATE + LIST verify helper."""

    @pytest.mark.asyncio
    async def test_create_new_folder(self, email_client):
        """When CREATE returns OK, the folder is newly created and LIST confirms it."""
        mock_imap = _make_mock_imap(
            create=AsyncMock(return_value=Response("OK", [b"created"])),
            list=AsyncMock(return_value=("OK", [b'(\\HasNoChildren) "/" "INBOX/Archive"'])),
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.ensure_folder("INBOX/Archive")

        assert result == {
            "folder": "INBOX/Archive",
            "existed": False,
            "created": True,
            "found": True,
        }
        mock_imap.create.assert_called_once_with('"INBOX/Archive"')
        mock_imap.list.assert_called_once_with('""', "INBOX/Archive")

    @pytest.mark.asyncio
    async def test_already_exists_is_success(self, email_client):
        """A NO ALREADYEXISTS response means the folder pre-existed — that's fine."""
        mock_imap = _make_mock_imap(
            create=AsyncMock(return_value=Response("NO", [b"[ALREADYEXISTS] Mailbox exists"])),
            list=AsyncMock(return_value=("OK", [b'(\\HasNoChildren) "/" "INBOX/Archive"'])),
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.ensure_folder("INBOX/Archive")

        assert result["existed"] is True
        assert result["created"] is False
        assert result["found"] is True

    @pytest.mark.asyncio
    async def test_create_no_response_recovered_via_list(self, email_client):
        """Some servers return NO without ALREADYEXISTS; LIST must still find the folder."""
        mock_imap = _make_mock_imap(
            create=AsyncMock(return_value=Response("NO", [b"some weird denial"])),
            list=AsyncMock(return_value=("OK", [b'(\\HasNoChildren) "/" "INBOX/Pending"'])),
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.ensure_folder("INBOX/Pending")

        # CREATE NO but LIST found it → treat as existed.
        assert result["existed"] is True
        assert result["created"] is False
        assert result["found"] is True

    @pytest.mark.asyncio
    async def test_create_succeeds_but_list_does_not_find(self, email_client):
        """If LIST cannot find the folder we still return what we know."""
        mock_imap = _make_mock_imap(
            create=AsyncMock(return_value=Response("OK", [b"created"])),
            list=AsyncMock(return_value=("OK", [])),
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.ensure_folder("INBOX/Future")

        assert result["created"] is True
        assert result["found"] is False
        assert result["existed"] is False

    @pytest.mark.asyncio
    async def test_create_exception_does_not_raise(self, email_client):
        """An exception during CREATE should be logged and recovery attempted via LIST."""
        mock_imap = _make_mock_imap(
            create=AsyncMock(side_effect=Exception("BAD: command parse")),
            list=AsyncMock(return_value=("OK", [b'(\\HasNoChildren) "/" "INBOX/Existing"'])),
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.ensure_folder("INBOX/Existing")

        # CREATE failed but folder is there via LIST → existed=True, found=True.
        assert result["found"] is True
        assert result["existed"] is True
        assert result["created"] is False

    @pytest.mark.asyncio
    async def test_verify_handles_dot_delimiter_server(self, email_client):
        """A server using '.' as delimiter should still satisfy a caller using '/'.

        Caller asks for ``INBOX/Archive``. Server (e.g. Dovecot, IONOS) returns
        ``INBOX.Archive`` with delimiter ``.``. The verify step must accept
        this as the same folder.
        """
        mock_imap = _make_mock_imap(
            create=AsyncMock(return_value=Response("OK", [b"created"])),
            list=AsyncMock(return_value=("OK", [b'(\\HasNoChildren) "." "INBOX.Archive"'])),
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.ensure_folder("INBOX/Archive")

        assert result["found"] is True
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_verify_inverse_delimiter_match(self, email_client):
        """Caller passing dot-style ``INBOX.Sub`` matches server using ``/``.

        Symmetric to the above: makes the verify normalization actually
        commutative between the two common delimiter conventions.
        """
        mock_imap = _make_mock_imap(
            create=AsyncMock(return_value=Response("OK", [b"created"])),
            list=AsyncMock(return_value=("OK", [b'(\\HasNoChildren) "/" "INBOX/Sub"'])),
        )

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            result = await email_client.ensure_folder("INBOX.Sub")

        assert result["found"] is True


# ===========================================================================
# ClassicEmailHandler delegation
# ===========================================================================


class TestHandlerDelegation:
    """ClassicEmailHandler should forward all three new operations to incoming_client."""

    @pytest.mark.asyncio
    async def test_mark_seen_delegates(self, classic_handler):
        mock = AsyncMock(return_value=(["1", "2"], []))
        with patch.object(classic_handler.incoming_client, "mark_seen", mock):
            ok, failed = await classic_handler.mark_seen(["1", "2"], "INBOX")
        assert ok == ["1", "2"]
        assert failed == []
        mock.assert_called_once_with(["1", "2"], "INBOX")

    @pytest.mark.asyncio
    async def test_mark_unseen_delegates(self, classic_handler):
        mock = AsyncMock(return_value=(["7"], ["8"]))
        with patch.object(classic_handler.incoming_client, "mark_unseen", mock):
            ok, failed = await classic_handler.mark_unseen(["7", "8"], "INBOX/Sub")
        assert ok == ["7"]
        assert failed == ["8"]
        mock.assert_called_once_with(["7", "8"], "INBOX/Sub")

    @pytest.mark.asyncio
    async def test_ensure_folder_delegates(self, classic_handler):
        mock = AsyncMock(return_value={"folder": "INBOX/X", "existed": True, "created": False, "found": True})
        with patch.object(classic_handler.incoming_client, "ensure_folder", mock):
            result = await classic_handler.ensure_folder("INBOX/X")
        assert result["folder"] == "INBOX/X"
        assert result["existed"] is True
        mock.assert_called_once_with("INBOX/X")
