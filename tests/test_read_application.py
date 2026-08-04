from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from mcp_email_server.application import limits as limits_module
from mcp_email_server.application import reads as reads_module
from mcp_email_server.application.mutations import APPLICATION_LIMITS, BatchMutationOutcome, TargetMutationOutcome
from mcp_email_server.application.reads import (
    AttachmentDownloadService,
    AttachmentPayload,
    DownloadAttachmentCommand,
    EmailContentService,
    GetEmailContentQuery,
    ListMailboxesQuery,
    MailboxDiscoveryService,
    ReadAccountSnapshot,
    ReadProviderAccess,
    ReadProviderError,
)
from mcp_email_server.emails.models import EmailBodyResponse, EmailContentBatchResponse, MailboxInfo


def _account(*, downloads: bool = False) -> ReadAccountSnapshot:
    return ReadAccountSnapshot(
        account_name="work",
        mode="managed",
        allowed_senders=("allowed@example.test",),
        enable_attachment_download=downloads,
    )


def _body(uid: str) -> EmailBodyResponse:
    return EmailBodyResponse(
        email_id=uid,
        message_id=None,
        subject="Subject",
        sender="allowed@example.test",
        recipients=["work@example.test"],
        date=datetime.now(UTC),
        body="body",
        attachments=[],
    )


def _ports(*, initial: ReadAccountSnapshot | None = None, fresh: ReadAccountSnapshot | None = None):
    authority = Mock()
    authority.resolve.return_value = initial or _account()
    provider = Mock()
    factory = Mock()
    factory.open.return_value = ReadProviderAccess(fresh or initial or _account(), provider)
    return authority, factory, provider


@pytest.mark.asyncio
async def test_content_service_deduplicates_and_chunks_best_effort_marking() -> None:
    authority, factory, provider = _ports()
    bodies = [_body(str(uid)) for uid in range(1, 102)]
    bodies.append(bodies[0])
    provider.get_content = AsyncMock(
        return_value=EmailContentBatchResponse(
            emails=bodies,
            requested_count=len(bodies),
            retrieved_count=len(bodies),
            failed_ids=[],
        )
    )

    async def mark(command):
        return BatchMutationOutcome(tuple(TargetMutationOutcome(uid, "succeeded") for uid in command.email_ids))

    mark_read = Mock()
    mark_read.execute = AsyncMock(side_effect=mark)
    service = EmailContentService(authority, factory, mark_read)

    response = await service.execute(
        GetEmailContentQuery(
            account_name="work",
            email_ids=tuple(body.email_id for body in bodies),
            mark_as_read=True,
        )
    )

    assert response.retrieved_count == 102
    assert mark_read.execute.await_count == 2
    assert len(mark_read.execute.await_args_list[0].args[0].email_ids) == 100
    assert mark_read.execute.await_args_list[1].args[0].email_ids == ("101",)


@pytest.mark.asyncio
async def test_content_service_stops_mark_chunks_after_unknown_or_reconciliation() -> None:
    authority, factory, provider = _ports()
    bodies = [_body(str(uid)) for uid in range(1, 102)]
    provider.get_content = AsyncMock(
        return_value=EmailContentBatchResponse(
            emails=bodies,
            requested_count=len(bodies),
            retrieved_count=len(bodies),
            failed_ids=[],
        )
    )
    mark_read = Mock()
    mark_read.execute = AsyncMock(
        return_value=BatchMutationOutcome(
            (
                *(TargetMutationOutcome(str(uid), "succeeded") for uid in range(1, 100)),
                TargetMutationOutcome("100", "unknown"),
            ),
            reconciliation_needed=True,
        )
    )

    await EmailContentService(authority, factory, mark_read).execute(
        GetEmailContentQuery(
            account_name="work",
            email_ids=tuple(body.email_id for body in bodies),
            mark_as_read=True,
        )
    )

    assert mark_read.execute.await_count == 1


@pytest.mark.asyncio
async def test_content_service_preserves_bodies_when_marking_fails() -> None:
    authority, factory, provider = _ports()
    response = EmailContentBatchResponse(emails=[_body("1")], requested_count=1, retrieved_count=1, failed_ids=[])
    provider.get_content = AsyncMock(return_value=response)
    mark_read = Mock()
    mark_read.execute = AsyncMock(side_effect=RuntimeError("provider details"))

    result = await EmailContentService(authority, factory, mark_read).execute(
        GetEmailContentQuery(account_name="work", email_ids=("1",), mark_as_read=True)
    )

    assert result == response


@pytest.mark.asyncio
async def test_attachment_service_rechecks_fresh_policy_before_provider_write() -> None:
    authority, factory, provider = _ports(initial=_account(downloads=True), fresh=_account(downloads=False))
    provider.fetch_attachment = AsyncMock()
    artifacts = Mock()
    artifacts.preflight = AsyncMock()
    artifacts.write = AsyncMock()
    command = DownloadAttachmentCommand("work", "1", "document.pdf", "downloads/document.pdf")

    with pytest.raises(PermissionError, match="disabled"):
        await AttachmentDownloadService(authority, factory, artifacts).execute(command)

    provider.fetch_attachment.assert_not_awaited()
    artifacts.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_service_preflights_filesystem_before_provider_or_credential_effect() -> None:
    authority, factory, provider = _ports(initial=_account(downloads=True), fresh=_account(downloads=True))
    provider.fetch_attachment = AsyncMock()
    artifacts = Mock()
    artifacts.preflight = AsyncMock(side_effect=PermissionError("unsupported local storage"))
    artifacts.write = AsyncMock()
    command = DownloadAttachmentCommand("work", "1", "document.pdf", "downloads/document.pdf")

    with pytest.raises(PermissionError, match="unsupported local storage"):
        await AttachmentDownloadService(authority, factory, artifacts).execute(command)

    artifacts.preflight.assert_awaited_once_with(command.save_path, command.attachment_name)
    factory.open.assert_not_called()
    provider.fetch_attachment.assert_not_awaited()
    artifacts.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_service_rechecks_authority_after_fetch_before_artifact_write() -> None:
    authority, factory, provider = _ports(initial=_account(downloads=True), fresh=_account(downloads=True))
    authority.resolve.side_effect = [_account(downloads=True), _account(downloads=False)]
    payload = AttachmentPayload("1", "document.pdf", "application/pdf", b"content")
    provider.fetch_attachment = AsyncMock(return_value=payload)
    artifacts = Mock()
    artifacts.preflight = AsyncMock()
    artifacts.write = AsyncMock()
    command = DownloadAttachmentCommand("work", "1", "document.pdf", "downloads/document.pdf")

    with pytest.raises(PermissionError, match="disabled"):
        await AttachmentDownloadService(authority, factory, artifacts).execute(command)

    provider.fetch_attachment.assert_awaited_once_with(command, factory.open.return_value.account)
    authority.resolve.assert_called_with("work", expected_mode="managed")
    artifacts.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_service_denies_cached_disabled_policy_before_open() -> None:
    authority, factory, _provider = _ports(initial=_account(downloads=False))

    artifacts = Mock()
    artifacts.preflight = AsyncMock()
    artifacts.write = AsyncMock()
    with pytest.raises(PermissionError, match="disabled"):
        await AttachmentDownloadService(authority, factory, artifacts).execute(
            DownloadAttachmentCommand("work", "1", "document.pdf", "downloads/document.pdf")
        )

    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_attachment_service_writes_only_bounded_provider_payload_through_artifact_port() -> None:
    authority, factory, provider = _ports(initial=_account(downloads=True), fresh=_account(downloads=True))
    payload = AttachmentPayload("1", "document.pdf", "application/pdf", b"content")
    provider.fetch_attachment = AsyncMock(return_value=payload)
    artifacts = Mock()
    artifacts.preflight = AsyncMock(return_value="/approved/document.pdf")
    artifacts.write = AsyncMock(return_value="/approved/document.pdf")
    command = DownloadAttachmentCommand("work", "1", "document.pdf", "downloads/document.pdf")

    response = await AttachmentDownloadService(authority, factory, artifacts).execute(command)

    assert response.size == len(payload.content)
    assert response.saved_path == "/approved/document.pdf"
    artifacts.preflight.assert_awaited_once_with(command.save_path, command.attachment_name)
    artifacts.write.assert_awaited_once_with("/approved/document.pdf", payload)


@pytest.mark.asyncio
async def test_attachment_service_uses_resolved_default_path_without_exposing_it_to_provider() -> None:
    authority, factory, provider = _ports(initial=_account(downloads=True), fresh=_account(downloads=True))
    payload = AttachmentPayload("1", "document.pdf", "application/pdf", b"content")
    provider.fetch_attachment = AsyncMock(return_value=payload)
    artifacts = Mock()
    artifacts.preflight = AsyncMock(return_value="/home/user/Downloads/mcp-email-server/document-abcd.pdf")
    artifacts.write = AsyncMock(return_value="/home/user/Downloads/mcp-email-server/document-abcd.pdf")
    command = DownloadAttachmentCommand("work", "1", "document.pdf")

    response = await AttachmentDownloadService(authority, factory, artifacts).execute(command)

    assert response.saved_path == "/home/user/Downloads/mcp-email-server/document-abcd.pdf"
    artifacts.preflight.assert_awaited_once_with(None, "document.pdf")
    provider.fetch_attachment.assert_awaited_once_with(command, factory.open.return_value.account)
    artifacts.write.assert_awaited_once_with(response.saved_path, payload)


@pytest.mark.asyncio
async def test_attachment_service_rejects_oversized_payload_before_artifact_write() -> None:
    authority, factory, provider = _ports(initial=_account(downloads=True), fresh=_account(downloads=True))
    provider.fetch_attachment = AsyncMock(
        return_value=AttachmentPayload(
            "1",
            "large.bin",
            "application/octet-stream",
            b"x" * (APPLICATION_LIMITS.attachment_bytes + 1),
        )
    )
    artifacts = Mock()
    artifacts.preflight = AsyncMock()
    artifacts.write = AsyncMock()

    with pytest.raises(ValueError, match="attachment exceeds"):
        await AttachmentDownloadService(authority, factory, artifacts).execute(
            DownloadAttachmentCommand("work", "1", "large.bin", "downloads/large.bin")
        )

    artifacts.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_mailbox_service_maps_query_and_preserves_provider_order() -> None:
    authority, factory, provider = _ports()
    mailboxes = [MailboxInfo(name="INBOX", delimiter="/", flags=[]), MailboxInfo(name="Sent", delimiter="/", flags=[])]
    provider.list_mailboxes = AsyncMock(return_value=mailboxes)

    result = await MailboxDiscoveryService(authority, factory).execute(
        ListMailboxesQuery("work", pattern="INBOX*", reference="")
    )

    assert result == mailboxes
    provider.list_mailboxes.assert_awaited_once()


@pytest.mark.parametrize(
    "query, message",
    [
        (GetEmailContentQuery("work", ("01",)), "canonical positive"),
        (GetEmailContentQuery("work", tuple(str(uid) for uid in range(1, 502))), "between 1 and 500"),
        (GetEmailContentQuery("work", ("1",), max_body_length=100_001), "between 1 and 100000"),
    ],
)
def test_content_query_rejects_invalid_or_unbounded_input(query: GetEmailContentQuery, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        query.validate()


@pytest.mark.parametrize(
    "query",
    [
        ListMailboxesQuery("work\x7f"),
        ListMailboxesQuery("work", pattern="*\x00"),
        ListMailboxesQuery("work", reference="root\x1f"),
        GetEmailContentQuery("work", ("1",), mailbox="INBOX\x7f"),
        DownloadAttachmentCommand("work", "1", "part\x00.txt", "download.txt"),
        DownloadAttachmentCommand("work", "1", "part.txt", "download\x7f.txt"),
    ],
)
def test_read_controlled_fields_reject_c0_and_del(
    query: ListMailboxesQuery | GetEmailContentQuery | DownloadAttachmentCommand,
) -> None:
    with pytest.raises(ValueError, match="control characters"):
        query.validate()


def test_mailbox_pattern_uses_utf8_byte_limit() -> None:
    ListMailboxesQuery("work", pattern="é" * (APPLICATION_LIMITS.mailbox_bytes // 2)).validate()

    with pytest.raises(ValueError, match="exceeds"):
        ListMailboxesQuery("work", pattern="é" * (APPLICATION_LIMITS.mailbox_bytes // 2) + "a").validate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "valid"),
    [("aaa", True), ("éé", True), ("ééa", False)],
)
async def test_content_aggregate_body_limit_uses_utf8_bytes_at_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        reads_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, body_bytes=10, aggregate_body_bytes=4),
    )
    authority, factory, provider = _ports()
    response = EmailContentBatchResponse(
        emails=[_body("1").model_copy(update={"body": body})],
        requested_count=1,
        retrieved_count=1,
        failed_ids=[],
    )
    provider.get_content = AsyncMock(return_value=response)
    service = EmailContentService(authority, factory, Mock())

    if valid:
        assert await service.execute(GetEmailContentQuery("work", ("1",))) == response
    else:
        with pytest.raises(ReadProviderError, match="bodies exceed"):
            await service.execute(GetEmailContentQuery("work", ("1",)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value", "valid"),
    [
        ("in_reply_to", "éé", True),
        ("in_reply_to", "ééa", False),
        ("references", "éé", True),
        ("references", "ééa", False),
    ],
)
async def test_content_thread_header_limit_uses_utf8_bytes_at_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: str,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        reads_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, header_bytes=4),
    )
    authority, factory, provider = _ports()
    response = EmailContentBatchResponse(
        emails=[_body("1").model_copy(update={field_name: value})],
        requested_count=1,
        retrieved_count=1,
        failed_ids=[],
    )
    provider.get_content = AsyncMock(return_value=response)
    service = EmailContentService(authority, factory, Mock())

    if valid:
        assert await service.execute(GetEmailContentQuery("work", ("1",))) == response
    else:
        with pytest.raises(ReadProviderError, match="thread header exceeds"):
            await service.execute(GetEmailContentQuery("work", ("1",)))


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "valid"), [("éé", True), ("ééa", False)])
async def test_content_thread_headers_count_toward_aggregate_header_limit(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    valid: bool,
) -> None:
    authority, factory, provider = _ports()
    email = _body("1")
    base_header_bytes = sum(
        len(item.encode("utf-8"))
        for item in (
            email.email_id,
            email.message_id or "",
            email.subject,
            email.sender,
            *email.recipients,
            *email.attachments,
        )
    )
    monkeypatch.setattr(
        reads_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, header_bytes=10, aggregate_header_bytes=base_header_bytes + 4),
    )
    response = EmailContentBatchResponse(
        emails=[email.model_copy(update={"references": value})],
        requested_count=1,
        retrieved_count=1,
        failed_ids=[],
    )
    provider.get_content = AsyncMock(return_value=response)
    service = EmailContentService(authority, factory, Mock())

    if valid:
        assert await service.execute(GetEmailContentQuery("work", ("1",))) == response
    else:
        with pytest.raises(ReadProviderError, match="headers exceed"):
            await service.execute(GetEmailContentQuery("work", ("1",)))


@pytest.mark.asyncio
@pytest.mark.parametrize(("failed_count", "valid"), [(1, True), (2, True), (3, False)])
async def test_content_warning_count_limit_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    failed_count: int,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        reads_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, warning_items=2),
    )
    authority, factory, provider = _ports()
    response = EmailContentBatchResponse(
        emails=[],
        requested_count=failed_count,
        retrieved_count=0,
        failed_ids=[str(uid) for uid in range(1, failed_count + 1)],
    )
    provider.get_content = AsyncMock(return_value=response)
    service = EmailContentService(authority, factory, Mock())
    query = GetEmailContentQuery("work", tuple(response.failed_ids))

    if valid:
        assert await service.execute(query) == response
    else:
        with pytest.raises(ReadProviderError, match="failed ID count"):
            await service.execute(query)


@pytest.mark.asyncio
@pytest.mark.parametrize(("ceiling_delta", "valid"), [(1, True), (0, True), (-1, False)])
async def test_content_serialized_result_limit_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    ceiling_delta: int,
    valid: bool,
) -> None:
    authority, factory, provider = _ports()
    response = EmailContentBatchResponse(
        emails=[],
        requested_count=1,
        retrieved_count=0,
        failed_ids=["1"],
    )
    provider.get_content = AsyncMock(return_value=response)
    serialized_size = len(response.model_dump_json().encode("utf-8"))
    monkeypatch.setattr(
        limits_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, serialized_response_bytes=serialized_size + ceiling_delta),
    )
    service = EmailContentService(authority, factory, Mock())

    if valid:
        assert await service.execute(GetEmailContentQuery("work", ("1",))) == response
    else:
        with pytest.raises(ReadProviderError, match="serialized content"):
            await service.execute(GetEmailContentQuery("work", ("1",)))


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["mailboxes", "content", "attachment"])
async def test_read_provider_calls_have_application_deadline(monkeypatch, workflow: str) -> None:
    monkeypatch.setattr(
        reads_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, provider_timeout_seconds=0.001),
    )
    authority, factory, provider = _ports(initial=_account(downloads=True), fresh=_account(downloads=True))

    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    if workflow == "mailboxes":
        provider.list_mailboxes = hang
        operation = MailboxDiscoveryService(authority, factory).execute(ListMailboxesQuery("work"))
    elif workflow == "content":
        provider.get_content = hang
        mark_read = Mock()
        mark_read.execute = AsyncMock()
        operation = EmailContentService(authority, factory, mark_read).execute(GetEmailContentQuery("work", ("1",)))
    else:
        provider.fetch_attachment = hang
        artifacts = Mock()
        artifacts.preflight = AsyncMock()
        artifacts.write = AsyncMock()
        operation = AttachmentDownloadService(authority, factory, artifacts).execute(
            DownloadAttachmentCommand("work", "1", "document.pdf", "downloads/document.pdf")
        )

    with pytest.raises(ReadProviderError, match="timed out"):
        await operation
