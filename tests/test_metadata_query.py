from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_email_server.application import limits as limits_module
from mcp_email_server.application import metadata as metadata_module
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.metadata import (
    ListEmailMetadataQuery,
    MailboxMetadataSnapshot,
    MailboxState,
    MetadataAccountSnapshot,
    MetadataProjectionError,
    MetadataProviderAccess,
    MetadataProviderError,
    MetadataProviderObservationError,
    MetadataQueryService,
)
from mcp_email_server.emails.models import EmailMetadata, EmailMetadataPageResponse, MailboxInfo

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


def _email(uid: int, sender: str = "allowed@example.test") -> dict[str, object]:
    date = NOW + timedelta(minutes=uid)
    return {
        "email_id": str(uid),
        "message_id": f"<{uid}@example.test>",
        "subject": f"Subject {uid}",
        "from": sender,
        "to": ["user@example.test"],
        "date": date,
        "attachments": [],
        "_internal_date": date,
        "_flags": [],
    }


def _fallback_response(subject: str | None = None) -> EmailMetadataPageResponse:
    return EmailMetadataPageResponse(
        page=1,
        page_size=10,
        before=None,
        since=None,
        subject=subject,
        emails=[],
        total=0,
    )


def _service(
    mode: str = "legacy",
) -> tuple[MetadataQueryService, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    account = MetadataAccountSnapshot(
        account_name="work",
        mode="managed" if mode == "managed" else "legacy",
        allowed_senders=("allowed@example.test",),
    )
    accounts = MagicMock()
    accounts.resolve.return_value = account
    provider = MagicMock()
    provider.list_metadata = AsyncMock(return_value=_fallback_response())
    provider.mailbox_state = AsyncMock(return_value=MailboxState(7, 4, 3))
    provider.mailbox_snapshot = AsyncMock()
    providers = MagicMock()
    providers.open.return_value = MetadataProviderAccess(account=account, provider=provider)
    projection = MagicMock()
    projection.read_complete = AsyncMock(return_value=None)
    projection.write_snapshot = AsyncMock()
    projections = MagicMock()
    projections.open = AsyncMock(return_value=projection)
    return (
        MetadataQueryService(accounts, providers, projections),
        accounts,
        providers,
        projections,
        provider,
        projection,
    )


@pytest.mark.asyncio
async def test_complete_index_answers_exact_page_after_provider_state_qualification() -> None:
    service, _accounts, providers, _projections, provider, projection = _service("managed")
    projection.read_complete.return_value = [_email(1), _email(3), _email(2, "blocked@example.test")]

    response = await service.execute(ListEmailMetadataQuery(account_name="work", page_size=1))

    assert response.total == 2
    assert [email.email_id for email in response.emails] == ["3"]
    projection.read_complete.assert_awaited_once()
    provider.mailbox_snapshot.assert_not_awaited()
    provider.list_metadata.assert_not_awaited()
    providers.open.assert_called_once_with("work", expected_mode="managed")


@pytest.mark.asyncio
async def test_unsupported_filter_uses_application_owned_provider_fallback_without_index_probe() -> None:
    service, _accounts, _providers, projections, provider, _projection = _service()
    provider.list_metadata.return_value = _fallback_response("needle")

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="needle"))

    assert response.subject == "needle"
    provider.list_metadata.assert_awaited_once()
    provider.mailbox_state.assert_not_awaited()
    projections.open.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["qualification", "refresh"])
async def test_optional_index_provider_probe_failure_preserves_bounded_fallback(failure_stage: str) -> None:
    service, _accounts, providers, _projections, provider, _projection = _service()
    if failure_stage == "qualification":
        provider.mailbox_state.side_effect = RuntimeError("STATUS unsupported")
    else:
        provider.mailbox_snapshot.side_effect = RuntimeError("LIST unsupported")

    response = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert response.total == 0
    provider.list_metadata.assert_awaited_once()
    assert providers.open.call_count >= 2


@pytest.mark.asyncio
async def test_complete_provider_snapshot_is_persisted_and_returns_compatible_exact_result() -> None:
    service, _accounts, _providers, _projections, provider, projection = _service()
    snapshot = MailboxMetadataSnapshot(
        state=MailboxState(7, 4, 3),
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=["\\Inbox"]),
        emails=(_email(1), _email(2), _email(3)),
        complete=True,
        observed_at=NOW,
    )
    provider.mailbox_snapshot.return_value = snapshot

    response = await service.execute(ListEmailMetadataQuery(account_name="work", page=2, page_size=2, order="desc"))

    assert response.total == 3
    assert [email.email_id for email in response.emails] == ["1"]
    projection.write_snapshot.assert_awaited_once_with("INBOX", snapshot)
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "managed"])
async def test_projection_write_failure_preserves_validated_provider_evidence(mode: str) -> None:
    service, _accounts, _providers, _projections, provider, projection = _service(mode)
    provider.mailbox_snapshot.return_value = MailboxMetadataSnapshot(
        state=MailboxState(7, 2, 1),
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(_email(1),),
        complete=True,
        observed_at=NOW,
    )
    projection.write_snapshot.side_effect = MetadataProjectionError("write failed with private path")

    response = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert response.total == 1
    assert [email.email_id for email in response.emails] == ["1"]
    assert response.warnings == ["projection_write_failed"]
    assert "private path" not in response.model_dump_json()
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        MailboxMetadataSnapshot(
            state=MailboxState(7, 2, 2),
            mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
            emails=(_email(1),),
            complete=True,
            observed_at=NOW,
        ),
        MailboxMetadataSnapshot(
            state=MailboxState(7, 2, 1),
            mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
            emails=(_email(2),),
            complete=True,
            observed_at=NOW,
        ),
        MailboxMetadataSnapshot(
            state=MailboxState(7, 3, 2),
            mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
            emails=(_email(1), _email(1)),
            complete=True,
            observed_at=NOW,
        ),
        MailboxMetadataSnapshot(
            state=MailboxState(7, 2, 1),
            mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
            emails=({**_email(1), "_internal_date": None},),
            complete=True,
            observed_at=NOW,
        ),
    ],
)
async def test_inconsistent_provider_snapshot_is_never_returned_or_persisted(
    snapshot: MailboxMetadataSnapshot,
) -> None:
    service, _accounts, _providers, _projections, provider, projection = _service()
    provider.mailbox_snapshot.return_value = snapshot

    with pytest.raises(MetadataProviderObservationError):
        await service.execute(ListEmailMetadataQuery(account_name="work"))
    projection.write_snapshot.assert_not_awaited()
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_snapshot_is_honest_and_falls_back_for_exact_total() -> None:
    service, _accounts, _providers, _projections, provider, projection = _service()
    provider.mailbox_snapshot.return_value = MailboxMetadataSnapshot(
        state=MailboxState(7, 1002, 1001),
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=tuple(_email(uid) for uid in range(2, 1002)),
        complete=False,
        observed_at=NOW,
    )

    response = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert response.total == 0
    projection.write_snapshot.assert_awaited_once()
    provider.list_metadata.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_index_open_failure_degrades_only_inside_query_service() -> None:
    service, _accounts, _providers, projections, provider, _projection = _service()
    projections.open.side_effect = MetadataProjectionError("unavailable")

    response = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert response.total == 0
    provider.list_metadata.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_index_open_failure_fails_closed_without_provider_fallback() -> None:
    service, _accounts, _providers, projections, provider, _projection = _service("managed")
    projections.open.side_effect = MetadataProjectionError("unavailable")

    with pytest.raises(MetadataProjectionError, match="unavailable"):
        await service.execute(ListEmailMetadataQuery(account_name="work"))

    provider.list_metadata.assert_not_awaited()
    provider.mailbox_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_index_read_failure_degrades_but_managed_read_failure_fails_closed() -> None:
    legacy, _accounts, _providers, _projections, legacy_provider, legacy_projection = _service()
    legacy_projection.read_complete.side_effect = MetadataProjectionError("read failed")
    assert (await legacy.execute(ListEmailMetadataQuery(account_name="work"))).total == 0
    legacy_provider.list_metadata.assert_awaited_once()

    managed, _accounts, _providers, _projections, managed_provider, managed_projection = _service("managed")
    managed_projection.read_complete.side_effect = MetadataProjectionError("read failed")
    with pytest.raises(MetadataProjectionError, match="read failed"):
        await managed.execute(ListEmailMetadataQuery(account_name="work"))
    managed_provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, message",
    [
        (ListEmailMetadataQuery(account_name="work", page=0), "page must be at least 1"),
        (ListEmailMetadataQuery(account_name="work", page_size=0), "page_size must be between 1 and 100"),
        (ListEmailMetadataQuery(account_name="work", page_size=101), "page_size must be between 1 and 100"),
    ],
)
async def test_query_bounds_are_enforced_before_account_or_provider_access(
    query: ListEmailMetadataQuery,
    message: str,
) -> None:
    service, accounts, providers, _projections, _provider, _projection = _service()
    with pytest.raises(ValueError, match=message):
        await service.execute(query)
    accounts.resolve.assert_not_called()
    providers.open.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        ListEmailMetadataQuery(account_name="work\x7f"),
        ListEmailMetadataQuery(account_name="work", mailbox="INBOX\x00"),
        ListEmailMetadataQuery(account_name="work", subject="subject\x1f"),
        ListEmailMetadataQuery(account_name="work", from_address="from@example.test\x7f"),
        ListEmailMetadataQuery(account_name="work", to_address="to@example.test\x00"),
        ListEmailMetadataQuery(account_name="work", body="body\x1f"),
        ListEmailMetadataQuery(account_name="work", text="text\x7f"),
    ],
)
def test_metadata_controlled_query_fields_reject_c0_and_del(query: ListEmailMetadataQuery) -> None:
    with pytest.raises(ValueError, match="control characters"):
        query.validate()


def test_metadata_text_query_uses_utf8_byte_limit() -> None:
    ListEmailMetadataQuery(
        account_name="work",
        text="é" * (APPLICATION_LIMITS.query_bytes // 2),
    ).validate()

    with pytest.raises(ValueError, match="exceeds"):
        ListEmailMetadataQuery(
            account_name="work",
            text="é" * (APPLICATION_LIMITS.query_bytes // 2) + "a",
        ).validate()


@pytest.mark.asyncio
@pytest.mark.parametrize(("ceiling_delta", "valid"), [(1, True), (0, True), (-1, False)])
async def test_metadata_serialized_result_limit_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    ceiling_delta: int,
    valid: bool,
) -> None:
    service, _accounts, _providers, _projections, provider, _projection = _service()
    response = _fallback_response("needle")
    provider.list_metadata.return_value = response
    serialized_size = len(response.model_dump_json().encode("utf-8"))
    monkeypatch.setattr(
        limits_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, serialized_response_bytes=serialized_size + ceiling_delta),
    )

    if valid:
        assert await service.execute(ListEmailMetadataQuery(account_name="work", subject="needle")) == response
    else:
        with pytest.raises(MetadataProviderObservationError, match="serialized result"):
            await service.execute(ListEmailMetadataQuery(account_name="work", subject="needle"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("header_delta", "valid"), [(-1, True), (0, True), (1, False)])
async def test_metadata_aggregate_header_limit_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    header_delta: int,
    valid: bool,
) -> None:
    service, _accounts, _providers, _projections, provider, _projection = _service()
    email_dict = _email(1)
    response = EmailMetadataPageResponse(
        page=1,
        page_size=1,
        before=None,
        since=None,
        subject="needle",
        emails=[EmailMetadata.from_email(email_dict)],
        total=1,
    )
    provider.list_metadata.return_value = response
    item = response.emails[0]
    header_size = (
        len(item.email_id.encode("utf-8"))
        + len((item.message_id or "").encode("utf-8"))
        + len(item.subject.encode("utf-8"))
        + len(item.sender.encode("utf-8"))
        + sum(len(value.encode("utf-8")) for value in item.recipients)
        + sum(len(value.encode("utf-8")) for value in item.attachments)
    )
    monkeypatch.setattr(
        metadata_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, aggregate_header_bytes=header_size - header_delta),
    )

    if valid:
        assert (await service.execute(ListEmailMetadataQuery(account_name="work", subject="needle"))).total == 1
    else:
        with pytest.raises(MetadataProviderObservationError, match="aggregate size"):
            await service.execute(ListEmailMetadataQuery(account_name="work", subject="needle"))


@pytest.mark.asyncio
async def test_metadata_provider_fallback_has_application_deadline(monkeypatch) -> None:
    monkeypatch.setattr(
        metadata_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, provider_timeout_seconds=0.001),
    )
    service, _accounts, _providers, _projections, provider, _projection = _service()

    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    provider.list_metadata.side_effect = hang

    with pytest.raises(MetadataProviderError, match="timed out"):
        await service.execute(ListEmailMetadataQuery(account_name="work", subject="needle"))
