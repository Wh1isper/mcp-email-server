from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from mcp_email_server.adapters.metadata import (
    ClassicMetadataProvider,
    LocalMetadataBackend,
    LocalMetadataProjectionFactory,
    SQLiteMetadataProjection,
)
from mcp_email_server.application.metadata import (
    ListEmailMetadataQuery,
    MailboxMetadataSnapshot,
    MailboxState,
    MetadataAccountSnapshot,
    MetadataProjectionError,
    MetadataProviderError,
)
from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import MAX_INDEXED_UID_WINDOW
from mcp_email_server.emails.models import EmailMetadataPageResponse, MailboxInfo
from mcp_email_server.metadata_index import IndexedMailboxSnapshot, MetadataIndexError

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


def _account() -> EmailSettings:
    return EmailSettings(
        account_name="work",
        full_name="Work User",
        email_address="user@example.test",
        incoming=EmailServer(
            user_name="user@example.test",
            password=SecretStr("secret"),
            host="imap.example.test",
            port=993,
        ),
    )


def _email() -> dict[str, object]:
    return {
        "email_id": "1",
        "message_id": "<1@example.test>",
        "subject": "Subject",
        "from": "sender@example.test",
        "to": ["user@example.test"],
        "date": NOW,
        "attachments": [],
        "_internal_date": NOW,
        "_flags": [],
    }


@pytest.mark.asyncio
async def test_classic_provider_maps_query_and_bounded_snapshot_contract() -> None:
    response = EmailMetadataPageResponse(
        page=2,
        page_size=20,
        before=None,
        since=None,
        subject="needle",
        emails=[],
        total=0,
    )
    snapshot = MailboxMetadataSnapshot(
        state=MailboxState(7, 2, 1),
        mailbox=MailboxInfo(name="Archive", delimiter="/", flags=[]),
        emails=(_email(),),
        complete=True,
        observed_at=NOW,
    )
    handler = MagicMock()
    handler.incoming_client.get_emails_metadata = AsyncMock(return_value=(response.total, []))
    handler.incoming_client.get_mailbox_state = AsyncMock(return_value=snapshot.state)
    handler.incoming_client.get_mailbox_metadata_snapshot = AsyncMock(return_value=snapshot)
    handler.incoming_client.get_email_flags = AsyncMock(return_value={"7": ["$label4"]})
    provider = ClassicMetadataProvider(handler)
    query = ListEmailMetadataQuery(
        account_name="work",
        page=2,
        page_size=20,
        subject="needle",
        order="asc",
        mailbox="Archive",
        seen=False,
        has_attachment=True,
    )

    account = MetadataAccountSnapshot(
        account_name="work",
        mode="managed",
        allowed_senders=("trusted@example.test",),
    )

    assert await provider.list_metadata(query, account) == response
    assert await provider.mailbox_state("Archive") == snapshot.state
    assert await provider.mailbox_snapshot("Archive") is snapshot
    assert await provider.flags_for("Archive", ("7",)) == {"7": ["$label4"]}
    handler.incoming_client.get_emails_metadata.assert_awaited_once_with(
        page=2,
        page_size=20,
        before=None,
        since=None,
        subject="needle",
        from_address=None,
        to_address=None,
        order="asc",
        mailbox="Archive",
        seen=False,
        flagged=None,
        answered=None,
        body=None,
        text=None,
        has_attachment=True,
        tag_keywords=[],
        tag_match="all",
        allowed_senders=["trusted@example.test"],
    )
    handler.incoming_client.get_email_flags.assert_awaited_once_with(["7"], "Archive")
    handler.incoming_client.get_mailbox_metadata_snapshot.assert_awaited_once_with(
        "Archive",
        maximum_window=MAX_INDEXED_UID_WINDOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "state", "snapshot"])
@pytest.mark.parametrize("exception_type", [OSError, RuntimeError, ValueError])
async def test_classic_provider_sanitizes_transport_exceptions(
    operation: str,
    exception_type: type[Exception],
) -> None:
    handler = MagicMock()
    handler.incoming_client.get_emails_metadata = AsyncMock(
        side_effect=exception_type("provider-controlled transport detail")
    )
    handler.incoming_client.get_mailbox_state = AsyncMock(
        side_effect=exception_type("provider-controlled transport detail")
    )
    handler.incoming_client.get_mailbox_metadata_snapshot = AsyncMock(
        side_effect=exception_type("provider-controlled transport detail")
    )
    provider = ClassicMetadataProvider(handler)

    with pytest.raises(MetadataProviderError, match="provider_failure") as exc_info:
        if operation == "list":
            await provider.list_metadata(
                ListEmailMetadataQuery(account_name="work"),
                MetadataAccountSnapshot(account_name="work", mode="managed", allowed_senders=()),
            )
        elif operation == "state":
            await provider.mailbox_state("INBOX")
        else:
            await provider.mailbox_snapshot("INBOX")

    assert "provider-controlled" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_sqlite_projection_maps_bound_identity_and_snapshot_fields() -> None:
    index = MagicMock()
    index.read_complete.return_value = IndexedMailboxSnapshot(
        uidvalidity=7,
        uidnext=2,
        message_count=1,
        emails=(_email(),),
        observed_at=NOW,
    )
    projection = SQLiteMetadataProjection(index, "operational-id")
    state = MailboxState(7, 2, 1)
    snapshot = MailboxMetadataSnapshot(
        state=state,
        mailbox=MailboxInfo(name="INBOX", delimiter="/", flags=["\\Inbox"]),
        emails=(_email(),),
        complete=True,
        observed_at=NOW,
    )

    assert await projection.read_complete("INBOX", state) == [_email()]
    await projection.write_snapshot("INBOX", snapshot)

    index.read_complete.assert_called_once_with(
        "operational-id",
        "INBOX",
        uidvalidity=7,
        uidnext=2,
        message_count=1,
    )
    index.write_snapshot.assert_called_once_with(
        "operational-id",
        "INBOX",
        delimiter="/",
        attributes=["\\Inbox"],
        uidvalidity=7,
        uidnext=2,
        message_count=1,
        emails=[_email()],
        complete=True,
        observed_at=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write"])
async def test_sqlite_projection_translates_repository_errors(operation: str) -> None:
    index = MagicMock()
    projection = SQLiteMetadataProjection(index, "operational-id")
    state = MailboxState(7, 2, 1)
    snapshot = MailboxMetadataSnapshot(
        state=state,
        mailbox=MailboxInfo(name="INBOX", delimiter="", flags=[]),
        emails=(_email(),),
        complete=True,
        observed_at=NOW,
    )
    if operation == "read":
        index.read_complete.side_effect = MetadataIndexError("broken")
        call = projection.read_complete("INBOX", state)
    else:
        index.write_snapshot.side_effect = MetadataIndexError("broken")
        call = projection.write_snapshot("INBOX", snapshot)

    with pytest.raises(MetadataProjectionError, match=f"projection {operation} failed"):
        await call


@pytest.mark.asyncio
async def test_local_backend_revalidates_mode_and_binds_projection_to_concrete_account() -> None:
    account = _account()
    settings = MagicMock()
    settings.db_location = "/private/operational.sqlite3"
    settings.allowed_senders = ["trusted@example.test"]
    settings.get_account.return_value = account
    index = MagicMock()
    index.resolve_operational_account.return_value = "operational-id"
    backend = LocalMetadataBackend()
    account_snapshot = MetadataAccountSnapshot(
        account_name="work",
        mode="managed",
        allowed_senders=("trusted@example.test",),
    )

    with (
        patch(
            "mcp_email_server.adapters.metadata.resolve_local_account",
            return_value=SimpleNamespace(mode="managed", settings=settings, account=account),
        ) as resolve_account,
        patch("mcp_email_server.adapters.metadata.MetadataIndex", return_value=index) as index_factory,
    ):
        projection = await LocalMetadataProjectionFactory(backend).open(account_snapshot)

    assert isinstance(projection, SQLiteMetadataProjection)
    resolve_account.assert_called_once_with("work", expected_mode="managed")
    index_factory.assert_called_once_with(Path(settings.db_location), "managed")
    index.resolve_operational_account.assert_called_once_with(account)
