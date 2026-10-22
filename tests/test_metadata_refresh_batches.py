from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from mcp_email_server.application import metadata as metadata_module
from mcp_email_server.application.limits import (
    APPLICATION_LIMITS,
    metadata_header_budget_bytes,
    metadata_refresh_batch_limit,
    metadata_refresh_batch_size,
    metadata_refresh_timeout_seconds,
)
from mcp_email_server.application.metadata import (
    ListEmailMetadataQuery,
    MailboxInfo,
    MailboxMetadataSnapshot,
    MailboxState,
    MailboxUidDelta,
    MetadataAccountSnapshot,
    MetadataIncrementalBaseline,
    MetadataProviderAccess,
    MetadataQueryService,
)
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.emails.models import EmailMetadataPageResponse
from mcp_email_server.imap_keywords import ImapKeywordRegistry

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


def _clear_refresh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS",
        "MCP_EMAIL_SERVER_METADATA_HEADER_BUDGET_BYTES",
        "MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("timeout", "budget", "batch", "expected_timeout", "expected_budget", "expected_batch"),
    [
        (None, None, None, 60.0, 4 * 1024 * 1024, 100),
        ("90", "8388608", "50", 90.0, 8_388_608, 50),
        ("3600", "67108864", "1000", 3600.0, 67_108_864, 1000),
        ("1.5", "65536", "1", 1.5, 65_536, 1),
    ],
)
def test_refresh_limits_accept_defaults_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
    timeout: str | None,
    budget: str | None,
    batch: str | None,
    expected_timeout: float,
    expected_budget: int,
    expected_batch: int,
) -> None:
    _clear_refresh_env(monkeypatch)
    if timeout is not None:
        monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS", timeout)
    if budget is not None:
        monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_HEADER_BUDGET_BYTES", budget)
    if batch is not None:
        monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", batch)

    assert metadata_refresh_timeout_seconds() == expected_timeout
    assert metadata_header_budget_bytes() == expected_budget
    assert metadata_refresh_batch_size() == expected_batch


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS", "0"),
        ("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS", "3600.1"),
        ("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS", "inf"),
        ("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS", "60s"),
        ("MCP_EMAIL_SERVER_METADATA_HEADER_BUDGET_BYTES", "65535"),
        ("MCP_EMAIL_SERVER_METADATA_HEADER_BUDGET_BYTES", "67108865"),
        ("MCP_EMAIL_SERVER_METADATA_HEADER_BUDGET_BYTES", "4_194_304"),
        ("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", "0"),
        ("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", "1001"),
    ],
)
def test_refresh_limits_reject_invalid_values(monkeypatch: pytest.MonkeyPatch, name: str, raw: str) -> None:
    _clear_refresh_env(monkeypatch)
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        if name.endswith("TIMEOUT_SECONDS"):
            metadata_refresh_timeout_seconds()
        elif name.endswith("BUDGET_BYTES"):
            metadata_header_budget_bytes()
        else:
            metadata_refresh_batch_size()


def test_batch_limit_shrinks_to_the_header_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_refresh_env(monkeypatch)
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", "1000")
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_HEADER_BUDGET_BYTES", "65536")

    assert metadata_refresh_batch_limit() == 1


def _raw_header(size: int) -> bytes:
    prefix = b"From: sender@example.test\r\nSubject: n\r\n\r\n"
    return prefix + b"x" * (size - len(prefix))


def _header_response(uid: str, raw: bytes) -> tuple[str, list[bytes | bytearray]]:
    return (
        "OK",
        [
            f"1 FETCH (UID {uid} FLAGS () BODY[HEADER] {{{len(raw)}}}".encode(),
            bytearray(raw),
            b")",
        ],
    )


@pytest.mark.asyncio
async def test_refresh_fetches_newest_first_and_resets_the_header_budget_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_refresh_env(monkeypatch)
    # Three 60 KiB headers exceed a 100 KiB budget if it accumulates, and fit when each UID is its own batch.
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_HEADER_BUDGET_BYTES", "100000")
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", "1")
    client = _client()
    imap = _imap(3)
    requested: list[str] = []

    async def fetch(_command: str, uid_list: str, _item: str) -> tuple[str, list[bytes | bytearray]]:
        requested.append(uid_list)
        return _header_response(uid_list, _raw_header(60_000))

    imap.uid.side_effect = fetch
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(side_effect=lambda _imap, uids: dict.fromkeys(uids, NOW))),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert requested == ["3", "2", "1"]
    assert snapshot.complete is True
    assert [email["email_id"] for email in snapshot.emails] == ["3", "2", "1"]


@pytest.mark.asyncio
async def test_header_budget_stops_one_batch_and_keeps_newer_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_refresh_env(monkeypatch)
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", "1")
    client = _client()
    imap = _imap(3)
    sizes = {"3": 100, "2": 100, "1": 65_537}

    async def fetch(_command: str, uid_list: str, _item: str) -> tuple[str, list[bytes | bytearray]]:
        return _header_response(uid_list, _raw_header(sizes[uid_list]))

    imap.uid.side_effect = fetch
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(side_effect=lambda _imap, uids: dict.fromkeys(uids, NOW))),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is False
    assert [email["email_id"] for email in snapshot.emails] == ["3", "2"]


@pytest.mark.asyncio
async def test_each_batch_has_its_own_timeout_and_a_failure_keeps_newer_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_refresh_env(monkeypatch)
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", "1")
    client = _client()
    imap = _imap(3)
    requested: list[list[str]] = []

    async def headers(
        _imap: AsyncMock, uids: list[str], *, include_flags: bool = False
    ) -> dict[str, dict[str, object]]:
        del include_flags
        requested.append(list(uids))
        if uids == ["2"]:
            await asyncio.sleep(3)
        return {uid: _email(int(uid)) for uid in uids}

    started = time.monotonic()
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(side_effect=lambda _imap, uids: dict.fromkeys(uids, NOW))),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")
    elapsed = time.monotonic() - started

    assert elapsed < 2.5
    assert requested == [["3"], ["2"]]
    assert snapshot.complete is False
    assert [email["email_id"] for email in snapshot.emails] == ["3"]


@pytest.mark.asyncio
async def test_retained_partial_rows_resume_newest_first_and_can_become_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_refresh_env(monkeypatch)
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", "1")
    client = _client()
    imap = _imap(3)
    requested: list[list[str]] = []

    fail_oldest = True

    async def headers(
        _imap: AsyncMock, uids: list[str], *, include_flags: bool = False
    ) -> dict[str, dict[str, object]]:
        del include_flags
        requested.append(list(uids))
        if uids == ["1"] and fail_oldest:
            raise TimeoutError("oldest batch timed out")
        return {uid: _email(int(uid)) for uid in uids}

    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(side_effect=lambda _imap, uids: dict.fromkeys(uids, NOW))),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        partial = await client.get_mailbox_metadata_snapshot("INBOX")
        assert partial.complete is False
        assert [email["email_id"] for email in partial.emails] == ["3", "2"]

        requested.clear()
        fail_oldest = False
        resumed = await client.get_mailbox_metadata_snapshot(
            "INBOX", retained_emails=list(partial.emails), retained_uidvalidity=partial.state.uidvalidity
        )

    assert requested == [["1"]]
    assert resumed.complete is True
    assert [email["email_id"] for email in resumed.emails] == ["3", "2", "1"]


@pytest.mark.asyncio
async def test_resume_failure_keeps_the_partial_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_refresh_env(monkeypatch)
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE", "1")
    client = _client()
    imap = _imap(3)

    async def headers(
        _imap: AsyncMock, uids: list[str], *, include_flags: bool = False
    ) -> dict[str, dict[str, object]]:
        del include_flags
        raise TimeoutError("resume batch timed out")

    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(side_effect=lambda _imap, uids: dict.fromkeys(uids, NOW))),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot(
            "INBOX", retained_emails=[_email(3)], retained_uidvalidity=77
        )

    assert snapshot.complete is False
    assert [email["email_id"] for email in snapshot.emails] == ["3"]


@pytest.mark.asyncio
async def test_snapshot_refresh_is_not_wrapped_in_the_shared_provider_timeout() -> None:
    service, provider, projection = _service()
    snapshot = MailboxMetadataSnapshot(
        state=MailboxState(7, 4, 3),
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=[]),
        emails=(_email(3), _email(2), _email(1)),
        complete=True,
        observed_at=NOW,
    )

    async def slow_snapshot(
        mailbox: str, *, retained_emails: tuple[dict[str, object], ...] = (), retained_uidvalidity: int | None = None
    ) -> MailboxMetadataSnapshot:
        del mailbox, retained_emails, retained_uidvalidity
        await asyncio.sleep(0.05)
        return snapshot

    provider.mailbox_snapshot.side_effect = slow_snapshot
    with patch.object(
        metadata_module, "APPLICATION_LIMITS", replace(APPLICATION_LIMITS, provider_timeout_seconds=0.01)
    ):
        response = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert response.total == 3
    provider.list_metadata.assert_not_awaited()
    projection.write_snapshot.assert_awaited_once_with("INBOX", snapshot)


@pytest.mark.asyncio
async def test_partial_snapshot_becomes_complete_and_then_syncs_incrementally() -> None:
    service, provider, projection = _service()
    mailbox = MailboxInfo(name="INBOX", delimiter="/", flags=[])
    partial = MailboxMetadataSnapshot(
        state=MailboxState(7, 4, 3),
        mailbox=mailbox,
        emails=(_email(3), _email(2)),
        complete=False,
        observed_at=NOW,
    )
    provider.mailbox_snapshot.return_value = partial

    first = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert first.total == 0
    provider.list_metadata.assert_awaited_once()
    projection.write_snapshot.assert_awaited_once_with("INBOX", partial)
    assert partial.complete is False
    assert provider.mailbox_snapshot.await_args.kwargs["retained_emails"] == ()

    stored = (_email(3), _email(2))
    projection.read_partial_emails.return_value = stored
    complete = MailboxMetadataSnapshot(
        state=MailboxState(7, 4, 3),
        mailbox=mailbox,
        emails=(_email(3), _email(2), _email(1)),
        complete=True,
        observed_at=NOW,
    )
    provider.mailbox_snapshot = AsyncMock(return_value=complete)
    provider.list_metadata.reset_mock()
    projection.write_snapshot.reset_mock()

    second = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert second.total == 3
    assert [email.email_id for email in second.emails] == ["3", "2", "1"]
    assert provider.mailbox_snapshot.await_args.kwargs["retained_emails"] == stored
    projection.write_snapshot.assert_awaited_once_with("INBOX", complete)
    provider.list_metadata.assert_not_awaited()
    provider.flags_for.assert_awaited_once()

    projection.read_incremental_baseline.return_value = MetadataIncrementalBaseline(
        state=MailboxState(7, 4, 3),
        high_uid=3,
        mailbox=mailbox,
        emails=(_email(1), _email(2), _email(3)),
    )
    provider.mailbox_state.return_value = MailboxState(7, 5, 4)
    provider.mailbox_uid_delta.return_value = MailboxUidDelta(
        state=MailboxState(7, 5, 4),
        mailbox=mailbox,
        emails=(_email(4),),
        observed_at=NOW,
        uidvalidity_changed=False,
        resumable=True,
    )
    provider.mailbox_snapshot.reset_mock()
    provider.flags_for.reset_mock()

    third = await service.execute(ListEmailMetadataQuery(account_name="work"))

    assert third.total == 4
    assert [email.email_id for email in third.emails] == ["4", "3", "2", "1"]
    provider.mailbox_uid_delta.assert_awaited_once()
    provider.mailbox_snapshot.assert_not_awaited()
    projection.read_partial_emails.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_epoch", [None, 7])
async def test_snapshot_refetches_rows_without_a_matching_retained_epoch(retained_epoch: int | None) -> None:
    client = _client()
    imap = _imap(1)
    stale = {**_email(1), "subject": "Old epoch content"}
    headers = AsyncMock(return_value={"1": _email(1)})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"1": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot(
            "INBOX", retained_emails=[stale], retained_uidvalidity=retained_epoch
        )
    headers.assert_awaited_once()
    assert snapshot.complete is True
    assert snapshot.state.uidvalidity == 77
    assert snapshot.emails[0]["subject"] == "Subject 1"


@pytest.mark.asyncio
async def test_service_preserves_retained_epoch_through_the_classic_adapter() -> None:
    from mcp_email_server.adapters.metadata import ClassicMetadataProvider

    service, provider, projection = _service()
    client = _client()
    imap = _imap(1)
    projection.read_partial_emails.return_value = ({**_email(1), "subject": "Old epoch content"},)
    handler = MagicMock()
    handler.incoming_client = client
    provider.mailbox_snapshot.side_effect = ClassicMetadataProvider(handler).mailbox_snapshot
    headers = AsyncMock(return_value={"1": _email(1)})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"1": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        response = await service.execute(ListEmailMetadataQuery(account_name="work"))
    assert provider.mailbox_snapshot.await_args.kwargs["retained_uidvalidity"] == 7
    assert response.emails[0].subject == "Subject 1"
    snapshot = projection.write_snapshot.await_args.args[1]
    assert snapshot.complete is True
    assert snapshot.state.uidvalidity == 77
    headers.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_connection_timeout_cancels_and_closes_the_pending_connection() -> None:
    client = _client()
    imap = MagicMock()
    imap._client_task = asyncio.create_task(asyncio.sleep(60))
    imap.wait_hello_from_server = AsyncMock()
    with (
        patch.object(client, "_imap_connect", return_value=imap),
        patch("mcp_email_server.emails.classic.metadata_refresh_timeout_seconds", return_value=0.01),
    ):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.get_mailbox_metadata_snapshot("INBOX"), timeout=1)
    assert imap._client_task.cancelled()
    imap.protocol.transport.close.assert_called_once()
    imap.wait_hello_from_server.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["batch", "final_state"])
async def test_partial_refresh_logs_only_value_free_diagnostics(failure_stage: str) -> None:
    from io import StringIO

    from mcp_email_server.log import logger

    client = _client()
    imap = _imap(2)
    output = StringIO()
    sink = logger.add(output, level="DEBUG", diagnose=True, backtrace=True)

    async def fail_with_private_data(*args: object, **kwargs: object) -> None:
        password = "synthetic-private-password"  # noqa: S105 - synthetic log-redaction probe
        raise TimeoutError(f"private provider text {password}")

    headers = AsyncMock(return_value={"2": _email(2), "1": _email(1)})
    status = AsyncMock(return_value=MailboxState(77, 3, 2))
    if failure_stage == "batch":
        headers.side_effect = fail_with_private_data
    else:
        status.side_effect = [MailboxState(77, 3, 2), TimeoutError("private provider text")]
    try:
        with (
            patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
            patch.object(client, "_status_mailbox", status),
            patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"1": NOW, "2": NOW})),
            patch.object(client, "_batch_fetch_headers", headers),
        ):
            snapshot = await client.get_mailbox_metadata_snapshot(
                "INBOX", retained_emails=[_email(2)], retained_uidvalidity=77
            )
    finally:
        logger.remove(sink)
    assert snapshot.complete is False
    assert snapshot.emails
    logged = output.getvalue()
    assert "TimeoutError" in logged
    assert "Traceback:" in logged
    assert "private provider text" not in logged
    assert "synthetic-private-password" not in logged
    assert "user@example.test" not in logged


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("mode", ["legacy", "managed"])
async def test_resume_read_failure_obeys_runtime_mode(partial: bool, mode: str) -> None:
    from mcp_email_server.application.metadata import MetadataProjectionError

    service, provider, projection = _service()
    account = service._accounts.resolve.return_value
    account = replace(account, mode=mode)
    service._accounts.resolve.return_value = account
    service._providers.open.return_value = MetadataProviderAccess(account=account, provider=provider)
    error = MetadataProjectionError("projection read failed")
    if partial:
        projection.read_partial_emails.side_effect = error
    else:
        projection.read_incremental_baseline.side_effect = error
    if mode == "managed":
        with pytest.raises(MetadataProjectionError):
            await service.execute(ListEmailMetadataQuery(account_name="work"))
        provider.list_metadata.assert_not_awaited()
    else:
        response = await service.execute(ListEmailMetadataQuery(account_name="work"))
        assert response.total == 0
        provider.list_metadata.assert_awaited_once()
    provider.mailbox_snapshot.assert_not_awaited()
    projection.write_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_delta_rebuilds_from_same_epoch_baseline() -> None:
    service, provider, projection = _service()
    mailbox = MailboxInfo(name="INBOX", delimiter="/", flags=[])
    projection.read_incremental_baseline.return_value = MetadataIncrementalBaseline(
        state=MailboxState(7, 2, 1), high_uid=1, mailbox=mailbox, emails=(_email(1),)
    )
    provider.mailbox_uid_delta.side_effect = TimeoutError("private delta detail")
    snapshot = MailboxMetadataSnapshot(
        state=MailboxState(7, 4, 3),
        mailbox=mailbox,
        emails=(_email(3), _email(2), _email(1)),
        complete=True,
        observed_at=NOW,
    )
    provider.mailbox_snapshot.return_value = snapshot
    response = await service.execute(ListEmailMetadataQuery(account_name="work"))
    assert response.total == 3
    provider.mailbox_snapshot.assert_awaited_once_with("INBOX", retained_emails=(_email(1),), retained_uidvalidity=7)
    provider.list_metadata.assert_not_awaited()
    projection.write_snapshot.assert_awaited_once_with("INBOX", snapshot)
