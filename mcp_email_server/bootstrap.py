from __future__ import annotations

import contextlib
import importlib.util
import os
import stat
import tempfile
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomli_w

BOOTSTRAP_VERSION = 1
BOOTSTRAP_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_CONFIG_PATH = "~/.config/mcp-email-server/config.toml"
Mode = Literal["legacy", "managed"]
_SECURE_BOOTSTRAP_FILES_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "fchmod")
    and hasattr(os, "getuid")
    and importlib.util.find_spec("fcntl") is not None
)


class BootstrapError(ValueError):
    """The bootstrap configuration is invalid or unsafe."""


class ManagedModeWriteError(RuntimeError):
    """A legacy writer was invoked while managed mode is selected."""


class BootstrapRevisionError(BootstrapError):
    """The bootstrap selection changed before a guarded write."""


@dataclass(frozen=True)
class Bootstrap:
    path: Path
    mode: Mode
    db_path: Path | None
    version: int | None
    revision: int
    exists: bool


_PROCESS_BOOTSTRAP: Bootstrap | None = None


def configured_path(default: str = DEFAULT_CONFIG_PATH) -> Path:
    raw = os.getenv("MCP_EMAIL_SERVER_CONFIG_PATH", default)
    return Path(os.path.abspath(Path(raw).expanduser()))


def _parse_mode(value: object, path: Path) -> Mode:
    if value in (None, "legacy"):
        return "legacy"
    if value == "managed":
        return "managed"
    raise BootstrapError(f"Invalid bootstrap mode in {path}: expected 'legacy' or 'managed'")


def _require_secure_bootstrap_files() -> None:
    if not _SECURE_BOOTSTRAP_FILES_SUPPORTED:
        raise BootstrapError(
            "Bootstrap management is unavailable because this platform cannot enforce owner-only no-follow files"
        )


def _assert_owner_only_directory(path: Path, *, label: str) -> None:
    _require_secure_bootstrap_files()
    current_uid = os.getuid()
    for index, candidate in enumerate((path, *path.parents)):
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise BootstrapError(f"Could not inspect {label} chain: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapError(f"{label} chain must contain real directories: {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        if index == 0:
            if metadata.st_uid != current_uid:
                raise BootstrapError(f"{label} must be owned by the current user: {path}")
            if mode & 0o077:
                raise BootstrapError(f"{label} must not be accessible by group or other users: {path}")
        elif metadata.st_uid not in {0, current_uid} or (mode & 0o022 and not metadata.st_mode & stat.S_ISVTX):
            raise BootstrapError(f"{label} ancestor permissions are unsafe: {candidate}")


def _assert_owner_only_file(path: Path, *, label: str) -> None:
    _require_secure_bootstrap_files()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BootstrapError(f"Could not inspect {label}: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BootstrapError(f"{label} must be a regular file and not a symlink: {path}")
    if metadata.st_uid != os.getuid():
        raise BootstrapError(f"{label} must be owned by the current user: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise BootstrapError(f"{label} must not be accessible by group or other users: {path}")


def read_bootstrap(  # noqa: C901
    path: Path | None = None, *, require_secure_managed: bool = True
) -> Bootstrap:
    path = configured_path() if path is None else Path(os.path.abspath(path.expanduser()))
    if not path.exists() and not path.is_symlink():
        return Bootstrap(path=path, mode="legacy", db_path=None, version=None, revision=0, exists=False)
    if path.is_symlink() or not path.is_file():
        raise BootstrapError(f"Bootstrap configuration must be a regular file and not a symlink: {path}")

    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BootstrapError(f"Could not parse bootstrap configuration: {path}") from exc
    if not isinstance(raw, dict):
        raise BootstrapError(f"Bootstrap configuration must contain a TOML table: {path}")

    mode = _parse_mode(raw.get("mode"), path)
    raw_version = raw.get("bootstrap_version")
    version = raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else None
    raw_revision = raw.get("bootstrap_revision", 0)
    if not isinstance(raw_revision, int) or isinstance(raw_revision, bool) or raw_revision < 0:
        raise BootstrapError(f"Invalid bootstrap revision in {path}")
    revision = raw_revision
    db_path: Path | None = None

    if mode == "managed":
        if version != BOOTSTRAP_VERSION:
            raise BootstrapError(f"Unsupported managed bootstrap version in {path}: expected {BOOTSTRAP_VERSION}")
        raw_db_path = raw.get("db_location")
        if not isinstance(raw_db_path, str) or not raw_db_path.strip():
            raise BootstrapError(f"Managed bootstrap requires db_location: {path}")
        candidate = Path(raw_db_path).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        db_path = Path(os.path.abspath(candidate))
        if require_secure_managed:
            _assert_owner_only_directory(path.parent, label="Managed bootstrap parent")
            _assert_owner_only_file(path, label="Managed bootstrap configuration")
    elif raw_version is not None and version != BOOTSTRAP_VERSION:
        raise BootstrapError(f"Unsupported bootstrap version in {path}: expected {BOOTSTRAP_VERSION}")
    else:
        raw_db_path = raw.get("db_location")
        if isinstance(raw_db_path, str) and raw_db_path.strip():
            candidate = Path(raw_db_path).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            db_path = Path(os.path.abspath(candidate))

    return Bootstrap(path=path, mode=mode, db_path=db_path, version=version, revision=revision, exists=True)


def freeze_process_bootstrap(path: Path | None = None) -> Bootstrap:
    """Capture runtime authority once; mode changes require a process restart."""
    global _PROCESS_BOOTSTRAP
    candidate = read_bootstrap(path)
    if _PROCESS_BOOTSTRAP is None:
        _PROCESS_BOOTSTRAP = candidate
    elif _PROCESS_BOOTSTRAP.path != candidate.path:
        # Test suites and embedded callers may host independent configurations
        # sequentially in one interpreter. A production transport freezes one path.
        _PROCESS_BOOTSTRAP = candidate
    return _PROCESS_BOOTSTRAP


def process_bootstrap(path: Path | None = None) -> Bootstrap:
    target = configured_path() if path is None else Path(os.path.abspath(path.expanduser()))
    if _PROCESS_BOOTSTRAP is not None and _PROCESS_BOOTSTRAP.path == target:
        return _PROCESS_BOOTSTRAP
    return read_bootstrap(target)


def effective_mode(path: Path | None = None) -> Mode:
    return process_bootstrap(path).mode


def assert_legacy_writable(operation: str, path: Path | None = None) -> None:
    if effective_mode(path) == "managed":
        raise ManagedModeWriteError(
            f"Cannot {operation} while managed mode is selected. Use the managed `config` and "
            "`account` CLI commands, or run `mcp-email-server config select legacy` and restart."
        )


def _ensure_private_parent(path: Path) -> None:
    _require_secure_bootstrap_files()
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    _assert_owner_only_directory(parent, label="Bootstrap parent")


@contextlib.contextmanager
def _bootstrap_lock(path: Path) -> Iterator[None]:
    _ensure_private_parent(path)
    lock_path = Path(f"{path}.lock")
    if lock_path.exists() or lock_path.is_symlink():
        _assert_owner_only_file(lock_path, label="Bootstrap lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise BootstrapError("Bootstrap lock could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        checked = lock_path.lstat()
        _assert_owner_only_file(lock_path, label="Bootstrap lock")
        if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
            raise BootstrapError("Bootstrap lock changed while it was opened")
        import fcntl

        deadline = time.monotonic() + BOOTSTRAP_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise BootstrapError("Bootstrap lock is busy") from exc
                time.sleep(0.05)
        yield
    finally:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(path: Path, content: str) -> None:
    _ensure_private_parent(path)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_name, path)
        with contextlib.suppress(OSError):
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def write_bootstrap(
    *,
    mode: Mode,
    db_path: Path | None = None,
    path: Path | None = None,
    expected_revision: int | None = None,
) -> Bootstrap:
    path = configured_path() if path is None else Path(os.path.abspath(path.expanduser()))
    with _bootstrap_lock(path):
        current = read_bootstrap(path)
        if expected_revision is not None and current.revision != expected_revision:
            raise BootstrapRevisionError("Bootstrap selection changed; reload and retry")
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                parsed = tomllib.loads(path.read_text())
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise BootstrapError(f"Could not parse bootstrap configuration: {path}") from exc
            if not isinstance(parsed, dict):
                raise BootstrapError(f"Bootstrap configuration must contain a TOML table: {path}")
            raw = parsed

        selected_db = db_path
        if selected_db is None:
            existing = raw.get("db_location")
            if isinstance(existing, str) and existing.strip():
                candidate = Path(existing).expanduser()
                selected_db = candidate if candidate.is_absolute() else path.parent / candidate
        if mode == "managed" and selected_db is None:
            raise BootstrapError("Selecting managed mode requires a database path")

        raw["bootstrap_version"] = BOOTSTRAP_VERSION
        raw["bootstrap_revision"] = current.revision + 1
        raw["mode"] = mode
        if selected_db is not None:
            raw["db_location"] = Path(os.path.abspath(selected_db.expanduser())).as_posix()
        _atomic_write(path, tomli_w.dumps(raw))
        return read_bootstrap(path)
