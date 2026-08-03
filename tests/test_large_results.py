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

from mcp_email_server import large_results as large_results_module
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
                in_reply_to="<parent@example.test>",
                references="<root@example.test> <parent@example.test>",
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


def test_large_result_metadata_requires_stable_identity() -> None:
    with pytest.raises(TypeError, match="stable identity"):
        large_results_module._metadata_key(object())


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
async def test_large_result_writer_removes_root_after_consumer_already_removed_artifact() -> None:
    writer = LocalLargeResultWriter()
    reference = await writer.write(prefix="email-content", content=b"consumed")
    path = Path(reference.output_file_path)
    root = path.parent
    path.unlink()

    await writer.aclose()

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
@pytest.mark.parametrize("failure", ["prefix", "size", "closed"])
async def test_large_result_writer_rejects_invalid_or_closed_writes(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    writer = LocalLargeResultWriter()
    prefix = "email-content"
    content = b"{}"
    expected = ""
    if failure == "prefix":
        prefix = "../unsafe"
        expected = "prefix is invalid"
    elif failure == "size":
        monkeypatch.setattr(
            large_results_module,
            "APPLICATION_LIMITS",
            replace(APPLICATION_LIMITS, spill_file_bytes=1),
        )
        expected = "exceeds the spill byte limit"
    else:
        await writer.aclose()
        expected = "writer is closed"

    with pytest.raises((ValueError, RuntimeError), match=expected):
        await writer.write(prefix=prefix, content=content)

    await writer.aclose()


@pytest.mark.skipif(os.name != "posix", reason="Owner and mode validation is POSIX-specific")
def test_large_result_writer_rejects_invalid_root_and_file_metadata(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"data")
    regular.chmod(0o600)
    with pytest.raises(RuntimeError, match="directory is not a real directory"):
        LocalLargeResultWriter._validate_root(regular)

    insecure_root = tmp_path / "root"
    insecure_root.mkdir(mode=0o755)
    insecure_root.chmod(0o755)
    with pytest.raises(RuntimeError, match="directory is not owner-only"):
        LocalLargeResultWriter._validate_root(insecure_root)

    directory = tmp_path / "artifact-directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="artifact is not a regular file"):
        LocalLargeResultWriter._validate_file(directory, expected_size=0)

    with pytest.raises(RuntimeError, match="artifact size changed"):
        LocalLargeResultWriter._validate_file(regular, expected_size=5)

    regular.chmod(0o644)
    with pytest.raises(RuntimeError, match="artifact is not owner-only"):
        LocalLargeResultWriter._validate_file(regular, expected_size=4)

    regular.chmod(0o600)
    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(regular)
    with pytest.raises(RuntimeError, match="artifact is not owner-only"):
        LocalLargeResultWriter._validate_file(regular, expected_size=4)

    wrong_owner = Mock()
    wrong_owner.lstat.return_value = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_size=4,
        st_uid=os.getuid() + 1,
        st_nlink=1,
    )
    with pytest.raises(RuntimeError, match="artifact is not owner-only"):
        LocalLargeResultWriter._validate_file(wrong_owner, expected_size=4)


@pytest.mark.skipif(os.name != "posix", reason="Injected fsync failure is POSIX-specific")
@pytest.mark.asyncio
async def test_large_result_writer_removes_partial_artifact_after_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = LocalLargeResultWriter()
    root, _identity = writer._ensure_root()
    monkeypatch.setattr(large_results_module.os, "fsync", Mock(side_effect=OSError("write failed")))

    with pytest.raises(OSError, match="write failed"):
        await writer.write(prefix="email-content", content=b"secret")

    assert list(root.iterdir()) == []
    await writer.aclose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX identity injection has native Windows coverage")
@pytest.mark.asyncio
async def test_large_result_writer_rejects_artifact_identity_change(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = LocalLargeResultWriter()
    root, _identity = writer._ensure_root()
    monkeypatch.setattr(
        writer,
        "_validate_file",
        Mock(return_value=SimpleNamespace(st_dev=-1, st_ino=-1)),
    )

    with pytest.raises(RuntimeError, match="artifact identity changed"):
        await writer.write(prefix="email-content", content=b"secret")

    assert list(root.iterdir()) == []
    await writer.aclose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX temporary-root injection has native Windows coverage")
def test_large_result_writer_removes_root_when_initial_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    writer = LocalLargeResultWriter()
    root = tmp_path / "results"

    def make_root(*, prefix: str) -> str:
        assert prefix == "mcp-email-server-results-"
        root.mkdir()
        return root.as_posix()

    monkeypatch.setattr(large_results_module.tempfile, "mkdtemp", make_root)
    monkeypatch.setattr(writer, "_validate_root", Mock(side_effect=RuntimeError("validation failed")))

    with pytest.raises(RuntimeError, match="validation failed"):
        writer._ensure_root()

    assert not root.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX identity metadata has native Windows coverage")
def test_large_result_writer_rejects_missing_root_identity(tmp_path: Path) -> None:
    writer = LocalLargeResultWriter()
    root = tmp_path / "results"
    root.mkdir(mode=0o700)
    writer._root = root

    with pytest.raises(RuntimeError, match="directory identity is unavailable"):
        writer._ensure_root()


@pytest.mark.skipif(os.name != "posix", reason="POSIX identity metadata has native Windows coverage")
@pytest.mark.asyncio
async def test_large_result_writer_rejects_root_identity_change() -> None:
    writer = LocalLargeResultWriter()
    root, identity = writer._ensure_root()
    writer._root_identity = (identity[0], identity[1] + 1)

    with pytest.raises(RuntimeError, match="directory identity changed"):
        await writer.write(prefix="email-content", content=b"secret")

    root.rmdir()


@pytest.mark.skipif(os.name != "posix", reason="POSIX validation injection has native Windows coverage")
@pytest.mark.asyncio
async def test_large_result_writer_close_fails_safe_when_root_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = LocalLargeResultWriter()
    root, _identity = writer._ensure_root()
    monkeypatch.setattr(writer, "_validate_root", Mock(side_effect=OSError("root unavailable")))

    await writer.aclose()

    assert root.exists()
    root.rmdir()


@pytest.mark.skipif(os.name != "posix", reason="POSIX validation injection has native Windows coverage")
@pytest.mark.asyncio
async def test_large_result_writer_close_preserves_unverifiable_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = LocalLargeResultWriter()
    reference = await writer.write(prefix="email-content", content=b"secret")
    path = Path(reference.output_file_path)
    root = path.parent
    monkeypatch.setattr(writer, "_validate_file", Mock(side_effect=OSError("artifact unavailable")))

    await writer.aclose()

    assert path.exists()
    path.unlink()
    root.rmdir()


@pytest.mark.asyncio
async def test_large_result_writer_close_is_idempotent_without_artifacts() -> None:
    writer = LocalLargeResultWriter()

    await writer.aclose()
    await writer.aclose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX identity metadata has native Windows coverage")
@pytest.mark.asyncio
async def test_large_result_writer_close_fails_safe_for_changed_root_identity() -> None:
    writer = LocalLargeResultWriter()
    root, identity = writer._ensure_root()
    writer._root_identity = (identity[0], identity[1] + 1)

    await writer.aclose()

    assert root.exists()
    root.rmdir()


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
    assert stored["emails"][0]["in_reply_to"] == response.emails[0].in_reply_to
    assert stored["emails"][0]["references"] == response.emails[0].references
    await writer.aclose()
