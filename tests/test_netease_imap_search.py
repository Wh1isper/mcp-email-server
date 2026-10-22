from __future__ import annotations

from datetime import UTC, datetime
from email.header import Header
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr, ValidationError

from mcp_email_server.application.metadata import (
    ListEmailMetadataQuery,
    MailboxState,
    MetadataAccountSnapshot,
    MetadataProviderAccess,
    MetadataQueryService,
    _header_text_matches,
)
from mcp_email_server.config import (
    EmailServer,
    EmailSettings,
    imap_search_mode,
    resolve_imap_search_mode,
)
from mcp_email_server.emails.classic import (
    _MAILBOX_STATE_NOTICES,
    EmailClient,
    _unobserved_uidvalidity,
)
from mcp_email_server.emails.models import EmailMetadata, EmailMetadataPageResponse
from mcp_email_server.imap_keywords import ImapKeywordRegistry
from mcp_email_server.log import logger

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_search_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", raising=False)
    _MAILBOX_STATE_NOTICES.clear()


def _client(host: str, *, mode: str | None = None) -> EmailClient:
    return EmailClient(
        EmailServer(
            user_name="user@example.test",
            password=SecretStr("secret"),
            host=host,
            port=993,
        ),
        sender="User <user@example.test>",
        imap_search_mode=mode,  # type: ignore[arg-type]
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


def _row(uid: str, subject: str) -> dict[str, object]:
    return {
        "email_id": uid,
        "message_id": f"<{uid}@example.test>",
        "subject": subject,
        "from": "allowed@example.test",
        "to": ["用户 <user@163.com>"],
        "date": NOW,
        "attachments": [],
        "_flags": [],
    }


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


@pytest.mark.parametrize(
    ("host", "configured", "env", "expected"),
    [
        ("imap.163.com", None, None, "client"),
        ("IMAP.126.COM.", None, None, "client"),
        (" imap.yeah.net ", None, None, "client"),
        ("imap.example.com", None, None, "server"),
        ("imap.qq.com", None, None, "server"),
        ("imap.163.com", None, "server", "server"),
        ("imap.example.com", None, "client", "client"),
        ("imap.163.com", "server", "client", "server"),
        ("imap.example.com", "client", "server", "client"),
        ("imap.example.com", "auto", "client", "server"),
        ("imap.163.com", "auto", "server", "client"),
    ],
)
def test_search_mode_selection(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    configured: str | None,
    env: str | None,
    expected: str,
) -> None:
    if env is not None:
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", env)

    assert resolve_imap_search_mode(host, configured) == expected  # type: ignore[arg-type]


def test_invalid_search_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SEARCH_MODE", "local")

    with pytest.raises(ValueError, match=r"one of: server, client, auto"):
        imap_search_mode()
    with pytest.raises(ValidationError):
        EmailSettings(
            account_name="work",
            full_name="Work",
            email_address="user@example.test",
            imap_search_mode="local",  # type: ignore[arg-type]
            incoming=EmailServer(
                user_name="user@example.test",
                password=SecretStr("secret"),
                host="imap.163.com",
                port=993,
            ),
        )


def test_header_text_matches_decoded_chinese_subjects_case_insensitively() -> None:
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
    assert _header_text_matches(
        ListEmailMetadataQuery(account_name="work", to_address="用户"),
        {"subject": "Hello", "from": "", "to": ["用户 <user@163.com>"]},
    )
    assert not _header_text_matches(query, {"subject": "Hello", "from": "", "to": []})

    client = _client("imap.163.com")
    raw = (
        f"Subject: {Header('开发', 'utf-8').encode()}\r\nFrom: Dev <dev@example.com>\r\nTo: user@163.com\r\n\r\n"
    ).encode()
    parsed = client._parse_headers("4", raw)

    assert parsed is not None
    assert _header_text_matches(query, parsed)


@pytest.mark.asyncio
async def test_status_without_uidnext_builds_snapshot_from_uid_fetch() -> None:
    client = _client("imap.163.com")
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 MESSAGES 2)"])
    output = StringIO()
    sink = logger.add(output, level="INFO")
    headers = AsyncMock(return_value={"12": _row("12", "开发进度"), "10": _row("10", "Hello")})
    try:
        with (
            patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
            patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"10": NOW, "12": NOW})),
            patch.object(client, "_batch_fetch_headers", headers),
        ):
            snapshot = await client.get_mailbox_metadata_snapshot("INBOX")
    finally:
        logger.remove(sink)

    assert snapshot.complete is True
    assert snapshot.state.uidvalidity == 77
    assert snapshot.state.uidnext == 13
    assert snapshot.state.message_count == 2
    assert snapshot.state.uidnext_derived is True
    assert snapshot.state.uidvalidity_unobserved is False
    assert [email["email_id"] for email in snapshot.emails] == ["12", "10"]
    assert snapshot.emails[0]["subject"] == "开发进度"
    imap.uid_search.assert_not_awaited()
    assert ("fetch", "1:*", "(UID)") in [call.args for call in imap.uid.await_args_list]
    assert "omitted UIDNEXT" in output.getvalue()


@pytest.mark.asyncio
async def test_status_without_uidvalidity_uses_a_synthetic_epoch() -> None:
    client = _client("imap.example.com")
    imap = _imap()
    imap.status.return_value = ("OK", [b"INBOX (UIDNEXT 5 MESSAGES 1)"])
    imap.uid.return_value = ("OK", [b"1 FETCH (UID 4)"])
    headers = AsyncMock(return_value={"4": _row("4", "开发")})
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_batch_fetch_dates", AsyncMock(return_value={"4": NOW})),
        patch.object(client, "_batch_fetch_headers", headers),
    ):
        snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is True
    assert snapshot.state.uidnext == 5
    assert snapshot.state.uidnext_derived is False
    assert snapshot.state.uidvalidity_unobserved is True
    assert snapshot.state.uidvalidity == _unobserved_uidvalidity(["4"])
    assert snapshot.state.uidvalidity > 2**32 - 1
    assert _unobserved_uidvalidity(["4", "9"]) != snapshot.state.uidvalidity
    assert [email["subject"] for email in snapshot.emails] == ["开发"]


@pytest.mark.asyncio
async def test_select_uidnext_does_not_derive_when_present() -> None:
    client = _client("imap.example.com")
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
@pytest.mark.parametrize("host", ["imap.163.com", "imap.126.com", "imap.yeah.net"])
async def test_auto_mode_filters_netease_subjects_from_the_snapshot(host: str) -> None:
    service, provider, projection = _service(host)
    projection.read_complete.return_value = [_row("12", "开发进度"), _row("10", "Hello")]

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 1
    assert [email.subject for email in response.emails] == ["开发进度"]
    provider.list_metadata.assert_not_awaited()
    provider.mailbox_state.assert_awaited()


@pytest.mark.asyncio
async def test_auto_mode_keeps_server_search_for_other_hosts() -> None:
    service, provider, projection = _service("imap.example.com")
    provider.list_metadata.return_value = EmailMetadataPageResponse(
        page=1,
        page_size=10,
        before=None,
        since=None,
        subject="开发",
        emails=[EmailMetadata.from_email(_row("1", "开发进度"))],
        total=1,
    )

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 1
    provider.list_metadata.assert_awaited_once()
    provider.mailbox_state.assert_not_awaited()
    projection.read_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_server_mode_keeps_netease_on_imap_search() -> None:
    service, provider, projection = _service("imap.163.com", "server")

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 0
    provider.list_metadata.assert_awaited_once()
    provider.mailbox_state.assert_not_awaited()
    projection.read_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_client_mode_filters_other_hosts_locally() -> None:
    service, provider, projection = _service("imap.example.com", "client")
    projection.read_complete.return_value = [_row("3", "开发")]

    response = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert response.total == 1
    assert response.emails[0].subject == "开发"
    provider.list_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_result_detection_switches_the_account_to_client_filtering() -> None:
    service, provider, projection = _service("imap.example.com")
    projection.read_complete.return_value = [_row("12", "开发进度"), _row("10", "Weekly notes")]
    output = StringIO()
    sink = logger.add(output, level="INFO")
    try:
        first = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))
        second = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))
    finally:
        logger.remove(sink)

    assert first.total == 1
    assert [email.subject for email in first.emails] == ["开发进度"]
    assert second.total == 1
    provider.list_metadata.assert_awaited_once()
    logged = output.getvalue()
    assert logged.count("using client-side filtering") == 1

    missed = await service.execute(ListEmailMetadataQuery(account_name="work", subject="不存在"))
    assert missed.total == 0
    provider.list_metadata.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_result_without_local_matches_stays_on_server_search() -> None:
    service, provider, projection = _service("imap.gmail.com")
    projection.read_complete.return_value = [_row("12", "Hello")]

    first = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))
    second = await service.execute(ListEmailMetadataQuery(account_name="work", subject="开发"))

    assert first.total == 0
    assert second.total == 0
    assert provider.list_metadata.await_count == 2
    assert projection.read_complete.await_count == 2
