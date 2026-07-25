from __future__ import annotations

import contextlib
import importlib.util
import os
import stat
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
_LEGACY_BOOTSTRAP_PROCESS_LOCK = threading.Lock()


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
    """Return the legacy TOML source path selected by the environment."""
    raw = os.getenv("MCP_EMAIL_SERVER_CONFIG_PATH", default)
    return Path(os.path.abspath(Path(raw).expanduser()))


def bootstrap_path(config_path: Path | None = None) -> Path:
    """Derive the private bootstrap sidecar without sharing the legacy source file."""
    source = configured_path() if config_path is None else Path(os.path.abspath(config_path.expanduser()))
    name = f"{source.stem}.bootstrap.toml" if source.suffix else f"{source.name}.bootstrap.toml"
    return source.with_name(name)


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
    source_path = configured_path() if path is None else Path(os.path.abspath(path.expanduser()))
    authority_path = bootstrap_path(source_path)
    sidecar_exists = authority_path.exists() or authority_path.is_symlink()
    if not sidecar_exists:
        if not source_path.exists() and not source_path.is_symlink():
            return Bootstrap(path=source_path, mode="legacy", db_path=None, version=None, revision=0, exists=False)
        try:
            raw = tomllib.loads(source_path.read_text())
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            # Legacy source validity is reported by the legacy loader. It is not
            # bootstrap authority unless it contains the old combined-file marker.
            return Bootstrap(path=source_path, mode="legacy", db_path=None, version=None, revision=0, exists=False)
        if not isinstance(raw, dict) or "bootstrap_version" not in raw:
            return Bootstrap(path=source_path, mode="legacy", db_path=None, version=None, revision=0, exists=False)
        # Read-only compatibility for pre-release combined config/bootstrap files.
        # The first selection write creates the sidecar without touching this file.
        authority_path = source_path
    else:
        if authority_path.is_symlink() or not authority_path.is_file():
            raise BootstrapError(f"Bootstrap configuration must be a regular file and not a symlink: {authority_path}")
        try:
            raw = tomllib.loads(authority_path.read_text())
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise BootstrapError(f"Could not parse bootstrap configuration: {authority_path}") from exc
        if not isinstance(raw, dict):
            raise BootstrapError(f"Bootstrap configuration must contain a TOML table: {authority_path}")

    mode = _parse_mode(raw.get("mode"), authority_path)
    raw_version = raw.get("bootstrap_version")
    version = raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else None
    raw_revision = raw.get("bootstrap_revision", 0)
    if not isinstance(raw_revision, int) or isinstance(raw_revision, bool) or raw_revision < 0:
        raise BootstrapError(f"Invalid bootstrap revision in {authority_path}")
    revision = raw_revision
    raw_managed_db_path = raw.get("managed_db_location")
    raw_managed_selection = raw.get("managed_selection")
    if raw_managed_selection is not None and not isinstance(raw_managed_selection, bool):
        raise BootstrapError(f"Invalid managed_selection in {authority_path}")
    if version != BOOTSTRAP_VERSION:
        raise BootstrapError(f"Unsupported bootstrap version in {authority_path}: expected {BOOTSTRAP_VERSION}")

    db_path: Path | None = None
    if sidecar_exists and raw_managed_selection is None:
        raise BootstrapError(f"Bootstrap selection marker is missing: {authority_path}")
    if raw_managed_selection is False:
        if raw_managed_db_path is not None:
            raise BootstrapError(f"Unselected bootstrap must not contain managed_db_location: {authority_path}")
    else:
        raw_db_path = raw_managed_db_path
        if raw_db_path is None and not sidecar_exists and (mode == "managed" or revision > 0):
            raw_db_path = raw.get("db_location")
        if raw_managed_selection is True or raw_db_path is not None or mode == "managed":
            if not isinstance(raw_db_path, str) or not raw_db_path.strip():
                raise BootstrapError(f"Selected bootstrap requires managed_db_location: {authority_path}")
            candidate = Path(raw_db_path).expanduser()
            if not candidate.is_absolute():
                candidate = authority_path.parent / candidate
            db_path = Path(os.path.abspath(candidate))
    if mode == "managed" and db_path is None:
        raise BootstrapError(f"Managed bootstrap requires a selected database: {authority_path}")

    if db_path is not None and require_secure_managed:
        _assert_owner_only_directory(authority_path.parent, label="Managed bootstrap parent")
        _assert_owner_only_file(authority_path, label="Managed bootstrap configuration")

    return Bootstrap(
        path=source_path,
        mode=mode,
        db_path=db_path,
        version=version,
        revision=revision,
        exists=True,
    )


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
def bootstrap_file_lock(path: Path, *, require_secure_parent: bool = True) -> Iterator[None]:
    source_path = Path(os.path.abspath(path.expanduser()))
    authority_path = bootstrap_path(source_path)
    if require_secure_parent:
        # Managed authority writes always require owner-only no-follow files and
        # a cross-process lock; _ensure_private_parent fails closed otherwise.
        _ensure_private_parent(authority_path)
    elif not _SECURE_BOOTSTRAP_FILES_SUPPORTED:
        # Legacy-only writes predate managed bootstrap requirements. Retain their
        # platform behavior while serializing this process and without creating a
        # misleading lock file.
        with _LEGACY_BOOTSTRAP_PROCESS_LOCK:
            yield
        return
    elif not authority_path.parent.exists():
        # Keep a newly required immediate parent suitable for later managed
        # initialization; never change an existing legacy directory here.
        authority_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = Path(f"{authority_path}.lock")
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


def _materialize_bootstrap_locked(current: Bootstrap) -> Bootstrap:
    """Copy read-only combined-file authority into the sidecar without a revision change."""
    authority_path = bootstrap_path(current.path)
    if authority_path.exists() or authority_path.is_symlink() or not current.exists:
        return current
    raw: dict[str, object] = {
        "bootstrap_version": current.version or BOOTSTRAP_VERSION,
        "bootstrap_revision": current.revision,
        "mode": current.mode,
        "managed_selection": current.db_path is not None,
    }
    if current.db_path is not None:
        raw["managed_db_location"] = current.db_path.as_posix()
    _atomic_write(authority_path, tomli_w.dumps(raw))
    return read_bootstrap(current.path)


def _write_bootstrap_locked(
    *,
    mode: Mode,
    db_path: Path | None,
    path: Path,
    expected_revision: int | None,
    expected_exists: bool | None,
) -> Bootstrap:
    """Write bootstrap authority while the shared source/selection lock is held."""
    current = read_bootstrap(path)
    if expected_revision is not None and current.revision != expected_revision:
        raise BootstrapRevisionError("Bootstrap selection changed; reload and retry")
    if expected_exists is not None and current.exists is not expected_exists:
        raise BootstrapRevisionError("Bootstrap existence changed; reload and retry")
    selected_db = db_path if db_path is not None else current.db_path
    if mode == "managed" and selected_db is None:
        raise BootstrapError("Selecting managed mode requires a database path")

    raw: dict[str, object] = {
        "bootstrap_version": BOOTSTRAP_VERSION,
        "bootstrap_revision": current.revision + 1,
        "mode": mode,
        "managed_selection": selected_db is not None,
    }
    if selected_db is not None:
        raw["managed_db_location"] = Path(os.path.abspath(selected_db.expanduser())).as_posix()
    _atomic_write(bootstrap_path(path), tomli_w.dumps(raw))
    return read_bootstrap(path)


def write_bootstrap(
    *,
    mode: Mode,
    db_path: Path | None = None,
    path: Path | None = None,
    expected_revision: int | None = None,
    expected_exists: bool | None = None,
) -> Bootstrap:
    source_path = configured_path() if path is None else Path(os.path.abspath(path.expanduser()))
    with bootstrap_file_lock(source_path):
        return _write_bootstrap_locked(
            mode=mode,
            db_path=db_path,
            path=source_path,
            expected_revision=expected_revision,
            expected_exists=expected_exists,
        )
