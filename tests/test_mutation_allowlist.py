"""Sender-allowlist enforcement on the UID mutation tools."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.emails.classic import EmailClient


@pytest.fixture
def email_client(email_server):  # email_server comes from conftest.py
    return EmailClient(email_server)


def _make_mock_imap(**overrides):
    """AsyncMock IMAP client with sensible mutation defaults."""
    capabilities = overrides.pop("capabilities", ("IMAP4rev1", "UIDPLUS"))
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    mock.id = AsyncMock(return_value=MagicMock(result="OK"))
    mock.select = AsyncMock(return_value=("OK", []))
    mock.uid = AsyncMock(return_value=("OK", []))
    mock.expunge = AsyncMock(return_value=("OK", []))
    mock.logout = AsyncMock()
    mock.protocol = MagicMock(capabilities=capabilities)
    mock.protocol.capability = AsyncMock()
    for k, v in overrides.items():
        setattr(mock, k, v)
    return mock


def _uid_op_targets(mock_imap, op):
    """UIDs that a given uid(op, uid, ...) command was issued for."""
    return [c.args[1] for c in mock_imap.uid.call_args_list if c.args and c.args[0] == op]


class _MailboxStateProtocolFake:
    """Protocol fake that makes target-scoped expunge observable."""

    def __init__(self) -> None:
        self._client_task = asyncio.Future()
        self._client_task.set_result(None)
        self.protocol = SimpleNamespace(
            capabilities=("IMAP4rev1", "UIDPLUS"),
            capability=AsyncMock(),
        )
        self.source_uids = {"99", "100"}
        self.deleted_uids = {"99"}
        self.destination_uids: set[str] = set()
        self.uid_calls: list[tuple[str, tuple[str, ...]]] = []
        self.mailbox_wide_expunge_called = False

    async def wait_hello_from_server(self):
        return None

    async def login(self, _username: str, _password: str):
        return SimpleNamespace(result="OK", lines=[])

    async def id(self, **_kwargs):
        return SimpleNamespace(result="OK")

    async def select(self, _mailbox: str):
        return "OK", []

    async def uid(self, command: str, *args: str):
        self.uid_calls.append((command, args))
        if command == "copy":
            self.destination_uids.add(args[0])
        elif command == "store":
            self.deleted_uids.add(args[0])
        elif command == "expunge":
            self.source_uids.difference_update(set(args[0].split(",")) & self.deleted_uids)
        return "OK", []

    async def expunge(self):
        self.mailbox_wide_expunge_called = True
        raise AssertionError("Message-scoped operations must not issue bare EXPUNGE")

    async def logout(self):
        return None


@pytest.mark.asyncio
async def test_delete_uid_expunge_preserves_another_clients_deleted_message(email_client):
    imap = _MailboxStateProtocolFake()
    with patch.object(email_client, "imap_class", return_value=imap):
        result = await email_client.delete_emails(["100"], allowed_senders=[])

    assert result == (["100"], [])
    assert imap.source_uids == {"99"}
    assert ("expunge", ("100",)) in imap.uid_calls
    assert not imap.mailbox_wide_expunge_called


@pytest.mark.asyncio
async def test_move_fallback_uid_expunge_preserves_another_clients_deleted_message(email_client):
    imap = _MailboxStateProtocolFake()
    with patch.object(email_client, "imap_class", return_value=imap):
        result = await email_client.move_emails(["100"], "INBOX", "Archive", allowed_senders=[])

    assert result == (["100"], [])
    assert imap.destination_uids == {"100"}
    assert imap.source_uids == {"99"}
    assert ("expunge", ("100",)) in imap.uid_calls
    assert not imap.mailbox_wide_expunge_called


@pytest.mark.asyncio
async def test_delete_without_uidplus_rejects_before_flagging(email_client):
    mock_imap = _make_mock_imap(capabilities=("IMAP4rev1",))
    with patch.object(email_client, "imap_class", return_value=mock_imap):
        deleted, failed = await email_client.delete_emails(["1", "2"], allowed_senders=[])

    assert deleted == []
    assert failed == ["1", "2"]
    assert _uid_op_targets(mock_imap, "store") == []
    assert _uid_op_targets(mock_imap, "expunge") == []
    mock_imap.expunge.assert_not_called()


@pytest.mark.asyncio
async def test_delete_uses_post_auth_capabilities_and_rejects_before_flagging(email_client):
    mock_imap = _make_mock_imap(capabilities=("IMAP4rev1", "UIDPLUS"))

    async def refresh_capabilities():
        mock_imap.protocol.capabilities = ("IMAP4rev1",)

    mock_imap.protocol.capability = AsyncMock(side_effect=refresh_capabilities)
    with patch.object(email_client, "imap_class", return_value=mock_imap):
        result = await email_client.delete_emails(["1"], allowed_senders=[])

    assert result == ([], ["1"])
    assert _uid_op_targets(mock_imap, "store") == []
    assert _uid_op_targets(mock_imap, "expunge") == []


@pytest.mark.asyncio
async def test_delete_normalizes_post_auth_uidplus_for_check_and_command(email_client):
    mock_imap = _make_mock_imap(capabilities=("IMAP4rev1",))

    async def refresh_capabilities():
        mock_imap.protocol.capabilities = ("imap4rev1", "uidplus")

    mock_imap.protocol.capability = AsyncMock(side_effect=refresh_capabilities)
    with patch.object(email_client, "imap_class", return_value=mock_imap):
        result = await email_client.delete_emails(["1"], allowed_senders=[])

    assert result == (["1"], [])
    assert mock_imap.protocol.capabilities == {"IMAP4REV1", "UIDPLUS"}
    assert _uid_op_targets(mock_imap, "store") == ["1"]
    assert _uid_op_targets(mock_imap, "expunge") == ["1"]


@pytest.mark.asyncio
async def test_delete_capability_refresh_timeout_fails_before_select_or_flagging(email_client):
    mock_imap = _make_mock_imap(capabilities=("IMAP4rev1", "UIDPLUS"))
    never_returns = asyncio.Event()
    mock_imap.protocol.capability = AsyncMock(side_effect=never_returns.wait)

    with (
        patch("mcp_email_server.emails.classic._IMAP_CAPABILITY_TIMEOUT_SECONDS", 0.01),
        patch.object(email_client, "imap_class", return_value=mock_imap),
        pytest.raises(TimeoutError),
    ):
        await email_client.delete_emails(["1"], allowed_senders=[])

    mock_imap.select.assert_not_called()
    mock_imap.uid.assert_not_called()


@pytest.mark.asyncio
async def test_delete_duplicate_uids_have_one_provider_effect_and_consistent_results(email_client):
    mock_imap = _make_mock_imap()
    with patch.object(email_client, "imap_class", return_value=mock_imap):
        result = await email_client.delete_emails(["1", "1"], allowed_senders=[])

    assert result == (["1", "1"], [])
    assert _uid_op_targets(mock_imap, "store") == ["1"]
    assert _uid_op_targets(mock_imap, "expunge") == ["1"]


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
        async def uid_side_effect(command, *args):
            if command == "expunge":
                return "NO", [b"UID EXPUNGE failed"]
            return "OK", []

        mock_imap = _make_mock_imap()
        mock_imap.uid = AsyncMock(side_effect=uid_side_effect)
        with patch.object(email_client, "imap_class", return_value=mock_imap):
            deleted, failed = await email_client.delete_emails(["1", "2"], allowed_senders=[])

        assert deleted == []
        assert failed == ["1", "2"]
        assert _uid_op_targets(mock_imap, "store") == ["1", "2"]
        assert _uid_op_targets(mock_imap, "expunge") == ["1,2"]
        mock_imap.expunge.assert_not_called()


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
        mock_imap = _make_mock_imap(capabilities=("IMAP4rev1", "UIDPLUS"))
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
        mock_imap = _make_mock_imap(capabilities=("IMAP4rev1", "UIDPLUS"))  # safe fallback path
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
        assert _uid_op_targets(mock_imap, "expunge") == ["1"]
        mock_imap.expunge.assert_not_called()
