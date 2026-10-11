"""copy_emails provider, service, adapter, and policy tests (spec 07 Copy Within One Account)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient


def _mock_imap(*, uid=("OK", [b"COPY completed"])):
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    mock.logout = AsyncMock()
    mock.protocol = MagicMock(capabilities=("IMAP4rev1", "UIDPLUS", "MOVE"))
    mock.protocol.capability = AsyncMock()
    mock.select = AsyncMock(return_value=("OK", [b"SELECT completed"]))
    mock.create = AsyncMock()
    mock.expunge = AsyncMock()
    if isinstance(uid, list | BaseException):
        mock.uid = AsyncMock(side_effect=uid)
    else:
        mock.uid = AsyncMock(return_value=uid)
    return mock


@pytest.fixture
def client():
    server = EmailServer(user_name="u", password="p", host="imap.example.com", port=993, use_ssl=True)
    return EmailClient(server, sender="Test <test@example.com>")


async def _copy(client, mock, ids, source="INBOX", destination="business/techem", **kwargs):
    with patch.object(client, "imap_class", return_value=mock):
        return await client.copy_emails_with_outcome(ids, source, destination, **kwargs)


def _summary(outcome):
    return [(item.target, item.status, item.detail) for item in outcome.outcomes]


@pytest.mark.asyncio
async def test_copies_each_uid_into_exact_destination(client):
    mock = _mock_imap()
    outcome = await _copy(client, mock, ["1", "2"])
    assert _summary(outcome) == [("1", "succeeded", None), ("2", "succeeded", None)]
    assert outcome.reconciliation_needed is False
    mock.select.assert_awaited_once_with('"INBOX"')
    assert mock.uid.await_args_list == [
        call("copy", "1", '"business/techem"'),
        call("copy", "2", '"business/techem"'),
    ]
    mock.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_never_moves_stores_expunges_or_creates(client):
    mock = _mock_imap()
    await _copy(client, mock, ["1", "2"])
    assert {item.args[0] for item in mock.uid.await_args_list} == {"copy"}
    mock.expunge.assert_not_awaited()
    mock.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_encodes_non_ascii_destination_as_modified_utf7(client):
    mock = _mock_imap()
    await _copy(client, mock, ["1"], destination="Účty")
    mock.uid.assert_awaited_once_with("copy", "1", '"&ANoBDQ-ty"')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", [("NO", [b"[TRYCREATE] Mailbox does not exist"]), ("BAD", [b"secret server detail"])]
)
async def test_rejected_copy_fails_target_and_continues(client, response):
    mock = _mock_imap(uid=[response, ("OK", [b"COPY completed"])])
    outcome = await _copy(client, mock, ["1", "2"])
    assert _summary(outcome) == [("1", "failed", "copy-rejected"), ("2", "succeeded", None)]
    assert "secret server detail" not in repr(outcome)
    mock.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionResetError(), TimeoutError()])
async def test_interrupted_copy_is_unknown_and_continues(client, error):
    mock = _mock_imap(uid=[error, ("OK", [b"COPY completed"])])
    outcome = await _copy(client, mock, ["1", "2"])
    assert _summary(outcome) == [("1", "unknown", "copy-unknown"), ("2", "succeeded", None)]
    assert outcome.reconciliation_needed is True


@pytest.mark.asyncio
async def test_bye_response_is_unknown(client):
    mock = _mock_imap(uid=("BYE", [b"closing"]))
    outcome = await _copy(client, mock, ["1"])
    assert _summary(outcome) == [("1", "unknown", "copy-unknown")]


@pytest.mark.asyncio
async def test_cancellation_marks_remaining_not_attempted(client):
    mock = _mock_imap(uid=[asyncio.CancelledError()])
    outcome = await _copy(client, mock, ["1", "2", "3"])
    assert _summary(outcome) == [
        ("1", "unknown", "copy-unknown"),
        ("2", "failed", "not-attempted"),
        ("3", "failed", "not-attempted"),
    ]
    assert mock.uid.await_count == 1


@pytest.mark.asyncio
async def test_revoked_authority_fails_targets_without_copy(client):
    mock = _mock_imap()
    client.mutation_guard = MagicMock(side_effect=PermissionError("Mutation class 'organize' is not allowed"))
    outcome = await _copy(client, mock, ["1", "2"])
    assert _summary(outcome) == [("1", "failed", "mutation-policy"), ("2", "failed", "mutation-policy")]
    mock.uid.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report", "expected"), [(False, ("2", "succeeded", None)), (True, ("2", "failed", "sender-policy"))]
)
async def test_sender_policy_matches_move(client, report, expected):
    mock = _mock_imap()
    with patch.object(client, "_blocked_uids", AsyncMock(return_value={"2"})):
        outcome = await _copy(
            client, mock, ["1", "2"], allowed_senders=["ok@example.com"], report_blocked_mutations=report
        )
    assert _summary(outcome) == [("1", "succeeded", None), expected]
    mock.uid.assert_awaited_once_with("copy", "1", '"business/techem"')


@pytest.mark.asyncio
async def test_select_failure_is_pre_effect_error(client):
    mock = _mock_imap()
    mock.select = AsyncMock(return_value=("NO", [b"no such mailbox"]))
    with pytest.raises(RuntimeError):
        await _copy(client, mock, ["1"])
    mock.uid.assert_not_awaited()
    mock.logout.assert_awaited_once()
