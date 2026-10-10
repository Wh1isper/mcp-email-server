"""create_mailbox provider, service, adapter, and policy tests (spec 07 Mailbox Creation)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import CreateMailboxCommand, MutationProviderError
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
from tests.test_mutation_application import _account, _services


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


# ---------------------------------------------------------------------------
# Application service, adapter, and grant enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   ", "a*b", "a%b", "x\x00y", "x" * 1025])
def test_command_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        CreateMailboxCommand("primary", name).validate()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["created", "already_exists"])
async def test_service_reports_provider_status(status):
    provider = MagicMock()
    provider.create_mailbox = AsyncMock(return_value=status)
    services, _, _, projection = _services(provider=provider)
    outcome = await services.create_mailbox.execute(CreateMailboxCommand("primary", "business/techem"))
    assert (outcome.mailbox, outcome.status, outcome.reconciliation_needed) == ("business/techem", status, False)
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_unknown_sets_reconciliation():
    provider = MagicMock()
    provider.create_mailbox = AsyncMock(return_value="unknown")
    services, _, _, _ = _services(provider=provider)
    outcome = await services.create_mailbox.execute(CreateMailboxCommand("primary", "techem"))
    assert outcome.status == "unknown"
    assert outcome.reconciliation_needed is True


@pytest.mark.asyncio
async def test_service_timeout_is_unknown_without_replay():
    provider = MagicMock()
    provider.create_mailbox = AsyncMock(side_effect=TimeoutError())
    services, _, _, _ = _services(provider=provider)
    outcome = await services.create_mailbox.execute(CreateMailboxCommand("primary", "techem"))
    assert (outcome.status, outcome.reconciliation_needed) == ("unknown", True)
    provider.create_mailbox.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_denies_without_organize_before_provider_open():
    services, _, factory, _ = _services(account=_account(allowed_mutations=("draft", "append")))
    with pytest.raises(PermissionError, match="not allowed"):
        await services.create_mailbox.execute(CreateMailboxCommand("primary", "techem"))
    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_service_rejects_invalid_name_before_resolve():
    services, authority, factory, _ = _services()
    with pytest.raises(ValueError):
        await services.create_mailbox.execute(CreateMailboxCommand("primary", "a*"))
    authority.resolve.assert_not_called()
    factory.open.assert_not_called()


def _handler(create_mailbox: AsyncMock) -> MagicMock:
    handler = MagicMock()
    handler.incoming_client.create_mailbox = create_mailbox
    handler.outgoing_client = None
    return handler


@pytest.mark.asyncio
async def test_adapter_guards_organize_and_calls_client():
    handler = _handler(AsyncMock(return_value="created"))
    result = await ClassicMutationProvider(handler).create_mailbox(
        CreateMailboxCommand("primary", "techem"), _account()
    )
    assert result == "created"
    handler.incoming_client.create_mailbox.assert_awaited_once_with("techem")
    assert callable(handler.incoming_client.mutation_guard)


@pytest.mark.asyncio
async def test_adapter_denies_without_organize_before_client():
    handler = _handler(AsyncMock(return_value="created"))
    with pytest.raises(PermissionError):
        await ClassicMutationProvider(handler).create_mailbox(
            CreateMailboxCommand("primary", "techem"), _account(allowed_mutations=("append",))
        )
    handler.incoming_client.create_mailbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_bounds_unexpected_client_errors():
    handler = _handler(AsyncMock(side_effect=RuntimeError("CREATE mailbox failed (NO)")))
    with pytest.raises(MutationProviderError, match="provider_failure"):
        await ClassicMutationProvider(handler).create_mailbox(CreateMailboxCommand("primary", "techem"), _account())
