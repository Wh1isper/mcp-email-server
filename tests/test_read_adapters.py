from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import mcp_email_server.adapters.reads as reads_adapter
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
                "in_reply_to": "<parent@example.test>",
                "references": "<root@example.test> <parent@example.test>",
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
    assert response.emails[0].in_reply_to == "<parent@example.test>"
    assert response.emails[0].references == "<root@example.test> <parent@example.test>"
    assert response.failed_ids == ["2"]
    first = handler.incoming_client.get_email_body_by_id.await_args_list[0]
    assert first.args[:4] == ("1", "Archive", False)
    assert first.kwargs["allowed_senders"] == ["allowed@example.test"]
    assert first.kwargs["body_offset"] == 10
    assert first.kwargs["max_body_length"] == 20


@pytest.mark.asyncio
async def test_content_adapter_stops_at_provider_time_aggregate_body_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reads_adapter,
        "APPLICATION_LIMITS",
        replace(reads_adapter.APPLICATION_LIMITS, body_bytes=10, aggregate_body_bytes=4),
    )
    handler = Mock()
    handler.incoming_client.get_email_body_by_id = AsyncMock(
        side_effect=[
            {
                "email_id": "1",
                "message_id": None,
                "subject": "One",
                "from": "allowed@example.test",
                "to": ["work@example.test"],
                "date": datetime.now(UTC),
                "body": "é",
                "attachments": [],
            },
            {
                "email_id": "2",
                "message_id": None,
                "subject": "Two",
                "from": "allowed@example.test",
                "to": ["work@example.test"],
                "date": datetime.now(UTC),
                "body": "éa",
                "attachments": [],
            },
            RuntimeError("must not be fetched"),
        ]
    )

    with pytest.raises(ReadProviderError, match="4 bytes in total"):
        await ClassicReadProvider(handler).get_content(
            GetEmailContentQuery("work", ("1", "2", "3")),
            _account(),
        )

    assert handler.incoming_client.get_email_body_by_id.await_count == 2


@pytest.mark.asyncio
async def test_content_adapter_rejects_oversized_thread_header_before_retaining_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reads_adapter,
        "APPLICATION_LIMITS",
        replace(reads_adapter.APPLICATION_LIMITS, header_bytes=4),
    )
    handler = Mock()
    handler.incoming_client.get_email_body_by_id = AsyncMock(
        return_value={
            "email_id": "1",
            "message_id": "<one@example.test>",
            "in_reply_to": None,
            "references": "ééa",
            "subject": "One",
            "from": "allowed@example.test",
            "to": ["work@example.test"],
            "date": datetime.now(UTC),
            "body": "body",
            "attachments": [],
        }
    )

    with pytest.raises(ReadProviderError, match="thread header exceeds 4 bytes"):
        await ClassicReadProvider(handler).get_content(GetEmailContentQuery("work", ("1",)), _account())

    assert handler.incoming_client.get_email_body_by_id.await_count == 1


@pytest.mark.asyncio
async def test_content_adapter_rejects_aggregate_headers_before_retaining_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reads_adapter,
        "APPLICATION_LIMITS",
        replace(reads_adapter.APPLICATION_LIMITS, header_bytes=100, aggregate_header_bytes=4),
    )
    handler = Mock()
    handler.incoming_client.get_email_body_by_id = AsyncMock(
        return_value={
            "email_id": "1",
            "message_id": None,
            "in_reply_to": None,
            "references": "root",
            "subject": "One",
            "from": "allowed@example.test",
            "to": ["work@example.test"],
            "date": datetime.now(UTC),
            "body": "body",
            "attachments": [],
        }
    )

    with pytest.raises(ReadProviderError, match="headers exceed 4 bytes in total"):
        await ClassicReadProvider(handler).get_content(GetEmailContentQuery("work", ("1",)), _account())

    assert handler.incoming_client.get_email_body_by_id.await_count == 1


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
async def test_artifact_writer_fails_closed_without_pinned_traversal(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "download.txt"
    monkeypatch.setattr("mcp_email_server.adapters.reads._SECURE_ATTACHMENT_WRITES_SUPPORTED", False)

    with pytest.raises(PermissionError, match="platform cannot enforce"):
        await LocalArtifactWriter().write(
            str(destination),
            AttachmentPayload("1", "download.txt", "text/plain", b"content"),
        )

    assert not destination.exists()


@pytest.mark.asyncio
async def test_artifact_writer_rejects_symlink_destination_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("preserve")
    destination = tmp_path / "download.txt"
    try:
        destination.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(PermissionError):
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

    with pytest.raises(PermissionError):
        await LocalArtifactWriter().write(
            str(linked_parent / "download.txt"),
            AttachmentPayload("1", "download.txt", "text/plain", b"content"),
        )

    assert not (real_parent / "download.txt").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor contract")
async def test_artifact_writer_atomically_overwrites_private_regular_target(tmp_path: Path) -> None:
    destination = tmp_path / "download.txt"
    destination.write_bytes(b"old")
    destination.chmod(0o600)
    previous_inode = destination.stat().st_ino

    await LocalArtifactWriter().write(
        str(destination),
        AttachmentPayload("1", "download.txt", "text/plain", b"replacement"),
    )

    assert destination.read_bytes() == b"replacement"
    assert destination.stat().st_ino != previous_inode
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".mcp-email-attachment-*.tmp"))


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor contract")
async def test_artifact_writer_failed_atomic_replace_preserves_original(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "download.txt"
    destination.write_bytes(b"original")
    destination.chmod(0o600)

    def fail_replace(*_args, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="could not be written"):
        await LocalArtifactWriter().write(
            str(destination),
            AttachmentPayload("1", "download.txt", "text/plain", b"replacement"),
        )

    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob(".mcp-email-attachment-*.tmp"))


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor contract")
async def test_artifact_writer_rejects_insecure_existing_target_and_hardlink(tmp_path: Path) -> None:
    destination = tmp_path / "download.txt"
    destination.write_bytes(b"preserve")
    destination.chmod(0o644)

    with pytest.raises(PermissionError, match="permissions are unsafe"):
        await LocalArtifactWriter().write(
            str(destination),
            AttachmentPayload("1", "download.txt", "text/plain", b"replacement"),
        )
    assert destination.read_bytes() == b"preserve"

    destination.chmod(0o600)
    hardlink = tmp_path / "hardlink.txt"
    os.link(destination, hardlink)
    with pytest.raises(PermissionError, match="identity is unsafe"):
        await LocalArtifactWriter().write(
            str(destination),
            AttachmentPayload("1", "download.txt", "text/plain", b"replacement"),
        )
    assert hardlink.read_bytes() == b"preserve"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor contract")
async def test_artifact_writer_rejects_non_sticky_world_writable_parent(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)

    with pytest.raises(PermissionError, match="permissions are unsafe"):
        await LocalArtifactWriter().write(
            str(parent / "download.txt"),
            AttachmentPayload("1", "download.txt", "text/plain", b"content"),
        )

    assert not (parent / "download.txt").exists()
