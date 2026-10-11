"""copy_emails provider, service, adapter, and policy tests (spec 07 Copy Within One Account)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from mcp_email_server import app as app_module
from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import (
    BatchMutationOutcome,
    CopyCommand,
    MoveCommand,
    MutationProviderAccess,
    MutationProviderError,
    TargetMutationOutcome,
)
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
from tests.test_mutation_application import _account, _services


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
    mock.uid_search = AsyncMock(
        side_effect=lambda criterion, uids, charset=None: ("OK", [uids.replace(",", " ").encode(), b"SEARCH done"])
    )
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
async def test_all_blocked_skips_search_and_copy(client):
    mock = _mock_imap()
    with patch.object(client, "_blocked_uids", AsyncMock(return_value={"1", "2"})):
        outcome = await _copy(client, mock, ["1", "2"], allowed_senders=["ok@example.com"])
    assert _summary(outcome) == [("1", "succeeded", None), ("2", "succeeded", None)]
    mock.uid_search.assert_not_awaited()
    mock.uid.assert_not_awaited()


@pytest.mark.asyncio
async def test_uid_missing_from_source_fails_without_copy(client):
    mock = _mock_imap()
    mock.uid_search = AsyncMock(return_value=("OK", [b"1", b"SEARCH done"]))
    outcome = await _copy(client, mock, ["1", "2"])
    assert _summary(outcome) == [("1", "succeeded", None), ("2", "failed", "not-found")]
    mock.uid_search.assert_awaited_once_with("UID", "1,2", charset=None)
    mock.uid.assert_awaited_once_with("copy", "1", '"business/techem"')


@pytest.mark.asyncio
async def test_uid_search_failure_is_pre_effect_error(client):
    mock = _mock_imap()
    mock.uid_search = AsyncMock(return_value=("NO", [b"search failed"]))
    with pytest.raises(RuntimeError):
        await _copy(client, mock, ["1"])
    mock.uid.assert_not_awaited()


@pytest.mark.asyncio
async def test_select_failure_is_pre_effect_error(client):
    mock = _mock_imap()
    mock.select = AsyncMock(return_value=("NO", [b"no such mailbox"]))
    with pytest.raises(RuntimeError):
        await _copy(client, mock, ["1"])
    mock.uid.assert_not_awaited()
    mock.logout.assert_awaited_once()


# ---------------------------------------------------------------------------
# Command, application service, adapter, and grant enforcement
# ---------------------------------------------------------------------------


def _ok(*ids):
    return BatchMutationOutcome(tuple(TargetMutationOutcome(i, "succeeded") for i in ids))


@pytest.mark.parametrize(("source", "destination"), [("INBOX", "INBOX"), ("INBOX", "inbox"), ("Archive", "Archive")])
@pytest.mark.parametrize("command_type", [CopyCommand, MoveCommand])
def test_same_mailbox_is_rejected(command_type, source, destination):
    with pytest.raises(ValueError, match="must differ"):
        command_type("primary", ("1",), source, destination).validate()


def test_non_inbox_case_variant_is_a_different_mailbox():
    CopyCommand("primary", ("1",), "Archive", "archive").validate()


@pytest.mark.parametrize(
    "command",
    [
        CopyCommand("primary", (), "INBOX", "Other"),
        CopyCommand("primary", ("1",), "INBOX", ""),
        CopyCommand("primary", ("1",), "", "Other"),
    ],
)
def test_command_rejects_invalid_input(command):
    with pytest.raises(ValueError):
        command.validate()


@pytest.mark.asyncio
async def test_service_invalidates_only_destination():
    provider = MagicMock()
    provider.copy = AsyncMock(return_value=_ok("1"))
    services, _, _, projection = _services(provider=provider)
    outcome = await services.copy.execute(CopyCommand("primary", ("1",), "INBOX", "techem"))
    assert outcome.targets("succeeded") == ["1"]
    assert outcome.reconciliation_needed is False
    projection.invalidate.assert_awaited_once_with(("techem",))


@pytest.mark.asyncio
async def test_service_skips_invalidation_when_nothing_started():
    provider = MagicMock()
    provider.copy = AsyncMock(
        return_value=BatchMutationOutcome((TargetMutationOutcome("1", "failed", "copy-rejected"),))
    )
    services, _, _, projection = _services(provider=provider)
    await services.copy.execute(CopyCommand("primary", ("1",), "INBOX", "techem"))
    projection.invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_projection_failure_keeps_success_and_flags_reconciliation():
    provider = MagicMock()
    provider.copy = AsyncMock(return_value=_ok("1"))
    projection = MagicMock()
    projection.invalidate = AsyncMock(side_effect=RuntimeError("index locked"))
    services, _, _, _ = _services(provider=provider, projection=projection)
    outcome = await services.copy.execute(CopyCommand("primary", ("1",), "INBOX", "techem"))
    assert outcome.targets("succeeded") == ["1"]
    assert outcome.reconciliation_needed is True


@pytest.mark.asyncio
async def test_service_timeout_is_unknown_without_replay():
    provider = MagicMock()
    provider.copy = AsyncMock(side_effect=TimeoutError())
    services, _, _, _ = _services(provider=provider)
    outcome = await services.copy.execute(CopyCommand("primary", ("1", "2"), "INBOX", "techem"))
    assert _summary(outcome) == [("1", "unknown", "provider-timeout"), ("2", "unknown", "provider-timeout")]
    assert outcome.reconciliation_needed is True
    provider.copy.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_denies_without_organize_before_provider_open():
    services, _, factory, _ = _services(account=_account(allowed_mutations=("draft", "append")))
    with pytest.raises(PermissionError, match="not allowed"):
        await services.copy.execute(CopyCommand("primary", ("1",), "INBOX", "techem"))
    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_service_rechecks_organize_after_open():
    provider = MagicMock()
    provider.copy = AsyncMock(return_value=_ok("1"))
    services, _, factory, _ = _services(provider=provider)
    factory.open.return_value = MutationProviderAccess(_account(allowed_mutations=("append",)), provider)
    with pytest.raises(PermissionError, match="not allowed"):
        await services.copy.execute(CopyCommand("primary", ("1",), "INBOX", "techem"))
    provider.copy.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_rejects_invalid_command_before_resolve():
    services, authority, factory, _ = _services()
    with pytest.raises(ValueError):
        await services.copy.execute(CopyCommand("primary", ("1",), "INBOX", "INBOX"))
    authority.resolve.assert_not_called()
    factory.open.assert_not_called()


def _handler(copy: AsyncMock) -> MagicMock:
    handler = MagicMock()
    handler.incoming_client.copy_emails_with_outcome = copy
    handler.outgoing_client = None
    return handler


@pytest.mark.asyncio
async def test_adapter_guards_organize_and_passes_sender_policy():
    handler = _handler(AsyncMock(return_value=_ok("1")))
    account = _account(allowed_senders=("ok@example.com",), report_blocked_mutations=True)
    result = await ClassicMutationProvider(handler).copy(CopyCommand("primary", ("1",), "INBOX", "techem"), account)
    assert result.targets("succeeded") == ["1"]
    handler.incoming_client.copy_emails_with_outcome.assert_awaited_once_with(
        ["1"], "INBOX", "techem", ["ok@example.com"], True
    )
    assert callable(handler.incoming_client.mutation_guard)


@pytest.mark.asyncio
async def test_adapter_denies_without_organize_before_client():
    handler = _handler(AsyncMock(return_value=_ok("1")))
    with pytest.raises(PermissionError):
        await ClassicMutationProvider(handler).copy(
            CopyCommand("primary", ("1",), "INBOX", "techem"), _account(allowed_mutations=("append",))
        )
    handler.incoming_client.copy_emails_with_outcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_bounds_unexpected_client_errors():
    handler = _handler(AsyncMock(side_effect=RuntimeError("SELECT source mailbox INBOX failed")))
    with pytest.raises(MutationProviderError, match="provider_failure"):
        await ClassicMutationProvider(handler).copy(CopyCommand("primary", ("1",), "INBOX", "techem"), _account())


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_command_dispatches_to_runtime_service():
    outcome = _ok("1")
    runtime = MagicMock()
    runtime.mutations.copy.execute = AsyncMock(return_value=outcome)
    command = CopyCommand("primary", ("1",), "INBOX", "techem")
    with patch("mcp_email_server.app.get_application_runtime", return_value=runtime):
        assert await app_module.copy_emails_command(command) is outcome
    runtime.mutations.copy.execute.assert_awaited_once_with(command)


@pytest.mark.asyncio
async def test_tool_is_advertised_as_non_destructive_non_idempotent():
    tools = {tool.name: tool for tool in await app_module.mcp.list_tools()}
    tool = tools["copy_emails"]
    assert tool.inputSchema["required"] == ["account_name", "email_ids", "destination_mailbox"]
    assert tool.inputSchema["properties"]["source_mailbox"]["default"] == "INBOX"
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is False
    assert tool.annotations.openWorldHint is True


@pytest.mark.asyncio
async def test_tool_reports_full_success():
    handler = AsyncMock(return_value=_ok("5", "7"))
    with patch("mcp_email_server.app.copy_emails_command", handler):
        result = await app_module.copy_emails(
            account_name="Gmail", email_ids=["5", "7"], destination_mailbox="business/techem"
        )
    assert result == "Successfully copied 2 email(s) to business/techem"
    assert handler.await_args.args[0] == CopyCommand("Gmail", ("5", "7"), "INBOX", "business/techem")


@pytest.mark.asyncio
async def test_tool_reports_partial_result_per_id():
    outcome = BatchMutationOutcome((
        TargetMutationOutcome("5", "succeeded"),
        TargetMutationOutcome("7", "unknown", "copy-unknown"),
    ))
    with patch("mcp_email_server.app.copy_emails_command", AsyncMock(return_value=outcome)):
        result = await app_module.copy_emails(
            account_name="Gmail", email_ids=["5", "7"], destination_mailbox="techem", source_mailbox="Archive"
        )
    assert result.startswith("Copy result [")
    assert "7" in result
    assert "reconciliation needed" in result


@pytest.mark.asyncio
async def test_sender_allowlist_description_names_copy_emails():
    tools = {tool.name: tool for tool in await app_module.mcp.list_tools()}
    assert "copy_emails" in tools["list_allowed_senders"].description
