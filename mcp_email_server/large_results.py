from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import stat
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.reads import LargeResultReference
from mcp_email_server.windows_security import (
    WindowsFileIdentity,
    WindowsSecurityError,
    create_private_temp_directory,
    remove_private_directory,
    remove_private_file,
    secure_file_lock,
    validate_private_directory,
    validate_private_file,
    windows_security_supported,
    write_private_new,
)

_SAFE_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_SECURE_LOCAL_RESULTS_SUPPORTED = (
    os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "fchmod") and hasattr(os, "getuid")
) or (os.name == "nt" and windows_security_supported())

SecurityMetadata = os.stat_result | WindowsFileIdentity


def _metadata_key(metadata: SecurityMetadata | object) -> tuple[int, int]:
    if isinstance(metadata, WindowsFileIdentity):  # pragma: no cover - native Windows CI
        return metadata.key
    device = getattr(metadata, "st_dev", None)
    inode = getattr(metadata, "st_ino", None)
    if not isinstance(device, int) or not isinstance(inode, int):
        raise TypeError("Filesystem metadata has no stable identity")
    return (device, inode)


def local_large_results_supported() -> bool:
    """Return whether owner-only no-follow spill storage can be enforced."""
    return _SECURE_LOCAL_RESULTS_SUPPORTED


# POSIX-only flags are absent from Windows typeshed stubs.
_posix_os: Any = os


class LocalLargeResultWriter:
    """Process-owned, owner-only spill storage for bounded local tool results."""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._root_identity: tuple[int, int] | None = None
        self._root_windows_identity: WindowsFileIdentity | None = None
        self._root_windows_lock: contextlib.AbstractContextManager[None] | None = None
        self._root_windows_lock_identity: WindowsFileIdentity | None = None
        self._paths: dict[Path, SecurityMetadata] = {}
        self._lock = threading.Lock()
        self._closed = False

    def _ensure_root(self) -> tuple[Path, tuple[int, int]]:
        if not _SECURE_LOCAL_RESULTS_SUPPORTED:
            raise RuntimeError(
                "Oversized local results are unavailable because this platform cannot enforce owner-only storage"
            )
        if self._root is None:
            if os.name == "nt":  # pragma: no cover - native Windows CI
                try:
                    root, windows_identity = create_private_temp_directory()
                except WindowsSecurityError as exc:
                    raise RuntimeError("Large-result directory could not be secured on Windows") from exc
                metadata: SecurityMetadata = windows_identity
                self._root_windows_identity = windows_identity
                lock_path = root / ".owner.lock"
                root_lock = secure_file_lock(lock_path, timeout=0)
                try:
                    root_lock.__enter__()
                    lock_identity = validate_private_file(lock_path)
                except BaseException:
                    root_lock.__exit__(*sys.exc_info())
                    with contextlib.suppress(OSError, WindowsSecurityError):
                        failed_lock_identity = validate_private_file(lock_path)
                        remove_private_file(lock_path, failed_lock_identity)
                    remove_private_directory(root, windows_identity)
                    raise
                self._root_windows_lock = root_lock
                self._root_windows_lock_identity = lock_identity
            else:
                root = Path(tempfile.mkdtemp(prefix="mcp-email-server-results-"))
                try:
                    root.chmod(0o700)
                    metadata = self._validate_root(root)
                except BaseException:
                    with contextlib.suppress(OSError):
                        root.rmdir()
                    raise
            self._root = root
            self._root_identity = _metadata_key(metadata)
        if self._root_identity is None:
            raise RuntimeError("Large-result directory identity is unavailable")
        return self._root, self._root_identity

    @staticmethod
    def _validate_root(path: Path) -> SecurityMetadata:
        if os.name == "nt":  # pragma: no cover - native Windows CI
            try:
                return validate_private_directory(path)
            except WindowsSecurityError as exc:
                raise RuntimeError("Large-result directory is not private Windows storage") from exc
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("Large-result directory is not a real directory")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError("Large-result directory is not owner-only")
        return metadata

    @staticmethod
    def _validate_file(path: Path, *, expected_size: int) -> SecurityMetadata:
        if os.name == "nt":  # pragma: no cover - native Windows CI
            try:
                return validate_private_file(path, expected_size=expected_size)
            except WindowsSecurityError as exc:
                raise RuntimeError("Large-result artifact is not a private Windows file") from exc
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("Large-result artifact is not a regular file")
        if metadata.st_size != expected_size:
            raise RuntimeError("Large-result artifact size changed during write")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise RuntimeError("Large-result artifact is not owner-only")
        return metadata

    @staticmethod
    def _write_posix_artifact(path: Path, content: bytes) -> os.stat_result:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _posix_os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(content)
            offset = 0
            while offset < len(view):
                offset += os.write(descriptor, view[offset:])
            os.fsync(descriptor)
            return os.fstat(descriptor)
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_platform_artifact(path: Path, content: bytes) -> SecurityMetadata:
        if os.name == "nt":  # pragma: no cover - native Windows CI
            try:
                return write_private_new(path, content)
            except WindowsSecurityError as exc:
                raise RuntimeError("Large-result artifact could not be written safely on Windows") from exc
        return LocalLargeResultWriter._write_posix_artifact(path, content)

    @staticmethod
    def _remove_identity_checked(path: Path, metadata: SecurityMetadata) -> None:
        if isinstance(metadata, WindowsFileIdentity):  # pragma: no cover - native Windows CI
            remove_private_file(path, metadata)
        else:
            with contextlib.suppress(OSError):
                path.unlink()

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
            if _metadata_key(root) != root_identity:
                raise RuntimeError("Large-result directory identity changed")
            path = root_path / f"{prefix}-{uuid.uuid4().hex[:16]}.json"
            opened = self._write_platform_artifact(path, content)
            checked = self._validate_file(path, expected_size=len(content))
            if _metadata_key(opened) != _metadata_key(checked):
                self._remove_identity_checked(path, opened)
                raise RuntimeError("Large-result artifact identity changed during write")
            self._paths[path] = checked
        return LargeResultReference(
            output_file_path=path.as_posix(),
            output_bytes=len(content),
            output_sha256=hashlib.sha256(content).hexdigest(),
        )

    async def write(self, *, prefix: str, content: bytes) -> LargeResultReference:
        return await asyncio.to_thread(self._write, prefix=prefix, content=content)

    def _remove_artifacts(self) -> bool:
        removed_all = True
        for path, expected in self._paths.items():
            if isinstance(expected, WindowsFileIdentity):  # pragma: no cover - native Windows CI
                try:
                    checked = validate_private_file(path, expected_size=expected.size)
                except FileNotFoundError:
                    continue
                except (OSError, WindowsSecurityError):
                    removed_all = False
                    continue
                if checked.key != expected.key or not remove_private_file(path, expected):
                    removed_all = False
                continue
            try:
                checked = self._validate_file(path, expected_size=int(expected.st_size))
                if _metadata_key(checked) != _metadata_key(expected):
                    removed_all = False
                    continue
                path.unlink()
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError):
                removed_all = False
        return removed_all

    def _release_windows_root_lock(self) -> None:
        if self._root_windows_lock is not None:  # pragma: no cover - native Windows CI
            self._root_windows_lock.__exit__(None, None, None)
            self._root_windows_lock = None

    def _remove_root(self) -> None:
        if self._root is None:
            return
        if os.name == "nt" and self._root_windows_identity is not None:  # pragma: no cover - native Windows CI
            self._release_windows_root_lock()
            if self._root_windows_lock_identity is not None:
                remove_private_file(self._root / ".owner.lock", self._root_windows_lock_identity)
            remove_private_directory(self._root, self._root_windows_identity)
            return
        with contextlib.suppress(OSError):
            self._root.rmdir()

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._root is None or self._root_identity is None:
                return
            try:
                root = self._validate_root(self._root)
            except (OSError, RuntimeError):
                self._release_windows_root_lock()
                return
            if _metadata_key(root) != self._root_identity:
                self._release_windows_root_lock()
                return
            if self._remove_artifacts():
                self._remove_root()
            else:
                # Keep the marker for later validated stale cleanup, but release
                # the live-process lock when this writer closes.
                self._release_windows_root_lock()

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close)
