"""Sender-allowlist enforcement on the UID mutation tools."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.emails.classic import EmailClient


@pytest.fixture
def email_client(email_server):  # email_server comes from conftest.py
    return EmailClient(email_server)


def _make_mock_imap(**overrides):
    """AsyncMock IMAP client with sensible mutation defaults (uid/expunge return OK)."""
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    mock.select = AsyncMock(return_value=("OK", []))
    mock.uid = AsyncMock(return_value=("OK", []))
    mock.expunge = AsyncMock(return_value=("OK", []))
    mock.logout = AsyncMock()
    for k, v in overrides.items():
        setattr(mock, k, v)
    return mock


def _uid_op_targets(mock_imap, op):
    """UIDs that a given uid(op, uid, ...) command was issued for."""
    return [c.args[1] for c in mock_imap.uid.call_args_list if c.args and c.args[0] == op]


class TestDeleteEmailsAllowlist:
    @pytest.mark.asyncio
    async def test_blocked_uid_not_deleted_default_silent(self, email_client):
        mock_imap = _make_mock_imap()
        senders = {"1": "ok@allowed.com", "2": "evil@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            deleted, failed = await email_client.delete_emails(["1", "2"], allowed_senders=["*@allowed.com"])
        # default: blocked "2" is a no-op success (never flagged), allowed "1" deleted
        assert deleted == ["1", "2"]
        assert failed == []
        assert _uid_op_targets(mock_imap, "store") == ["1"]  # blocked UID never STOREd \Deleted

    @pytest.mark.asyncio
    async def test_blocked_uid_reported_when_configured(self, email_client):
        mock_imap = _make_mock_imap()
        senders = {"1": "ok@allowed.com", "2": "evil@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            deleted, failed = await email_client.delete_emails(
                ["1", "2"], allowed_senders=["*@allowed.com"], report_blocked_mutations=True
            )
        assert deleted == ["1"]
        assert failed == ["2"]
        assert _uid_op_targets(mock_imap, "store") == ["1"]

    @pytest.mark.asyncio
    async def test_empty_allowlist_no_sender_fetch(self, email_client):
        mock_imap = _make_mock_imap()
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock()) as mock_senders,
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            deleted, failed = await email_client.delete_emails(["1", "2"], allowed_senders=[])
        assert deleted == ["1", "2"]
        assert failed == []
        mock_senders.assert_not_called()  # no allowlist => no extra IMAP work

    @pytest.mark.asyncio
    async def test_all_blocked_no_store_no_expunge(self, email_client):
        mock_imap = _make_mock_imap()
        senders = {"1": "evil@blocked.com", "2": "spam@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            deleted, failed = await email_client.delete_emails(["1", "2"], allowed_senders=["*@allowed.com"])
        assert deleted == ["1", "2"]  # silent no-op success
        assert failed == []
        assert _uid_op_targets(mock_imap, "store") == []  # no STORE issued
        mock_imap.expunge.assert_not_called()  # and crucially, no EXPUNGE

    @pytest.mark.asyncio
    async def test_sender_fetch_failure_is_not_reported_as_silent_success(self, email_client):
        mock_imap = _make_mock_imap()

        async def uid_side_effect(command, *args):
            if command == "fetch":
                return "NO", [b"FETCH failed"]
            return "OK", []

        mock_imap.uid = AsyncMock(side_effect=uid_side_effect)
        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with pytest.raises(RuntimeError, match="FETCH From headers for UIDs 1,2 failed"):
                await email_client.delete_emails(["1", "2"], allowed_senders=["*@allowed.com"])

        assert _uid_op_targets(mock_imap, "store") == []
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_expunge_failure_reports_delete_failure(self, email_client):
        mock_imap = _make_mock_imap(expunge=AsyncMock(return_value=("NO", [b"EXPUNGE failed"])))
        with patch.object(email_client, "imap_class", return_value=mock_imap):
            deleted, failed = await email_client.delete_emails(["1", "2"], allowed_senders=[])

        assert deleted == []
        assert failed == ["1", "2"]
        assert _uid_op_targets(mock_imap, "store") == ["1", "2"]
        mock_imap.expunge.assert_called_once()


class TestMarkAsReadAllowlist:
    @pytest.mark.asyncio
    async def test_blocked_uid_not_marked_default_silent(self, email_client):
        mock_imap = _make_mock_imap()
        senders = {"1": "ok@allowed.com", "2": "evil@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            marked, failed = await email_client.mark_emails_as_read(["1", "2"], allowed_senders=["*@allowed.com"])
        assert marked == ["1", "2"]
        assert failed == []
        assert _uid_op_targets(mock_imap, "store") == ["1"]  # blocked UID never STOREd \Seen

    @pytest.mark.asyncio
    async def test_blocked_uid_reported_when_configured(self, email_client):
        mock_imap = _make_mock_imap()
        senders = {"1": "ok@allowed.com", "2": "evil@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            marked, failed = await email_client.mark_emails_as_read(
                ["1", "2"], allowed_senders=["*@allowed.com"], report_blocked_mutations=True
            )
        assert marked == ["1"]
        assert failed == ["2"]
        assert _uid_op_targets(mock_imap, "store") == ["1"]


class TestMoveEmailsAllowlist:
    @pytest.mark.asyncio
    async def test_blocked_uid_not_moved_default_silent(self, email_client):
        mock_imap = _make_mock_imap(move=AsyncMock(return_value=("OK", [])), capabilities=("IMAP4rev1", "MOVE"))
        senders = {"1": "ok@allowed.com", "2": "evil@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            moved, failed = await email_client.move_emails(
                ["1", "2"], "INBOX", "Archive", allowed_senders=["*@allowed.com"]
            )
        assert moved == ["1", "2"]
        assert failed == []
        assert _uid_op_targets(mock_imap, "move") == ["1"]  # blocked UID never MOVEd

    @pytest.mark.asyncio
    async def test_blocked_uid_reported_when_configured(self, email_client):
        mock_imap = _make_mock_imap(move=AsyncMock(return_value=("OK", [])), capabilities=("IMAP4rev1", "MOVE"))
        senders = {"1": "ok@allowed.com", "2": "evil@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            moved, failed = await email_client.move_emails(
                ["1", "2"], "INBOX", "Archive", allowed_senders=["*@allowed.com"], report_blocked_mutations=True
            )
        assert moved == ["1"]
        assert failed == ["2"]
        assert _uid_op_targets(mock_imap, "move") == ["1"]

    @pytest.mark.asyncio
    async def test_blocked_uid_not_copied_on_fallback_default_silent(self, email_client):
        # No MOVE capability -> COPY + STORE \Deleted fallback path
        mock_imap = _make_mock_imap(capabilities=("IMAP4rev1",))
        senders = {"1": "ok@allowed.com", "2": "evil@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            moved, failed = await email_client.move_emails(
                ["1", "2"], "INBOX", "Archive", allowed_senders=["*@allowed.com"]
            )
        assert moved == ["1", "2"]  # blocked "2" is a silent no-op success
        assert failed == []
        assert _uid_op_targets(mock_imap, "copy") == ["1"]  # blocked UID never COPYed
        assert _uid_op_targets(mock_imap, "store") == ["1"]  # blocked UID never STOREd \Deleted

    @pytest.mark.asyncio
    async def test_all_blocked_fallback_no_copy_no_store_no_expunge(self, email_client):
        mock_imap = _make_mock_imap(capabilities=("IMAP4rev1",))  # no MOVE -> COPY+STORE fallback
        senders = {"1": "evil@blocked.com", "2": "spam@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            moved, failed = await email_client.move_emails(
                ["1", "2"], "INBOX", "Archive", allowed_senders=["*@allowed.com"]
            )
        assert moved == ["1", "2"]
        assert failed == []
        assert _uid_op_targets(mock_imap, "copy") == []
        assert _uid_op_targets(mock_imap, "store") == []
        mock_imap.expunge.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_fallback_expunge_only_from_allowed_work(self, email_client):
        mock_imap = _make_mock_imap(capabilities=("IMAP4rev1",))  # fallback path
        senders = {"1": "ok@allowed.com", "2": "evil@blocked.com"}
        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value=senders)),
            patch.object(email_client, "imap_class", return_value=mock_imap),
        ):
            moved, failed = await email_client.move_emails(
                ["1", "2"], "INBOX", "Archive", allowed_senders=["*@allowed.com"]
            )
        assert moved == ["1", "2"]  # "1" moved, "2" silent no-op
        assert failed == []
        assert _uid_op_targets(mock_imap, "copy") == ["1"]  # only allowed UID copied
        assert _uid_op_targets(mock_imap, "store") == ["1"]  # only allowed UID \Deleted-flagged
        mock_imap.expunge.assert_called_once()  # expunge from real work, not the no-op
