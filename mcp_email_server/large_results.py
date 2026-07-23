from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import stat
import tempfile
import threading
import uuid
from pathlib import Path

from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.reads import LargeResultReference

_SAFE_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_SECURE_LOCAL_RESULTS_SUPPORTED = (
    os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "fchmod") and hasattr(os, "getuid")
)


def local_large_results_supported() -> bool:
    """Return whether owner-only no-follow spill storage can be enforced."""
    return _SECURE_LOCAL_RESULTS_SUPPORTED


class LocalLargeResultWriter:
    """Process-owned, owner-only spill storage for bounded local tool results."""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._root_identity: tuple[int, int] | None = None
        self._paths: set[Path] = set()
        self._lock = threading.Lock()
        self._closed = False

    def _ensure_root(self) -> tuple[Path, tuple[int, int]]:
        if not _SECURE_LOCAL_RESULTS_SUPPORTED:
            raise RuntimeError(
                "Oversized local results are unavailable because this platform cannot enforce owner-only storage"
            )
        if self._root is None:
            root = Path(tempfile.mkdtemp(prefix="mcp-email-server-results-"))
            try:
                root.chmod(0o700)
                metadata = self._validate_root(root)
            except BaseException:
                with contextlib.suppress(OSError):
                    root.rmdir()
                raise
            self._root = root
            self._root_identity = (metadata.st_dev, metadata.st_ino)
        if self._root_identity is None:
            raise RuntimeError("Large-result directory identity is unavailable")
        return self._root, self._root_identity

    @staticmethod
    def _validate_root(path: Path) -> os.stat_result:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("Large-result directory is not a real directory")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError("Large-result directory is not owner-only")
        return metadata

    @staticmethod
    def _validate_file(path: Path, *, expected_size: int) -> os.stat_result:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("Large-result artifact is not a regular file")
        if metadata.st_size != expected_size:
            raise RuntimeError("Large-result artifact size changed during write")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise RuntimeError("Large-result artifact is not owner-only")
        return metadata

    def _write(self, *, prefix: str, content: bytes) -> LargeResultReference:
        if not _SAFE_PREFIX.fullmatch(prefix):
            raise ValueError("Large-result prefix is invalid")
        if len(content) > APPLICATION_LIMITS.spill_file_bytes:
            raise ValueError("Large-result artifact exceeds the spill byte limit")
        with self._lock:
            if self._closed:
                raise RuntimeError("Large-result writer is closed")
            root_path, root_identity = self._ensure_root()
            root = self._validate_root(root_path)
            if (root.st_dev, root.st_ino) != root_identity:
                raise RuntimeError("Large-result directory identity changed")
            path = root_path / f"{prefix}-{uuid.uuid4().hex[:16]}.json"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(content)
                offset = 0
                while offset < len(view):
                    offset += os.write(descriptor, view[offset:])
                os.fsync(descriptor)
                opened = os.fstat(descriptor)
            except BaseException:
                os.close(descriptor)
                with contextlib.suppress(OSError):
                    path.unlink()
                raise
            os.close(descriptor)
            checked = self._validate_file(path, expected_size=len(content))
            if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
                with contextlib.suppress(OSError):
                    path.unlink()
                raise RuntimeError("Large-result artifact identity changed during write")
            self._paths.add(path)
        return LargeResultReference(
            output_file_path=path.as_posix(),
            output_bytes=len(content),
            output_sha256=hashlib.sha256(content).hexdigest(),
        )

    async def write(self, *, prefix: str, content: bytes) -> LargeResultReference:
        return await asyncio.to_thread(self._write, prefix=prefix, content=content)

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._root is None or self._root_identity is None:
                return
            try:
                root = self._validate_root(self._root)
            except OSError:
                return
            if (root.st_dev, root.st_ino) != self._root_identity:
                return
            for path in self._paths:
                try:
                    self._validate_file(path, expected_size=path.lstat().st_size)
                    path.unlink()
                except OSError:
                    continue
            with contextlib.suppress(OSError):
                self._root.rmdir()

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close)
