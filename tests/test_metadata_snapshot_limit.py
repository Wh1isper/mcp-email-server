from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from mcp_email_server.adapters.metadata import ClassicMetadataProvider
from mcp_email_server.application.limits import metadata_snapshot_row_limit
from mcp_email_server.application.metadata import (
    ListEmailMetadataQuery,
    MailboxInfo,
    MailboxMetadataSnapshot,
    MailboxState,
    MailboxUidDelta,
    MetadataAccountSnapshot,
    MetadataIncrementalBaseline,
    MetadataProviderAccess,
    MetadataProviderError,
    MetadataQueryService,
)
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.emails.models import EmailMetadataPageResponse
from mcp_email_server.imap_keywords import ImapKeywordRegistry
from mcp_email_server.log import logger

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


def _email(uid: int) -> dict[str, object]:
    return {
        "email_id": str(uid),
        "message_id": f"<{uid}@example.test>",
        "subject": f"Subject {uid}",
        "from": "allowed@example.test",
        "to": ["user@example.test"],
        "date": NOW,
        "attachments": [],
        "_internal_date": NOW,
        "_flags": [],
    }


def _client() -> EmailClient:
    return EmailClient(
        EmailServer(
            user_name="user@example.test",
            password=SecretStr("secret"),
            host="imap.example.test",
            port=993,
        ),
        sender="User <user@example.test>",
    )


def _imap(count: int) -> AsyncMock:
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.logout.return_value = ("BYE", [])
    imap.list.return_value = ("OK", [])
    imap.select.return_value = ("OK", [])
    imap.status.return_value = (
        "OK",
        [f"INBOX (UIDVALIDITY 77 UIDNEXT {count + 1} MESSAGES {count})".encode()],
    )
    imap.uid_search.return_value = (
        "OK",
        [b" ".join(str(uid).encode() for uid in range(1, count + 1))],
    )
    return imap


def _service() -> tuple[MetadataQueryService, MagicMock, MagicMock]:
    account = MetadataAccountSnapshot(
        account_name="work",
        mode="legacy",
        allowed_senders=("allowed@example.test",),
        tag_registry=ImapKeywordRegistry(),
    )
    accounts = MagicMock()
    accounts.resolve.return_value = account
    provider = MagicMock()
    provider.list_metadata = AsyncMock(
        return_value=EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[],
            total=0,
        )
    )
    provider.mailbox_state = AsyncMock(return_value=MailboxState(7, 4, 3))
    provider.mailbox_snapshot = AsyncMock()
    provider.mailbox_uid_delta = AsyncMock()
    provider.flags_for = AsyncMock(side_effect=lambda _mailbox, email_ids: {uid: [] for uid in email_ids})
    providers = MagicMock()
    providers.open.return_value = MetadataProviderAccess(account=account, provider=provider)
    projection = MagicMock()
    projection.read_complete = AsyncMock(return_value=None)
    projection.read_incremental_baseline = AsyncMock(return_value=None)
    projection.read_partial_emails = AsyncMock(return_value=None)
    projection.write_snapshot = AsyncMock()
    projections = MagicMock()
    projections.open = AsyncMock(return_value=projection)
    return MetadataQueryService(accounts, providers, projections), provider, projection


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 1_000),
        ("1000", 1_000),
        ("10000", 10_000),
        ("100000", 100_000),
    ],
)
def test_metadata_snapshot_row_limit_accepts_default_and_larger_bounds(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
    expected: int,
) -> None:
    monkeypatch.delenv("MCP_EMAIL_SERVER_METADATA_SNAPSHOT_ROWS", raising=False)
    if raw is not None:
        monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_SNAPSHOT_ROWS", raw)

    assert metadata_snapshot_row_limit() == expected


@pytest.mark.parametrize("raw", ["0", "100001", "-1", "10_000", "1000.0", "many", "", "  "])
def test_metadata_snapshot_row_limit_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_SNAPSHOT_ROWS", raw)

    with pytest.raises(ValueError, match="MCP_EMAIL_SERVER_METADATA_SNAPSHOT_ROWS"):
        metadata_snapshot_row_limit()


@pytest.mark.asyncio
async def test_snapshot_window_follows_configured_row_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_SNAPSHOT_ROWS", "3")
    client = _client()
    imap = _imap(3)
    metadata = {
        str(uid): {
            "email_id": str(uid),
            "subject": str(uid),
            "from": "sender@example.test",
            "to": [],
            "date": NOW,
            "attachments": [],
            "_flags": [],
        }
        for uid in range(1, 4)
    }
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value=dict.fromkeys(["3", "2", "1"], NOW))),
        patch.object(client, "_batch_fetch_headers", AsyncMock(return_value=metadata)),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is True
    assert [email["email_id"] for email in snapshot.emails] == ["3", "2", "1"]


@pytest.mark.asyncio
async def test_snapshot_above_configured_window_stays_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_SNAPSHOT_ROWS", "3")
    client = _client()
    imap = _imap(4)
    selected = ["4", "3", "2"]
    metadata = {
        uid: {
            "email_id": uid,
            "subject": uid,
            "from": "sender@example.test",
            "to": [],
            "date": NOW,
            "attachments": [],
            "_flags": [],
        }
        for uid in selected
    }
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value=dict.fromkeys(selected, NOW))),
        patch.object(client, "_batch_fetch_headers", AsyncMock(return_value=metadata)),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is False
    assert [email["email_id"] for email in snapshot.emails] == selected


@pytest.mark.asyncio
async def test_uid_delta_fetches_only_uids_above_the_cached_high_water_mark() -> None:
    client = _client()
    imap = _imap(4)
    imap.uid_search.return_value = ("OK", [b"3 4"])
    selected = ["3", "4"]
    metadata = {
        uid: {
            "email_id": uid,
            "subject": uid,
            "from": "sender@example.test",
            "to": [],
            "date": NOW,
            "attachments": [],
            "_flags": ["\\Seen"],
        }
        for uid in selected
    }
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value=dict.fromkeys(selected, NOW))) as dates,
        patch.object(client, "_batch_fetch_headers", AsyncMock(return_value=metadata)) as headers,
    ):
        delta = await client.get_mailbox_uid_delta("INBOX", 2, maximum_new_messages=10)

    assert delta.resumable is True
    assert delta.uidvalidity_changed is False
    assert [email["email_id"] for email in delta.emails] == selected
    imap.uid_search.assert_awaited_once_with("UID", "3:*", charset=None)
    dates.assert_awaited_once_with(imap, selected)
    headers.assert_awaited_once()
    assert headers.await_args.args[1] == selected


@pytest.mark.asyncio
async def test_uid_delta_does_not_fetch_headers_when_the_new_range_exceeds_the_window() -> None:
    client = _client()
    imap = _imap(4)
    imap.uid_search.return_value = ("OK", [b"3 4"])
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock()) as dates,
        patch.object(client, "_batch_fetch_headers", AsyncMock()) as headers,
    ):
        delta = await client.get_mailbox_uid_delta("INBOX", 2, maximum_new_messages=1)

    assert delta.resumable is False
    assert delta.emails == ()
    dates.assert_not_awaited()
    headers.assert_not_awaited()


@pytest.mark.asyncio
async def test_uid_delta_discards_rows_when_uidvalidity_changes() -> None:
    client = _client()
    imap = _imap(2)
    imap.status.side_effect = [
        ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 3 MESSAGES 2)"]),
        ("OK", [b"INBOX (UIDVALIDITY 78 UIDNEXT 3 MESSAGES 2)"]),
    ]
    imap.uid_search.return_value = ("OK", [b"2"])
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"2": NOW})),
        patch.object(client, "_batch_fetch_headers", AsyncMock(return_value={"2": _email(2)})),
    ):
        delta = await client.get_mailbox_uid_delta("INBOX", 1, maximum_new_messages=10)

    assert delta.uidvalidity_changed is True
    assert delta.resumable is False
    assert delta.emails == ()
    assert delta.state.uidvalidity == 78


def _raise_snapshot_timeout() -> None:
    raise TimeoutError("metadata snapshot timed out")


@pytest.mark.asyncio
async def test_refresh_failure_logs_the_causal_exception_and_traceback() -> None:
    service, provider, projection = _service()
    try:
        _raise_snapshot_timeout()
    except TimeoutError as exc:
        diagnostic = exc
    provider.mailbox_snapshot.side_effect = MetadataProviderError(
        "provider_failure: metadata provider request failed",
        diagnostic=diagnostic,
    )
    records: list[object] = []
    sink_id = logger.add(lambda message: records.append(message), level="WARNING")
    try:
        response = await service.execute(ListEmailMetadataQuery(account_name="work"))
    finally:
        logger.remove(sink_id)

    assert response.total == 0
    logged = "\n".join(str(record) for record in records)
    assert "Metadata projection refresh failed" in logged
    assert "TimeoutError" in logged
    assert "metadata snapshot timed out" not in logged
    assert "_raise_snapshot_timeout:" in logged
    assert "Traceback" in logged
    assert "metadata snapshot timed out" not in response.model_dump_json()
    projection.write_snapshot.assert_not_awaited()
    provider.list_metadata.assert_awaited_once()


@pytest.mark.asyncio
async def test_incremental_sync_appends_uids_above_the_cached_high_water_mark() -> None:
    service, provider, projection = _service()
    provider.mailbox_state.return_value = MailboxState(7, 4, 3)
    projection.read_incremental_baseline.return_value = MetadataIncrementalBaseline(
        state=MailboxState(7, 3, 2),
        high_uid=2,
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(_email(1), _email(2)),
    )
    provider.mailbox_uid_delta.return_value = MailboxUidDelta(
        state=MailboxState(7, 4, 3),
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(_email(3),),
        observed_at=NOW,
        uidvalidity_changed=False,
        resumable=True,
    )

    response = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert response.total == 3
    assert [email.email_id for email in response.emails] == ["3", "2", "1"]
    provider.mailbox_uid_delta.assert_awaited_once_with("INBOX", 2, maximum_new_messages=998)
    provider.mailbox_snapshot.assert_not_awaited()
    provider.list_metadata.assert_not_awaited()
    written = projection.write_snapshot.await_args.args[1]
    assert written.complete is True
    assert [email["email_id"] for email in written.emails] == ["1", "2", "3"]
    provider.flags_for.assert_awaited_once()


@pytest.mark.asyncio
async def test_incremental_sync_falls_back_when_messages_were_deleted() -> None:
    service, provider, projection = _service()
    provider.mailbox_state.return_value = MailboxState(7, 4, 1)
    projection.read_incremental_baseline.return_value = MetadataIncrementalBaseline(
        state=MailboxState(7, 3, 2),
        high_uid=2,
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(_email(1), _email(2)),
    )
    provider.mailbox_uid_delta.return_value = MailboxUidDelta(
        state=MailboxState(7, 4, 1),
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(),
        observed_at=NOW,
        uidvalidity_changed=False,
        resumable=True,
    )
    provider.mailbox_snapshot.return_value = MailboxMetadataSnapshot(
        state=MailboxState(7, 4, 1),
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(_email(1),),
        complete=True,
        observed_at=NOW,
    )

    response = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert response.total == 1
    provider.mailbox_snapshot.assert_awaited_once()
    written = projection.write_snapshot.await_args.args[1]
    assert [email["email_id"] for email in written.emails] == ["1"]


@pytest.mark.asyncio
async def test_snapshot_adapter_keeps_the_causal_exception_off_the_public_message() -> None:
    handler = MagicMock()
    handler.incoming_client.get_mailbox_metadata_snapshot = AsyncMock(side_effect=TimeoutError("slow mailbox"))
    provider = ClassicMetadataProvider(handler)

    with pytest.raises(MetadataProviderError, match="provider_failure") as exc_info:
        await provider.mailbox_snapshot("INBOX")

    assert "slow mailbox" not in str(exc_info.value)
    assert isinstance(exc_info.value.diagnostic, TimeoutError)
    assert "slow mailbox" in str(exc_info.value.diagnostic)


@pytest.mark.asyncio
async def test_refresh_login_timeout_does_not_log_credentials_or_exception_text() -> None:
    service, provider, projection = _service()
    client = _client()
    client.email_server.password = SecretStr("synthetic-refresh-password")
    imap = _imap(1)
    imap.login.side_effect = TimeoutError("synthetic-private-provider-detail")
    handler = MagicMock()
    handler.incoming_client = client
    provider.mailbox_snapshot.side_effect = ClassicMetadataProvider(handler).mailbox_snapshot
    records: list[str] = []
    # Even a diagnostic sink must not receive the raw exception or its locals.
    sink_id = logger.add(lambda message: records.append(str(message)), diagnose=True, level="DEBUG")
    try:
        with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
            response = await service.execute(ListEmailMetadataQuery(account_name="work"))
    finally:
        logger.remove(sink_id)

    logged = "\n".join(records)
    assert "TimeoutError" in logged
    assert "_imap_login:" in logged
    assert "synthetic-refresh-password" not in logged
    assert "synthetic-private-provider-detail" not in logged
    assert "user@example.test" not in logged
    assert response.total == 0
    projection.write_snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_status",
    [
        b"INBOX (UIDVALIDITY 77 UIDNEXT 5 MESSAGES 3)",  # Expunge UID 1 and append UID 4.
        b"INBOX (UIDVALIDITY 77 UIDNEXT 5 MESSAGES 4)",  # Append during FETCH.
        b"INBOX (UIDVALIDITY 77 UIDNEXT 4 MESSAGES 2)",  # Expunge during FETCH.
    ],
)
async def test_uid_delta_rejects_mailbox_changes_during_fetch(final_status: bytes) -> None:
    client = _client()
    imap = _imap(3)
    imap.status.side_effect = [
        ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 4 MESSAGES 3)"]),
        ("OK", [final_status]),
    ]
    imap.uid_search.return_value = ("OK", [b"3"])
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"3": NOW})),
        patch.object(client, "_batch_fetch_headers", AsyncMock(return_value={"3": _email(3)})),
    ):
        delta = await client.get_mailbox_uid_delta("INBOX", 2, maximum_new_messages=10)

    assert delta.resumable is False
    assert delta.uidvalidity_changed is False


@pytest.mark.asyncio
async def test_incremental_replacement_race_refreshes_instead_of_publishing_false_complete() -> None:
    service, provider, projection = _service()
    client = _client()
    imap = _imap(3)
    imap.status.side_effect = [
        ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 4 MESSAGES 3)"]),
        ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 5 MESSAGES 3)"]),
    ]
    imap.uid_search.return_value = ("OK", [b"3"])
    provider.mailbox_state.return_value = MailboxState(77, 4, 3)
    projection.read_incremental_baseline.return_value = MetadataIncrementalBaseline(
        state=MailboxState(77, 3, 2),
        high_uid=2,
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(_email(1), _email(2)),
    )
    provider.mailbox_uid_delta.side_effect = client.get_mailbox_uid_delta
    provider.mailbox_snapshot.return_value = MailboxMetadataSnapshot(
        state=MailboxState(77, 5, 3),
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(_email(2), _email(3), _email(4)),
        complete=True,
        observed_at=NOW,
    )
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"3": NOW})),
        patch.object(client, "_batch_fetch_headers", AsyncMock(return_value={"3": _email(3)})),
    ):
        response = await service.execute(ListEmailMetadataQuery(account_name="work", page_size=1))

    provider.mailbox_snapshot.assert_awaited_once()
    assert [email.email_id for email in response.emails] == ["4"]
    written = projection.write_snapshot.await_args.args[1]
    assert {email["email_id"] for email in written.emails} == {"2", "3", "4"}
    assert written.complete is True
