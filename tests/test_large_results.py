from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mcp_email_server.application import limits as limits_module
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.reads import (
    EmailContentService,
    GetEmailContentQuery,
    ReadAccountSnapshot,
    ReadProviderAccess,
)
from mcp_email_server.emails.models import EmailBodyResponse, EmailContentBatchResponse
from mcp_email_server.large_results import LocalLargeResultWriter


def _content_response(body: str) -> EmailContentBatchResponse:
    return EmailContentBatchResponse(
        emails=[
            EmailBodyResponse(
                email_id="1",
                subject="Subject",
                sender="alice@example.test",
                recipients=["bob@example.test"],
                date=datetime(2026, 7, 23, tzinfo=UTC),
                attachments=[],
                body=body,
            )
        ],
        requested_count=1,
        retrieved_count=1,
        failed_ids=[],
    )


@pytest.mark.asyncio
async def test_large_result_writer_fails_closed_lazily_without_owner_only_storage(monkeypatch) -> None:
    monkeypatch.setattr("mcp_email_server.large_results._SECURE_LOCAL_RESULTS_SUPPORTED", False)
    writer = LocalLargeResultWriter()

    with pytest.raises(RuntimeError, match="platform cannot enforce"):
        await writer.write(prefix="emails", content=b"{}")

    await writer.aclose()


@pytest.mark.asyncio
async def test_large_result_writer_creates_private_integrity_checked_artifact() -> None:
    writer = LocalLargeResultWriter()
    content = b'{"message":"private local content"}'

    reference = await writer.write(prefix="email-content", content=content)
    path = Path(reference.output_file_path)
    root = path.parent

    assert path.read_bytes() == content
    assert reference.output_bytes == len(content)
    assert reference.output_sha256 == hashlib.sha256(content).hexdigest()
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_nlink == 1

    await writer.aclose()
    assert not path.exists()
    assert not root.exists()


@pytest.mark.asyncio
async def test_large_result_writer_rejects_precreated_symlink() -> None:
    writer = LocalLargeResultWriter()
    root, _identity = writer._ensure_root()
    target = root / "outside.json"
    target.write_text("outside")
    target.chmod(0o600)
    name = "email-content-aaaaaaaaaaaaaaaa.json"
    (root / name).symlink_to(target)

    with (
        patch(
            "mcp_email_server.large_results.uuid.uuid4",
            return_value=SimpleNamespace(hex="a" * 32),
        ),
        pytest.raises(FileExistsError),
    ):
        await writer.write(prefix="email-content", content=b"secret")

    assert target.read_text() == "outside"
    (root / name).unlink()
    target.unlink()
    await writer.aclose()


@pytest.mark.asyncio
async def test_content_service_spills_oversized_result_and_returns_bounded_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _content_response("body-" * 1_000)
    serialized = response.model_dump_json().encode()
    monkeypatch.setattr(
        limits_module,
        "APPLICATION_LIMITS",
        replace(APPLICATION_LIMITS, serialized_response_bytes=512),
    )
    authority = Mock()
    authority.resolve.return_value = ReadAccountSnapshot("work", "legacy", (), False)
    provider = Mock()
    provider.get_content = AsyncMock(return_value=response)
    factory = Mock()
    factory.open.return_value = ReadProviderAccess(authority.resolve.return_value, provider)
    writer = LocalLargeResultWriter()
    service = EmailContentService(authority, factory, Mock(), writer)

    result = await service.execute(GetEmailContentQuery("work", ("1",)))

    assert result.content_omitted is True
    assert result.emails == []
    assert result.output_file_path is not None
    assert result.output_bytes == len(serialized)
    assert result.output_sha256 == hashlib.sha256(serialized).hexdigest()
    assert len(result.model_dump_json().encode()) <= 512
    stored = json.loads(Path(result.output_file_path).read_text())
    assert stored["emails"][0]["body"] == response.emails[0].body
    await writer.aclose()
