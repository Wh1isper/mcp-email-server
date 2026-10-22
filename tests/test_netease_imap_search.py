from __future__ import annotations

from datetime import UTC, datetime
from email.header import Header
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr, ValidationError

from mcp_email_server.adapters.metadata import SQLiteMetadataProjection
from mcp_email_server.application.metadata import (
    ListEmailMetadataQuery,
    MailboxMetadataSnapshot,
    MailboxState,
    MailboxUidDelta,
    MetadataAccountSnapshot,
    MetadataIncrementalBaseline,
    MetadataProjectionError,
    MetadataProviderAccess,
    MetadataProviderError,
    MetadataProviderObservationError,
    MetadataQueryService,
    MetadataQueryTooBroadError,
    _header_text_matches,
    _is_header_text_query,
    _joined_header_text,
    _merge_append_only_snapshot,
    _validate_snapshot,
)
from mcp_email_server.config import EmailServer, EmailSettings, imap_search_mode
from mcp_email_server.emails.classic import _MAILBOX_STATE_NOTICES, MAX_IMAP_UID, EmailClient, _uid_belongs_to_epoch
from mcp_email_server.emails.models import EmailMetadataPageResponse, MailboxInfo
from mcp_email_server.imap_keywords import ImapKeywordRegistry
from mcp_email_server.log import logger

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_search_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", raising=False)
    _MAILBOX_STATE_NOTICES.clear()


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


def _imap() -> AsyncMock:
    imap = AsyncMock()
    imap.protocol = None
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.logout.return_value = ("BYE", [])
    imap.list.return_value = ("OK", [])
    imap.select.return_value = ("OK", [b"2 EXISTS"])
    imap.uid_search.return_value = ("OK", [b""])
    imap.uid.return_value = ("OK", [b"1 FETCH (UID 10)", b"2 FETCH (UID 12)"])
    return imap


def _row(
    uid: str, subject: str, *, to: list[str] | None = None, to_addresses: list[str] | None = None
) -> dict[str, object]:
    row: dict[str, object] = {
        "email_id": uid,
        "message_id": f"<{uid}@example.test>",
        "subject": subject,
        "from": "allowed@example.test",
        "to": ["用户 <user@163.com>"] if to is None else to,
        "date": NOW,
        "attachments": [],
        "_flags": [],
        "_internal_date": NOW,
    }
    if to_addresses is not None:
        row["to_addresses"] = to_addresses
    return row


def _mailbox_snapshot(
    emails: tuple[dict[str, object], ...],
    *,
    state: MailboxState | None = None,
    complete: bool = True,
) -> MailboxMetadataSnapshot:
    return MailboxMetadataSnapshot(
        state=state or MailboxState(77, 13, len(emails)),
        mailbox=MailboxInfo(name="INBOX", delimiter="", flags=[]),
        emails=emails,
        complete=complete,
        observed_at=NOW,
    )


def _service(
    host: str = "",
    mode: str | None = None,
) -> tuple[MetadataQueryService, MagicMock, MagicMock]:
    account = MetadataAccountSnapshot(
        account_name="work",
        mode="legacy",
        allowed_senders=("allowed@example.test",),
        tag_registry=ImapKeywordRegistry(),
        imap_host=host,
        imap_search_mode=mode,  # type: ignore[arg-type]
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
    provider.mailbox_state = AsyncMock(return_value=MailboxState(77, 13, 2))
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


def test_header_search_mode_defaults_to_server_and_accepts_local(monkeypatch: pytest.MonkeyPatch) -> None:
    assert imap_search_mode() == "server"

    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", " local ")
    assert imap_search_mode() == "local"
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", "SERVER")
    assert imap_search_mode() == "server"

    account = EmailSettings(
        account_name="work",
        full_name="Work",
        email_address="user@example.test",
        imap_search_mode="local",
        incoming=EmailServer(
            user_name="user@example.test",
            password=SecretStr("secret"),
            host="imap.163.com",
            port=993,
        ),
    )
    assert account.imap_search_mode == "local"


@pytest.mark.parametrize("raw", ["auto", "client", "snapshot", ""])
def test_removed_search_modes_are_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", raw)

    with pytest.raises(ValueError, match=r"one of: server, local"):
        imap_search_mode()
    with pytest.raises(ValidationError):
        EmailSettings(
            account_name="work",
            full_name="Work",
            email_address="user@example.test",
            imap_search_mode=raw,  # type: ignore[arg-type]
            incoming=EmailServer(
                user_name="user@example.test",
                password=SecretStr("secret"),
                host="imap.example.test",
                port=993,
            ),
        )


def test_header_text_matches_decoded_subjects_and_ignores_cc() -> None:
    utf8_subject = Header("开发", "utf-8").encode()
    gb_subject = Header("开发者", "gb2312").encode()
    query = ListEmailMetadataQuery(account_name="work", subject="开发")

    assert _header_text_matches(query, {"subject": utf8_subject, "from": "", "to": []})
    assert _header_text_matches(query, {"subject": "开发进度", "from": "", "to": []})
    assert _header_text_matches(
        ListEmailMetadataQuery(account_name="work", subject="发者"),
        {"subject": gb_subject, "from": "", "to": []},
    )
    assert _header_text_matches(
        ListEmailMetadataQuery(account_name="work", from_address="dev"),
        {"subject": "Hello", "from": "Developer <dev@example.com>", "to": []},
    )
    assert not _header_text_matches(query, {"subject": "Hello", "from": "", "to": []})

    to_query = ListEmailMetadataQuery(account_name="work", to_address="用户")
    assert _header_text_matches(to_query, {"subject": "Hello", "from": "", "to_addresses": ["用户 <user@163.com>"]})
    # Display recipients include Cc. That list must not satisfy To, and a row
    # that never stored a distinct To field cannot prove a To match.
    assert not _header_text_matches(
        ListEmailMetadataQuery(account_name="work", to_address="cc@example.com"),
        {"subject": "Hello", "from": "", "to": ["cc@example.com"], "to_addresses": ["to@example.com"]},
    )
    assert not _header_text_matches(
        ListEmailMetadataQuery(account_name="work", to_address="cc@example.com"),
        {"subject": "Hello", "from": "", "to": ["cc@example.com"]},
    )

    client = _client()
    raw = (
        f"Subject: {Header('开发', 'utf-8').encode()}\r\n"
        "From: Dev <dev@example.com>\r\n"
        "To: to@example.com\r\n"
        "Cc: cc@example.com\r\n"
        "\r\n"
    ).encode()
    parsed = client._parse_headers("4", raw)
    parsed_body = client._parse_email_data(raw + b"body", email_id="4")

    assert parsed is not None
    assert _header_text_matches(query, parsed)
    assert "cc@example.com" in parsed["to"]
    assert parsed["to_addresses"] == ["to@example.com"]
    assert parsed_body["to_addresses"] == ["to@example.com"]
    assert "cc@example.com" in parsed_body["to"]
    assert _header_text_matches(ListEmailMetadataQuery(account_name="work", to_address="to@example.com"), parsed)
    assert not _header_text_matches(ListEmailMetadataQuery(account_name="work", to_address="cc@example.com"), parsed)
    assert not _header_text_matches(
        ListEmailMetadataQuery(account_name="work", from_address="missing"),
        {"subject": "Hello", "from": "Dev <dev@example.com>", "to": []},
    )
    assert _joined_header_text(None) == ""
    assert _joined_header_text(["=?x-no-such-codec?q?abc?=", "plain"]) == "=?x-no-such-codec?q?abc?=\nplain"
    assert not _is_header_text_query(ListEmailMetadataQuery(account_name="work", subject="a", semantic_tags=("todo",)))
    assert not _is_header_text_query(
        ListEmailMetadataQuery(account_name="work", subject="a", provider_keywords=("keyword",))
    )
    assert not _uid_belongs_to_epoch({"email_id": None}, 5)
    assert not _uid_belongs_to_epoch({"email_id": "nope"}, 5)
    assert not _uid_belongs_to_epoch({"email_id": "0"}, 5)
    assert not _uid_belongs_to_epoch({"email_id": str(MAX_IMAP_UID + 1)}, None)
    assert not _uid_belongs_to_epoch({"email_id": "8"}, 8)
    assert _uid_belongs_to_epoch({"email_id": "8"}, None)


@pytest.mark.asyncio
async def test_netease_protocol_uses_uid_inventory_and_keeps_uid_gaps() -> None:
    """Sanitized NetEase-shaped responses: no credentials or message bodies.

    STATUS and SELECT return MESSAGES and UIDVALIDITY and omit UIDNEXT.
    UID SEARCH is empty. UID FETCH shows a gap (10, 12, 40), so the next
    assigned UID is not known to be 41.
    """

    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (MESSAGES 3 UIDVALIDITY 381)"])
    imap.select.return_value = ("OK", [b"3 EXISTS", b"[UIDVALIDITY 381] UIDs valid"])
    imap.uid_search.return_value = ("OK", [b""])
    imap.uid.return_value = (
        "OK",
        [b"1 FETCH (UID 10)", b"ignored", b"2 FETCH (UID 12)", b"3 FETCH (UID 40)"],
    )
    headers = AsyncMock(
        return_value={
            "40": _row("40", "开发进度", to_addresses=["to@example.com"]),
            "12": _row("12", "Notes", to=["cc-only@example.com"], to_addresses=[]),
            "10": _row("10", "Hello", to_addresses=["to@example.com"]),
        }
    )
    output = StringIO()
    sink = logger.add(output, level="INFO")
    try:
        with (
            patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
            patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"10": NOW, "12": NOW, "40": NOW})),
            patch.object(client, "_batch_fetch_headers", headers),
        ):
            snapshot = await client.get_mailbox_metadata_snapshot("INBOX")
    finally:
        logger.remove(sink)

    assert snapshot.complete is True
    assert snapshot.state.uidvalidity == 381
    assert snapshot.state.uidnext is None
    assert snapshot.state.message_count == 3
    assert [email["email_id"] for email in snapshot.emails] == ["40", "12", "10"]
    assert snapshot.emails[0]["subject"] == "开发进度"
    imap.uid_search.assert_not_awaited()
    assert ("fetch", "1:*", "(UID)") in [call.args for call in imap.uid.await_args_list]
    assert "omitted UIDNEXT" in output.getvalue()
    assert "allocation counter" in output.getvalue()
    logged = output.getvalue()
    assert logged.count("omitted UIDNEXT") == 1


@pytest.mark.asyncio
async def test_missing_uidvalidity_does_not_reuse_rows_or_invent_an_epoch() -> None:
    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDNEXT 5 MESSAGES 1)"])
    imap.select.return_value = ("OK", [b"1 EXISTS"])
    imap.uid.return_value = ("OK", [b"1 FETCH (UID 4)"])
    retained = _row("4", "old subject")
    headers = AsyncMock(return_value={"4": _row("4", "new subject")})
    output = StringIO()
    sink = logger.add(output, level="INFO")
    try:
        with (
            patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
            patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"4": NOW})),
            patch.object(client, "_batch_fetch_headers", headers),
        ):
            first = await client.get_mailbox_metadata_snapshot(
                "INBOX",
                retained_emails=(retained,),
                retained_uidvalidity=None,
            )
            headers.return_value = {"4": _row("4", "reset subject")}
            second = await client.get_mailbox_metadata_snapshot(
                "INBOX",
                retained_emails=first.emails,
                retained_uidvalidity=first.state.uidvalidity,
            )
    finally:
        logger.remove(sink)

    assert first.complete is True
    assert first.state.uidvalidity is None
    assert first.state.uidnext == 5
    assert first.emails[0]["subject"] == "new subject"
    assert second.emails[0]["subject"] == "reset subject"
    assert second.state.uidvalidity is None
    assert headers.await_count == 2
    assert "omitted UIDVALIDITY" in output.getvalue()
    assert "cached rows will not be reused" in output.getvalue()


@pytest.mark.asyncio
async def test_same_uidvalidity_reuses_rows_when_uidnext_is_missing() -> None:
    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 MESSAGES 2)"])
    imap.select.return_value = ("OK", [b"2 EXISTS"])
    imap.uid.return_value = ("OK", [b"1 FETCH (UID 4)", b"2 FETCH (UID 9)"])
    retained = _row("4", "kept")
    headers = AsyncMock(return_value={"9": _row("9", "newer")})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"9": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot(
            "INBOX",
            retained_emails=(retained,),
            retained_uidvalidity=77,
        )

    assert snapshot.complete is True
    assert snapshot.state.uidnext is None
    assert snapshot.state.uidvalidity == 77
    assert snapshot.state.message_count == 2
    assert [email["email_id"] for email in snapshot.emails] == ["9", "4"]
    headers.assert_awaited()
    fetched_uids = headers.await_args.args[1]
    assert fetched_uids == ["9"]


@pytest.mark.asyncio
async def test_select_uidnext_does_not_fetch_an_inventory() -> None:
    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 MESSAGES 1)"])
    imap.select.return_value = ("OK", [b"1 EXISTS", b"[UIDNEXT 8]"])
    imap.uid_search.return_value = ("OK", [b"7"])
    headers = AsyncMock(return_value={"7": _row("7", "Hello")})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"7": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is True
    assert snapshot.state == MailboxState(77, 8, 1)
    imap.uid.assert_not_awaited()
    imap.uid_search.assert_awaited()


@pytest.mark.asyncio
async def test_empty_mailbox_without_uidnext_is_complete_without_search() -> None:
    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 MESSAGES 0)"])
    imap.select.return_value = ("OK", [b"0 EXISTS"])
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is True
    assert snapshot.state == MailboxState(77, None, 0)
    assert snapshot.emails == ()
    imap.uid.assert_not_awaited()
    imap.uid_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_uid_inventory_rejects_duplicates_overflow_and_skips_non_bytes() -> None:
    client = _client()
    imap = _imap()
    imap.uid.return_value = ("OK", [b"1 FETCH (UID 4)", "skip", b"2 FETCH (UID 5)"])
    assert await client._fetch_uid_inventory(imap, maximum_uids=10) == ["4", "5"]

    imap.uid.return_value = ("OK", [b"1 FETCH (UID 4)", b"2 FETCH (UID 4)"])
    with pytest.raises(MetadataProviderObservationError, match="invalid UID inventory"):
        await client._fetch_uid_inventory(imap, maximum_uids=10)

    imap.uid.return_value = ("OK", [b"1 FETCH (UID 4294967296)"])
    with pytest.raises(MetadataProviderObservationError, match="invalid UID inventory"):
        await client._fetch_uid_inventory(imap, maximum_uids=10)

    imap.uid.return_value = ("OK", [b"1 FETCH (UID 1)", b"2 FETCH (UID 2)"])
    with pytest.raises(MetadataQueryTooBroadError, match="query_too_broad"):
        await client._fetch_uid_inventory(imap, maximum_uids=1)


@pytest.mark.asyncio
async def test_uid_appearing_during_refresh_without_validity_drops_the_rows() -> None:
    client = _client()
    imap = _imap()
    imap.status.side_effect = [
        ("OK", [b"INBOX (UIDNEXT 5 MESSAGES 1)"]),
        ("OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 5 MESSAGES 1)"]),
    ]
    imap.select.return_value = ("OK", [b"1 EXISTS"])
    imap.uid.return_value = ("OK", [b"1 FETCH (UID 4)"])
    headers = AsyncMock(return_value={"4": _row("4", "unstable")})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"4": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is False
    assert snapshot.emails == ()
    assert snapshot.state.uidvalidity == 9


@pytest.mark.asyncio
async def test_delta_without_uidnext_is_not_an_append_only_extension() -> None:
    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 MESSAGES 3)"])
    imap.select.return_value = ("OK", [b"3 EXISTS"])
    imap.uid.return_value = ("OK", [b"1 FETCH (UID 1)", b"2 FETCH (UID 5)", b"3 FETCH (UID 9)"])
    headers = AsyncMock(return_value={"5": _row("5", "gap"), "9": _row("9", "tail")})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"5": NOW, "9": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        delta = await client.get_mailbox_uid_delta("INBOX", 1, maximum_new_messages=10)

    assert delta.resumable is False
    assert delta.uidvalidity_changed is False
    assert delta.state.uidnext is None
    assert [email["email_id"] for email in delta.emails] == ["5", "9"]
    imap.uid_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_delta_without_uidvalidity_does_not_resume() -> None:
    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDNEXT 8 MESSAGES 1)"])
    imap.select.return_value = ("OK", [b"1 EXISTS"])
    imap.uid.return_value = ("OK", [b"1 FETCH (UID 4)"])
    headers = AsyncMock(return_value={"4": _row("4", "reset")})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"4": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        delta = await client.get_mailbox_uid_delta("INBOX", 0, maximum_new_messages=10)

    assert delta.resumable is False
    assert delta.uidvalidity_changed is True
    assert delta.emails == ()
    assert delta.state.uidvalidity is None


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["imap.163.com", "imap.126.com", "imap.yeah.net", "imap.example.com", ""])
async def test_default_mode_keeps_server_search_on_every_host(host: str) -> None:
    service, provider, projection = _service(host)

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 0
    provider.list_metadata.assert_awaited_once()
    provider.mailbox_state.assert_not_awaited()
    projection.read_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_server_mode_overrides_local_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", "local")
    service, provider, projection = _service("imap.example.com", "server")

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 0
    provider.list_metadata.assert_awaited_once()
    projection.read_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mode_filters_a_qualified_projection() -> None:
    service, provider, projection = _service("imap.example.com", "local")
    projection.read_complete.return_value = [_row("12", "开发进度"), _row("10", "Hello")]

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 1
    assert [email.subject for email in response.emails] == ["开发进度"]
    provider.list_metadata.assert_not_awaited()
    provider.mailbox_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mode_to_match_ignores_cc_and_refetches_without_a_to_field() -> None:
    service, provider, projection = _service("imap.163.com", "local")
    projection.read_complete.return_value = [_row("12", "Hello", to=["cc-only@example.com"])]
    provider.mailbox_snapshot.return_value = _mailbox_snapshot((
        _row(
            "12",
            "Hello",
            to=["to@example.com", "cc-only@example.com"],
            to_addresses=["to@example.com"],
        ),
    ))

    missed = await service.execute(ListEmailMetadataQuery(account_name="work", to_address="cc-only@example.com"))
    matched = await service.execute(ListEmailMetadataQuery(account_name="work", to_address="to@example.com"))

    assert missed.total == 0
    assert matched.total == 1
    assert matched.emails[0].recipients == ["to@example.com", "cc-only@example.com"]
    provider.list_metadata.assert_not_awaited()
    assert provider.mailbox_snapshot.await_count == 2


@pytest.mark.asyncio
async def test_local_mode_reads_headers_when_the_projection_is_unavailable() -> None:
    service, provider, _projection = _service("imap.example.com", "local")
    service._projections.open = AsyncMock(side_effect=MetadataProjectionError("unavailable"))
    provider.mailbox_state.return_value = MailboxState(None, None, 1)
    provider.mailbox_snapshot.return_value = _mailbox_snapshot(
        (_row("4", "开发"),),
        state=MailboxState(None, None, 1),
    )

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 1
    assert response.emails[0].subject == "开发"
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_uidvalidity_does_not_return_a_cached_row_after_uid_reuse() -> None:
    service, provider, projection = _service("imap.example.com", "local")
    provider.mailbox_state.return_value = MailboxState(None, 5, 1)
    projection.read_complete.return_value = [_row("4", "old subject")]
    provider.mailbox_snapshot.side_effect = [
        _mailbox_snapshot((_row("4", "old subject"),), state=MailboxState(None, 5, 1)),
        _mailbox_snapshot((_row("4", "new subject"),), state=MailboxState(None, 5, 1)),
    ]

    first = await service.execute(ListEmailMetadataQuery(account_name="work", subject="old"))
    second = await service.execute(ListEmailMetadataQuery(account_name="work", subject="new"))

    assert [email.subject for email in first.emails] == ["old subject"]
    assert [email.subject for email in second.emails] == ["new subject"]
    projection.read_complete.assert_not_awaited()
    projection.write_snapshot.assert_not_awaited()
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mode_rejects_incomplete_and_over_limit_reads() -> None:
    service, provider, projection = _service("imap.example.com", "local")
    provider.mailbox_snapshot.return_value = _mailbox_snapshot(
        (_row("4", "partial"),),
        state=MailboxState(77, 10, 5),
        complete=False,
    )

    with pytest.raises(MetadataQueryTooBroadError, match="complete mailbox coverage"):
        await service.execute(ListEmailMetadataQuery(account_name="work", subject="partial"))
    provider.list_metadata.assert_not_awaited()

    provider.mailbox_snapshot.side_effect = MetadataQueryTooBroadError(
        "query_too_broad: metadata search exceeded 1000 candidate UIDs"
    )
    with pytest.raises(MetadataQueryTooBroadError, match="exceeded 1000"):
        await service.execute(ListEmailMetadataQuery(account_name="work", subject="partial"))
    provider.list_metadata.assert_not_awaited()
    projection.read_complete.assert_awaited()


@pytest.mark.asyncio
async def test_local_mode_does_not_fall_back_when_header_evidence_is_missing() -> None:
    service, provider, _projection = _service("imap.example.com", "local")
    service._load_mailbox_snapshot = AsyncMock(return_value=None)

    with pytest.raises(MetadataProviderError, match="could not read mailbox headers"):
        await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mode_propagates_provider_and_projection_read_failures() -> None:
    service, provider, projection = _service("imap.example.com", "local")
    provider.mailbox_state.side_effect = RuntimeError("mailbox down")
    with pytest.raises(RuntimeError, match="mailbox down"):
        await service.execute(ListEmailMetadataQuery(account_name="work", from_address="allowed"))
    provider.list_metadata.assert_not_awaited()

    provider.mailbox_state.side_effect = None
    provider.mailbox_state.return_value = MailboxState(77, 5, 1)
    projection.read_complete.side_effect = MetadataProjectionError("read failed")
    provider.mailbox_snapshot.return_value = _mailbox_snapshot((_row("3", "from cache miss"),))
    response = await service.execute(ListEmailMetadataQuery(account_name="work", from_address="allowed"))
    assert response.total == 1
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        ListEmailMetadataQuery(account_name="work", subject="开发", body="notes"),
        ListEmailMetadataQuery(account_name="work", from_address="dev", text="notes"),
        ListEmailMetadataQuery(account_name="work", to_address="user@example.com", seen=True),
    ],
)
async def test_local_mode_rejects_unsupported_predicate_combinations(query: ListEmailMetadataQuery) -> None:
    service, provider, _projection = _service("imap.163.com", "local")

    with pytest.raises(ValueError, match="subject, from, and to"):
        await service.execute(query)
    provider.list_metadata.assert_not_awaited()
    provider.mailbox_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mode_leaves_body_search_on_the_server() -> None:
    service, provider, projection = _service("imap.163.com", "local")

    response = await service.execute(ListEmailMetadataQuery(account_name="work", body="notes"))

    assert response.total == 0
    provider.list_metadata.assert_awaited_once()
    projection.read_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_environment_local_mode_applies_when_the_account_does_not_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", "local")
    service, provider, projection = _service("imap.gmail.com")
    projection.read_complete.return_value = [_row("3", "开发")]

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 1
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_projection_port_refuses_reuse_without_provider_identity() -> None:
    index = MagicMock()
    index.read_complete.return_value = None
    projection = SQLiteMetadataProjection(index, "operational")

    assert await projection.read_complete("INBOX", MailboxState(None, 5, 1)) is None
    assert await projection.read_complete("INBOX", MailboxState(77, None, 1)) is None
    index.read_complete.assert_not_called()

    observed = await projection.read_complete("INBOX", MailboxState(77, 5, 1))
    assert observed is None
    index.read_complete.assert_called_once()

    with pytest.raises(MetadataProjectionError, match="cannot be reused"):
        await projection.write_snapshot("INBOX", _mailbox_snapshot((), state=MailboxState(None, None, 0)))
    index.write_snapshot.assert_not_called()


def test_mailbox_state_parsing_keeps_omitted_fields_and_rejects_invalid_ones() -> None:
    parsed = EmailClient._parse_mailbox_status_fields((
        "OK",
        [bytearray(b"INBOX (MESSAGES 1 UIDVALIDITY 2 UIDNEXT 3)")],
    ))
    assert parsed == (2, 3, 1)
    with pytest.raises(RuntimeError, match="required bounded state"):
        EmailClient._parse_mailbox_status_fields(("OK", "not-a-list"))
    with pytest.raises(RuntimeError, match="invalid state"):
        EmailClient._parse_mailbox_status_fields(("OK", [b"INBOX (MESSAGES 1 UIDVALIDITY 0 UIDNEXT 2)"]))
    with pytest.raises(RuntimeError, match="invalid state"):
        EmailClient._parse_mailbox_status_fields(("OK", [b"INBOX (MESSAGES 1 UIDVALIDITY 2 UIDNEXT 0)"]))

    assert EmailClient._parse_select_state_fields("OK") == (None, None)
    assert EmailClient._parse_select_state_fields(("OK",)) == (None, None)
    assert EmailClient._parse_select_state_fields(("OK", [b"[UIDVALIDITY 0] [UIDNEXT 0]"])) == (None, None)

    query = ListEmailMetadataQuery(account_name="work")
    snapshot = _mailbox_snapshot((_row("1", "Hello"),), state=MailboxState(0, 2, 1))
    with pytest.raises(MetadataProviderObservationError, match="invalid"):
        _validate_snapshot(query, snapshot)
    with pytest.raises(MetadataProviderObservationError, match="invalid"):
        _validate_snapshot(query, _mailbox_snapshot((_row("1", "Hello"),), state=MailboxState(2, 0, 1)))
    with pytest.raises(MetadataProviderObservationError, match="invalid"):
        _validate_snapshot(query, _mailbox_snapshot((_row("1", "Hello"),), state=MailboxState(2, 2, -1)))

    mailbox = MailboxInfo(name="INBOX", delimiter="", flags=[])
    baseline = MetadataIncrementalBaseline(
        state=MailboxState(7, 4, 1),
        high_uid=1,
        mailbox=mailbox,
        emails=(_row("1", "kept"),),
    )
    unknown_baseline = MetadataIncrementalBaseline(
        state=MailboxState(None, 4, 1),
        high_uid=1,
        mailbox=mailbox,
        emails=(_row("1", "kept"),),
    )
    assert (
        _merge_append_only_snapshot(
            "INBOX",
            unknown_baseline,
            MailboxUidDelta(
                state=MailboxState(7, 4, 2),
                mailbox=mailbox,
                emails=(_row("2", "new"),),
                observed_at=NOW,
                uidvalidity_changed=False,
                resumable=True,
            ),
        )
        is None
    )
    for state in (MailboxState(None, 4, 1), MailboxState(7, None, 1)):
        assert (
            _merge_append_only_snapshot(
                "INBOX",
                baseline,
                MailboxUidDelta(
                    state=state,
                    mailbox=mailbox,
                    emails=(),
                    observed_at=NOW,
                    uidvalidity_changed=False,
                    resumable=True,
                ),
            )
            is None
        )


@pytest.mark.asyncio
async def test_select_can_supply_the_uidvalidity_status_omitted() -> None:
    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDNEXT 5 MESSAGES 1)"])
    imap.select.return_value = ("OK", [b"1 EXISTS", b"[UIDVALIDITY 77]"])
    imap.uid_search.return_value = ("OK", [b"4"])
    headers = AsyncMock(return_value={"4": _row("4", "Hello")})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"4": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is True
    assert snapshot.state == MailboxState(77, 5, 1)
    imap.uid.assert_not_awaited()


@pytest.mark.asyncio
async def test_uid_inventory_bounds_and_bytearray_items() -> None:
    client = _client()
    imap = _imap()
    with pytest.raises(ValueError, match="inventory limit"):
        await client._fetch_uid_inventory(imap, maximum_uids=0)
    imap.uid.return_value = ("OK", None)
    with pytest.raises(MetadataProviderObservationError, match="invalid UID inventory"):
        await client._fetch_uid_inventory(imap, maximum_uids=5)
    imap.uid.return_value = ("OK", [bytearray(b"1 FETCH (UID 6)")])
    assert await client._fetch_uid_inventory(imap, maximum_uids=5) == ["6"]


@pytest.mark.asyncio
async def test_delta_at_the_uid_ceiling_does_not_search() -> None:
    client = _client()
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 5 MESSAGES 1)"])
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={})),
        patch.object(client, "_batch_fetch_headers", AsyncMock(return_value={})),
    ):
        delta = await client.get_mailbox_uid_delta("INBOX", MAX_IMAP_UID, maximum_new_messages=1)

    assert delta.resumable is True
    assert delta.emails == ()
    imap.uid_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mode_keeps_reading_when_cached_rows_cannot_be_loaded() -> None:
    service, provider, projection = _service("imap.example.com", "local")
    projection.read_incremental_baseline.side_effect = MetadataProjectionError("baseline")
    projection.read_partial_emails.side_effect = MetadataProjectionError("partial")
    provider.mailbox_snapshot.return_value = _mailbox_snapshot((_row("3", "开发"),))

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 1
    provider.list_metadata.assert_not_awaited()

    provider.mailbox_snapshot.side_effect = RuntimeError("header fetch failed")
    with pytest.raises(RuntimeError, match="header fetch failed"):
        await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))
    provider.list_metadata.assert_not_awaited()
