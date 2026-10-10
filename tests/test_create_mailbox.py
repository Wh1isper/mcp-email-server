"""create_mailbox provider, service, adapter, and policy tests (spec 07 Mailbox Creation)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient


def _mock_imap(*, list_lines=(), create=("OK", [b"CREATE completed"])):
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    mock.logout = AsyncMock()
    mock.protocol = MagicMock(capabilities=("IMAP4rev1",))
    mock.protocol.capability = AsyncMock()
    mock.list = AsyncMock(return_value=("OK", [*list_lines, b"LIST completed"]))
    if isinstance(create, BaseException):
        mock.create = AsyncMock(side_effect=create)
    else:
        mock.create = AsyncMock(return_value=create)
    return mock


@pytest.fixture
def client():
    server = EmailServer(user_name="u", password="p", host="imap.example.com", port=993, use_ssl=True)
    return EmailClient(server, sender="Test <test@example.com>")


async def _create(client, mock, name):
    with patch.object(client, "imap_class", return_value=mock):
        return await client.create_mailbox(name)


@pytest.mark.asyncio
async def test_creates_nested_name_exactly(client):
    mock = _mock_imap()
    assert await _create(client, mock, "business/techem") == "created"
    mock.list.assert_awaited_once_with('""', '"business/techem"')
    mock.create.assert_awaited_once_with('"business/techem"')
    mock.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_encodes_non_ascii_as_modified_utf7(client):
    mock = _mock_imap()
    assert await _create(client, mock, "Účty") == "created"
    mock.create.assert_awaited_once_with('"&ANoBDQ-ty"')


@pytest.mark.asyncio
async def test_existing_exact_name_skips_create(client):
    mock = _mock_imap(list_lines=[b'(\\HasNoChildren) "/" "business/techem"'])
    assert await _create(client, mock, "business/techem") == "already_exists"
    mock.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["INBOX", "inbox", "Inbox"])
async def test_inbox_is_case_insensitive(client, requested):
    mock = _mock_imap(list_lines=[b'(\\HasNoChildren) "/" "INBOX"'])
    assert await _create(client, mock, requested) == "already_exists"
    mock.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_case_match_is_not_existence(client):
    mock = _mock_imap(list_lines=[b'(\\HasNoChildren) "/" "Business"'])
    assert await _create(client, mock, "business") == "created"
    mock.create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("line", [b"[ALREADYEXISTS] Mailbox exists", b"[alreadyexists]  exists"])
async def test_alreadyexists_response_code(client, line):
    mock = _mock_imap(create=("NO", [line]))
    assert await _create(client, mock, "techem") == "already_exists"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["NO", "BAD"])
async def test_other_rejection_is_bounded_failure(client, status):
    mock = _mock_imap(create=(status, [b"[CANNOT] secret server detail"]))
    with pytest.raises(RuntimeError) as raised:
        await _create(client, mock, "techem")
    assert "secret server detail" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), ConnectionResetError()])
async def test_interrupted_create_is_unknown(client, error):
    mock = _mock_imap(create=error)
    assert await _create(client, mock, "techem") == "unknown"


@pytest.mark.asyncio
async def test_bye_response_is_unknown(client):
    mock = _mock_imap(create=("BYE", [b"closing"]))
    assert await _create(client, mock, "techem") == "unknown"


@pytest.mark.asyncio
async def test_guard_revoked_between_list_and_create(client):
    mock = _mock_imap()
    client.mutation_guard = MagicMock(side_effect=PermissionError("Mutation class 'organize' is not allowed"))
    with pytest.raises(PermissionError):
        await _create(client, mock, "techem")
    mock.list.assert_awaited_once()
    mock.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_failure_is_pre_effect_error(client):
    mock = _mock_imap()
    mock.list = AsyncMock(return_value=("NO", [b"denied"]))
    with pytest.raises(RuntimeError):
        await _create(client, mock, "techem")
    mock.create.assert_not_awaited()
