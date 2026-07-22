from __future__ import annotations

import asyncio
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from mcp_email_server.adapters.reads import ClassicReadProvider, LocalArtifactWriter
from mcp_email_server.application.reads import (
    AttachmentPayload,
    DownloadAttachmentCommand,
    GetEmailContentQuery,
    ListMailboxesQuery,
    ReadAccountSnapshot,
    ReadProviderError,
)
from mcp_email_server.emails.models import MailboxInfo


def _account() -> ReadAccountSnapshot:
    return ReadAccountSnapshot(
        account_name="work",
        mode="managed",
        allowed_senders=("allowed@example.test",),
        enable_attachment_download=True,
    )


@pytest.mark.asyncio
async def test_content_adapter_forwards_snapshot_sender_policy_and_collapses_per_id_failures() -> None:
    handler = Mock()
    handler.incoming_client.get_email_body_by_id = AsyncMock(
        side_effect=[
            {
                "email_id": "1",
                "message_id": "<one@example.test>",
                "subject": "One",
                "from": "allowed@example.test",
                "to": ["work@example.test"],
                "date": datetime.now(UTC),
                "body": "body",
                "attachments": [],
            },
            RuntimeError("provider-controlled detail"),
        ]
    )
    provider = ClassicReadProvider(handler)
    query = GetEmailContentQuery("work", ("1", "2"), mailbox="Archive", body_offset=10, max_body_length=20)

    response = await provider.get_content(query, _account())

    assert [email.email_id for email in response.emails] == ["1"]
    assert response.failed_ids == ["2"]
    first = handler.incoming_client.get_email_body_by_id.await_args_list[0]
    assert first.args[:4] == ("1", "Archive", False)
    assert first.kwargs["allowed_senders"] == ["allowed@example.test"]
    assert first.kwargs["body_offset"] == 10
    assert first.kwargs["max_body_length"] == 20


@pytest.mark.asyncio
async def test_content_adapter_propagates_cancellation() -> None:
    handler = Mock()
    handler.incoming_client.get_email_body_by_id = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await ClassicReadProvider(handler).get_content(GetEmailContentQuery("work", ("1",)), _account())


@pytest.mark.asyncio
async def test_attachment_adapter_forwards_sender_policy_and_maps_response() -> None:
    handler = Mock()
    handler.incoming_client.fetch_attachment = AsyncMock(
        return_value={
            "email_id": "1",
            "attachment_name": "document.pdf",
            "mime_type": "application/pdf",
            "content": b"document",
        }
    )
    command = DownloadAttachmentCommand("work", "1", "document.pdf", "downloads/document.pdf", "Archive")

    response = await ClassicReadProvider(handler).fetch_attachment(command, _account())

    assert response.content == b"document"
    handler.incoming_client.fetch_attachment.assert_awaited_once_with(
        "1",
        "document.pdf",
        "Archive",
        allowed_senders=["allowed@example.test"],
    )


@pytest.mark.asyncio
async def test_mailbox_adapter_sanitizes_provider_failure() -> None:
    handler = Mock()
    handler.list_mailboxes = AsyncMock(side_effect=RuntimeError("raw provider line"))

    with pytest.raises(ReadProviderError, match="mailbox discovery failed") as exc_info:
        await ClassicReadProvider(handler).list_mailboxes(ListMailboxesQuery("work"))

    assert "raw provider line" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_mailbox_adapter_preserves_order() -> None:
    handler = Mock()
    expected = [MailboxInfo(name="INBOX", delimiter="/", flags=[]), MailboxInfo(name="Sent", delimiter="/", flags=[])]
    handler.list_mailboxes = AsyncMock(return_value=expected)

    result = await ClassicReadProvider(handler).list_mailboxes(ListMailboxesQuery("work"))

    assert result == expected


@pytest.mark.asyncio
async def test_artifact_writer_writes_only_explicit_regular_file(tmp_path: Path) -> None:
    destination = tmp_path / "downloads" / "document.pdf"
    payload = AttachmentPayload("1", "document.pdf", "application/pdf", b"document")

    saved_path = await LocalArtifactWriter().write(str(destination), payload)

    assert saved_path == destination.as_posix()
    assert destination.read_bytes() == b"document"
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_artifact_writer_rejects_symlink_destination_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("preserve")
    destination = tmp_path / "download.txt"
    try:
        destination.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(PermissionError, match="regular file"):
        await LocalArtifactWriter().write(
            str(destination),
            AttachmentPayload("1", "download.txt", "text/plain", b"replacement"),
        )

    assert target.read_text() == "preserve"


@pytest.mark.asyncio
async def test_artifact_writer_rejects_symlink_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(PermissionError, match="parent is unsafe"):
        await LocalArtifactWriter().write(
            str(linked_parent / "download.txt"),
            AttachmentPayload("1", "download.txt", "text/plain", b"content"),
        )

    assert not (real_parent / "download.txt").exists()
