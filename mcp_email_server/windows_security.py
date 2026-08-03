from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

if os.name == "nt":  # pragma: win32 cover
    import ntsecuritycon as _ntsecuritycon
    import win32api as _win32api
    import win32con as _win32con
    import win32file as _win32file
    import win32security as _win32security
else:  # Keep imports and package installation portable on POSIX.
    _ntsecuritycon = None
    _win32api = None
    _win32con = None
    _win32file = None
    _win32security = None

# pywin32 ships no complete type information and is conditionally installed.
# Keep all dynamic binding access inside this adapter rather than leaking Any
# into callers.
ntsecuritycon: Any = _ntsecuritycon
win32api: Any = _win32api
win32con: Any = _win32con
win32file: Any = _win32file
win32security: Any = _win32security

_ATTACHMENT_TEMP_PREFIX = ".mcp-email-attachment-"
_BOOTSTRAP_TEMP_PREFIX = ".mcp-email-bootstrap-"
_RESULT_CONTAINER_NAME = "mcp-email-server-results"
_RESULT_ROOT_PREFIX = "result-"
_RESULT_LOCK_NAME = ".owner.lock"
_RESULT_ROOT = re.compile(rf"{re.escape(_RESULT_ROOT_PREFIX)}[0-9a-f]{{24}}\Z")
_RESULT_FILE = re.compile(r"[a-z][a-z0-9-]{0,31}-[0-9a-f]{16}\.json\Z")
_RESERVED_COMPONENTS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_STALE_AGE_SECONDS = 24 * 60 * 60
_MAX_STALE_CANDIDATES = 64
_MAX_STALE_ENTRIES_EXAMINED = 256
_MOVE_RETRY_SECONDS = 5.0
_MOVE_RETRY_INTERVAL_SECONDS = 0.01
_OPEN_RETRY_SECONDS = 0.5
_OPEN_RETRY_INTERVAL_SECONDS = 0.01
_TEMP_FILE_PATTERNS = {
    prefix: re.compile(rf"{re.escape(prefix)}[0-9a-f]{{32}}\.tmp\Z")
    for prefix in (_ATTACHMENT_TEMP_PREFIX, _BOOTSTRAP_TEMP_PREFIX)
}


class WindowsSecurityError(RuntimeError):
    """A Windows path cannot satisfy the local single-user security contract."""


@dataclass(frozen=True)
class WindowsFileIdentity:
    volume_serial: int
    file_index: int
    size: int
    links: int
    attributes: int

    @property
    def key(self) -> tuple[int, int]:
        return (self.volume_serial, self.file_index)


def windows_security_supported() -> bool:
    return os.name == "nt" and all(
        module is not None for module in (ntsecuritycon, win32api, win32con, win32file, win32security)
    )


def _require_windows() -> None:
    if not windows_security_supported():
        raise WindowsSecurityError("Secure Windows filesystem support is unavailable")


def _normalize(path: Path) -> Path:
    _require_windows()
    raw = os.fspath(path.expanduser())
    lowered = raw.replace("/", "\\").lower()
    if lowered.startswith(("\\\\", "\\?\\", "\\.\\", "//")):
        raise WindowsSecurityError("UNC, network, and device paths are unsupported")
    normalized = Path(os.path.abspath(raw))
    drive, tail = os.path.splitdrive(os.fspath(normalized))
    if not re.fullmatch(r"[A-Za-z]:", drive) or ":" in tail:
        raise WindowsSecurityError("The path must be an ordinary drive-letter path without alternate streams")
    for component in normalized.parts[1:]:
        if component.endswith((" ", ".")):
            raise WindowsSecurityError("Windows path components must not use trailing spaces or dots")
        base = component.split(".", 1)[0].casefold()
        if base in _RESERVED_COMPONENTS:
            raise WindowsSecurityError("Windows device-name path components are unsupported")
    return normalized


def _volume_root(path: Path) -> str:
    drive, _tail = os.path.splitdrive(os.fspath(path))
    return f"{drive}\\"


def _validate_volume(path: Path) -> None:
    root = _volume_root(path)
    try:
        drive_type = win32file.GetDriveType(root)
    except Exception as exc:
        raise WindowsSecurityError("The Windows destination drive type could not be inspected") from exc
    if drive_type != win32con.DRIVE_FIXED:
        raise WindowsSecurityError("Only a local fixed Windows volume is supported")
    try:
        volume = win32api.GetVolumeInformation(root)
    except Exception as exc:
        raise WindowsSecurityError("The Windows destination volume could not be inspected") from exc
    if len(volume) < 5 or str(volume[4]).upper() != "NTFS":
        raise WindowsSecurityError("Only a local fixed NTFS volume is supported")


def _error_code(exc: BaseException) -> object:
    code = getattr(exc, "winerror", None)
    if code is None and getattr(exc, "args", ()):
        code = exc.args[0]
    return code


def _missing_error(exc: BaseException) -> bool:
    return _error_code(exc) in {2, 3}


def _exists_error(exc: BaseException) -> bool:
    return _error_code(exc) in {80, 183}


def _close(handle: Any) -> None:
    with contextlib.suppress(Exception):
        handle.Close()


def _current_user_sid() -> Any:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        _close(token)


def _trusted_sid_strings() -> set[str]:
    return {
        win32security.ConvertSidToStringSid(_current_user_sid()),
        win32security.ConvertSidToStringSid(win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)),
        win32security.ConvertSidToStringSid(
            win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
        ),
    }


def _private_security_attributes() -> Any:
    user = _current_user_sid()
    trusted = (
        user,
        win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None),
        win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None),
    )
    acl = win32security.ACL()
    inheritance = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    for sid in trusted:
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inheritance, ntsecuritycon.FILE_ALL_ACCESS, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.Initialize()
    descriptor.SetSecurityDescriptorOwner(user, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    protected = win32security.SE_DACL_PROTECTED
    descriptor.SetSecurityDescriptorControl(protected, protected)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _open_path(
    path: Path,
    *,
    directory: bool,
    write: bool = False,
    security_write: bool = False,
) -> Any:
    access = ntsecuritycon.FILE_READ_ATTRIBUTES | ntsecuritycon.READ_CONTROL
    if write:
        access |= ntsecuritycon.GENERIC_READ | ntsecuritycon.GENERIC_WRITE
    if security_write:
        access |= ntsecuritycon.WRITE_DAC | ntsecuritycon.WRITE_OWNER
    flags = win32file.FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= win32con.FILE_FLAG_BACKUP_SEMANTICS
    return win32file.CreateFile(
        os.fspath(path),
        access,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
        None,
        win32con.OPEN_EXISTING,
        flags,
        None,
    )


def _information(handle: Any) -> WindowsFileIdentity:
    raw = win32file.GetFileInformationByHandle(handle)
    if isinstance(raw, dict):
        attributes = int(raw["FileAttributes"])
        volume_serial = int(raw["VolumeSerialNumber"])
        size = (int(raw["FileSizeHigh"]) << 32) | int(raw["FileSizeLow"])
        links = int(raw["NumberOfLinks"])
        file_index = (int(raw["FileIndexHigh"]) << 32) | int(raw["FileIndexLow"])
    else:
        attributes = int(raw[0])
        volume_serial = int(raw[4])
        size = (int(raw[5]) << 32) | int(raw[6])
        links = int(raw[7])
        file_index = (int(raw[8]) << 32) | int(raw[9])
    return WindowsFileIdentity(volume_serial, file_index, size, links, attributes)


def _validate_untrusted_aces(dacl: Any, trusted: set[str], prohibited: int) -> None:
    allowed_type = win32security.ACCESS_ALLOWED_ACE_TYPE
    denied_type = win32security.ACCESS_DENIED_ACE_TYPE
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        ace_type, ace_flags = (int(value) for value in ace[0])
        if ace_flags & win32con.INHERIT_ONLY_ACE or ace_type == denied_type:
            continue
        if ace_type != allowed_type:
            raise WindowsSecurityError("Windows DACL contains an unsupported ACE form")
        sid_text = win32security.ConvertSidToStringSid(ace[-1])
        mask = int(ace[1])
        if sid_text not in trusted and mask & prohibited:
            raise WindowsSecurityError(
                f"Windows DACL grants unsafe access to another principal (SID {sid_text}, mask {mask:#x})"
            )


def _apply_private_security(handle: Any) -> None:
    descriptor = _private_security_attributes().SECURITY_DESCRIPTOR
    win32security.SetSecurityInfo(
        handle,
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor.GetSecurityDescriptorOwner(),
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


def _validate_acl(
    handle: Any,
    *,
    private: bool,
    directory: bool,
    sensitive_parent: bool = False,
    require_protected: bool = True,
) -> None:
    security = win32security.GetSecurityInfo(
        handle,
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = security.GetSecurityDescriptorOwner()
    dacl = security.GetSecurityDescriptorDacl()
    if owner is None or dacl is None:
        raise WindowsSecurityError("Windows owner or DACL is unavailable")
    trusted = _trusted_sid_strings()
    owner_text = win32security.ConvertSidToStringSid(owner)
    current_text = win32security.ConvertSidToStringSid(_current_user_sid())
    if owner_text not in trusted or (private and owner_text != current_text):
        raise WindowsSecurityError("Windows object ownership is unsafe")
    control, _revision = security.GetSecurityDescriptorControl()
    if private and require_protected and not control & win32security.SE_DACL_PROTECTED:
        raise WindowsSecurityError("Windows private-object DACL must be protected")

    dangerous = (
        ntsecuritycon.FILE_WRITE_EA
        | ntsecuritycon.FILE_WRITE_ATTRIBUTES
        | ntsecuritycon.DELETE
        | ntsecuritycon.WRITE_DAC
        | ntsecuritycon.WRITE_OWNER
        | ntsecuritycon.GENERIC_WRITE
        | ntsecuritycon.GENERIC_ALL
    )
    if directory:
        dangerous |= ntsecuritycon.FILE_DELETE_CHILD
        if sensitive_parent:
            dangerous |= ntsecuritycon.FILE_ADD_FILE | ntsecuritycon.FILE_ADD_SUBDIRECTORY
    else:
        dangerous |= ntsecuritycon.FILE_WRITE_DATA | ntsecuritycon.FILE_APPEND_DATA
    # A private object has no legitimate allow ACE for an untrusted SID. Using
    # the complete mask also rejects raw generic bits before Windows maps them.
    private_rights = 0xFFFFFFFF
    if private:
        _validate_untrusted_aces(dacl, trusted, private_rights)
    elif not directory or sensitive_parent:
        _validate_untrusted_aces(dacl, trusted, dangerous)


def _validate_handle(
    handle: Any,
    *,
    directory: bool,
    private: bool,
    expected_size: int | None = None,
    sensitive_parent: bool = False,
    require_protected: bool = True,
) -> WindowsFileIdentity:
    identity = _information(handle)
    if identity.attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
        raise WindowsSecurityError("Windows reparse points are unsupported")
    is_directory = bool(identity.attributes & win32con.FILE_ATTRIBUTE_DIRECTORY)
    if is_directory is not directory:
        raise WindowsSecurityError("Windows object type is unsafe")
    if not directory and identity.links != 1:
        raise WindowsSecurityError("Windows private files must have exactly one hard link")
    if expected_size is not None and identity.size != expected_size:
        raise WindowsSecurityError("Windows file size verification failed")
    _validate_acl(
        handle,
        private=private,
        directory=directory,
        sensitive_parent=sensitive_parent,
        require_protected=require_protected,
    )
    return identity


def _directory_components(path: Path) -> tuple[Path, ...]:
    parts = path.parts
    if not parts:
        raise WindowsSecurityError("Windows path has no root")
    current = Path(parts[0])
    components: list[Path] = []
    for part in parts[1:]:
        current /= part
        components.append(current)
    return tuple(components)


def _validate_directory_component(
    handle: Any,
    component: Path,
    *,
    private: bool,
    allow_hardening: bool,
    sensitive_parent: bool,
) -> Any:
    try:
        if not private:
            _validate_handle(
                handle,
                directory=True,
                private=False,
                sensitive_parent=sensitive_parent,
            )
            return handle
        try:
            _validate_handle(handle, directory=True, private=True)
            return handle
        except WindowsSecurityError:
            if not allow_hardening:
                raise
        _close(handle)
        handle = _open_path(component, directory=True, security_write=True)
        _validate_handle(
            handle,
            directory=True,
            private=True,
            require_protected=False,
        )
        _apply_private_security(handle)
        _validate_handle(handle, directory=True, private=True)
        return handle
    except BaseException:
        _close(handle)
        raise


def _open_directory_chain(path: Path, *, create: bool, private_leaf: bool) -> list[Any]:
    normalized = _normalize(path)
    _validate_volume(normalized)
    handles: list[Any] = []
    try:
        components = _directory_components(normalized)
        for index, component in enumerate(components):
            try:
                handle = _open_path(component, directory=True)
            except Exception as exc:
                if not create and _missing_error(exc):
                    raise FileNotFoundError(os.fspath(component)) from exc
                if not create or not _missing_error(exc):
                    raise WindowsSecurityError("Windows directory chain could not be opened safely") from exc
                try:
                    win32file.CreateDirectory(os.fspath(component), _private_security_attributes())
                except Exception as create_exc:
                    if not _exists_error(create_exc):
                        raise WindowsSecurityError("Windows private directory could not be created") from create_exc
                handle = _open_path(component, directory=True)
            is_private_leaf = private_leaf and index == len(components) - 1
            handle = _validate_directory_component(
                handle,
                component,
                private=is_private_leaf,
                allow_hardening=create,
                sensitive_parent=index == len(components) - 1,
            )
            handles.append(handle)
        return handles
    except BaseException as exc:
        for handle in reversed(handles):
            _close(handle)
        if isinstance(exc, Exception) and not isinstance(exc, (FileNotFoundError, WindowsSecurityError)):
            raise WindowsSecurityError("Windows directory chain validation failed") from exc
        raise


@contextlib.contextmanager
def pinned_parent(path: Path, *, create: bool, private: bool) -> Iterator[Path]:
    normalized = _normalize(path)
    if normalized.parent == Path(normalized.anchor):
        raise WindowsSecurityError("Direct Windows volume-root storage is unsupported")
    handles = _open_directory_chain(normalized.parent, create=create, private_leaf=private)
    try:
        yield normalized
    finally:
        for handle in reversed(handles):
            _close(handle)


def ensure_private_parent(path: Path) -> None:
    with pinned_parent(path, create=True, private=True):
        return


def validate_private_directory(path: Path) -> WindowsFileIdentity:
    normalized = _normalize(path)
    handles = _open_directory_chain(normalized, create=False, private_leaf=True)
    if not handles:
        raise WindowsSecurityError("A volume root cannot be used as private storage")
    try:
        try:
            return _validate_handle(handles[-1], directory=True, private=True)
        except WindowsSecurityError:
            raise
        except Exception as exc:
            raise WindowsSecurityError("Windows private directory validation failed") from exc
    finally:
        for handle in reversed(handles):
            _close(handle)


def _open_validated_file(
    path: Path,
    *,
    private_parent: bool,
    private: bool = True,
    expected_size: int | None = None,
) -> tuple[Any, WindowsFileIdentity]:
    with pinned_parent(path, create=False, private=private_parent) as normalized:
        deadline = time.monotonic() + _OPEN_RETRY_SECONDS
        while True:
            try:
                handle = _open_path(normalized, directory=False)
                break
            except Exception as exc:
                if _missing_error(exc):
                    raise FileNotFoundError(os.fspath(path)) from exc
                if _error_code(exc) in {5, 32} and time.monotonic() < deadline:
                    time.sleep(_OPEN_RETRY_INTERVAL_SECONDS)
                    continue
                raise WindowsSecurityError("Windows private file could not be opened safely") from exc
        try:
            identity = _validate_handle(handle, directory=False, private=private, expected_size=expected_size)
        except BaseException as exc:
            _close(handle)
            if isinstance(exc, Exception) and not isinstance(exc, WindowsSecurityError):
                raise WindowsSecurityError("Windows private file validation failed") from exc
            raise
        return handle, identity


def validate_private_file(
    path: Path,
    *,
    expected_size: int | None = None,
    private_parent: bool = True,
) -> WindowsFileIdentity:
    handle, identity = _open_validated_file(path, private_parent=private_parent, expected_size=expected_size)
    _close(handle)
    return identity


def safe_regular_file_exists(
    path: Path,
    *,
    private: bool,
    private_parent: bool,
) -> bool:
    """Probe an ordinary local file only after validating its complete parent chain."""
    try:
        handle, _identity = _open_validated_file(
            path,
            private_parent=private_parent,
            private=private,
        )
    except FileNotFoundError:
        return False
    _close(handle)
    return True


def _validate_hardenable_sidecar(handle: Any) -> None:
    before = _information(handle)
    if before.attributes & (win32con.FILE_ATTRIBUTE_REPARSE_POINT | win32con.FILE_ATTRIBUTE_DIRECTORY):
        raise WindowsSecurityError("Windows sidecar type is unsafe")
    if before.links != 1:
        raise WindowsSecurityError("Windows sidecar has an unsafe hard-link count")


def harden_private_file(path: Path) -> WindowsFileIdentity:
    """Apply the private protected DACL to a newly SQLite-created sidecar."""
    with pinned_parent(path, create=False, private=True) as normalized:
        deadline = time.monotonic() + _OPEN_RETRY_SECONDS
        while True:
            try:
                handle = win32file.CreateFile(
                    os.fspath(normalized),
                    ntsecuritycon.FILE_READ_ATTRIBUTES
                    | ntsecuritycon.READ_CONTROL
                    | ntsecuritycon.WRITE_DAC
                    | ntsecuritycon.WRITE_OWNER,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                    None,
                    win32con.OPEN_EXISTING,
                    win32con.FILE_ATTRIBUTE_NORMAL | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
                break
            except Exception as exc:
                if _missing_error(exc):
                    raise FileNotFoundError(os.fspath(path)) from exc
                if _error_code(exc) in {5, 32} and time.monotonic() < deadline:
                    time.sleep(_OPEN_RETRY_INTERVAL_SECONDS)
                    continue
                raise WindowsSecurityError("Windows sidecar could not be opened for DACL hardening") from exc
        try:
            _validate_hardenable_sidecar(handle)
            _apply_private_security(handle)
            return _validate_handle(handle, directory=False, private=True)
        except WindowsSecurityError:
            raise
        except Exception as exc:
            raise WindowsSecurityError("Windows sidecar DACL hardening failed") from exc
        finally:
            _close(handle)


def _create_file(path: Path) -> Any:
    return win32file.CreateFile(
        os.fspath(path),
        ntsecuritycon.GENERIC_READ | ntsecuritycon.GENERIC_WRITE | ntsecuritycon.READ_CONTROL,
        0,
        _private_security_attributes(),
        win32con.CREATE_NEW,
        win32con.FILE_ATTRIBUTE_NORMAL | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )


def create_private_file(path: Path) -> WindowsFileIdentity:
    with pinned_parent(path, create=True, private=True) as normalized:
        try:
            handle = _create_file(normalized)
        except Exception as exc:
            if _exists_error(exc):
                raise FileExistsError(os.fspath(path)) from exc
            raise WindowsSecurityError("Windows private file could not be created safely") from exc
        try:
            return _validate_handle(handle, directory=False, private=True, expected_size=0)
        finally:
            _close(handle)


def _write_all(handle: Any, content: bytes) -> None:
    offset = 0
    view = memoryview(content)
    while offset < len(view):
        chunk = bytes(view[offset : offset + 16 * 1024 * 1024])
        result = win32file.WriteFile(handle, chunk)
        written = result[1] if isinstance(result, tuple) and len(result) > 1 else len(chunk)
        if not isinstance(written, int) or written <= 0:
            raise WindowsSecurityError("Windows file write did not make progress")
        offset += written


def write_private_new(path: Path, content: bytes, *, private_parent: bool = True) -> WindowsFileIdentity:
    with pinned_parent(path, create=True, private=private_parent) as normalized:
        try:
            handle = _create_file(normalized)
        except Exception as exc:
            if _exists_error(exc):
                raise FileExistsError(os.fspath(path)) from exc
            raise WindowsSecurityError("Windows private file could not be created safely") from exc
        try:
            _validate_handle(handle, directory=False, private=True, expected_size=0)
            _write_all(handle, content)
            win32file.FlushFileBuffers(handle)
            return _validate_handle(handle, directory=False, private=True, expected_size=len(content))
        except BaseException as exc:
            _close(handle)
            with contextlib.suppress(Exception):
                win32file.DeleteFile(os.fspath(normalized))
            if isinstance(exc, Exception) and not isinstance(exc, WindowsSecurityError):
                raise WindowsSecurityError("Windows private-file write failed") from exc
            raise
        finally:
            _close(handle)


def _delete_if_identity(path: Path, identity: WindowsFileIdentity, *, private_parent: bool) -> bool:
    try:
        current = validate_private_file(path, private_parent=private_parent)
    except (FileNotFoundError, WindowsSecurityError, OSError):
        return False
    if current.key != identity.key:
        return False
    try:
        win32file.DeleteFile(os.fspath(path))
    except Exception:
        return False
    return True


def _validate_optional_target(path: Path, *, private_parent: bool) -> WindowsFileIdentity | None:
    try:
        return validate_private_file(path, private_parent=private_parent)
    except Exception as exc:
        if _missing_error(exc) or isinstance(exc, FileNotFoundError):
            return None
        raise


def _move_replace(source: Path, destination: Path) -> None:
    flags = win32file.MOVEFILE_REPLACE_EXISTING | win32file.MOVEFILE_WRITE_THROUGH
    deadline = time.monotonic() + _MOVE_RETRY_SECONDS
    while True:
        try:
            win32file.MoveFileEx(os.fspath(source), os.fspath(destination), flags)
            return
        except Exception as exc:
            if _error_code(exc) != 32 or time.monotonic() >= deadline:
                raise WindowsSecurityError("Windows write-through replacement failed") from exc
            time.sleep(_MOVE_RETRY_INTERVAL_SECONDS)


def atomic_write_private(
    path: Path,
    content: bytes,
    *,
    prefix: str = _BOOTSTRAP_TEMP_PREFIX,
    private_parent: bool = True,
) -> WindowsFileIdentity:
    with pinned_parent(path, create=True, private=private_parent) as normalized:
        _validate_optional_target(normalized, private_parent=private_parent)
        cleanup_stale_files(normalized.parent, prefix=prefix, private_parent=private_parent)
        temporary = normalized.with_name(f"{prefix}{secrets.token_hex(16)}.tmp")
        try:
            handle = _create_file(temporary)
        except Exception as exc:
            raise WindowsSecurityError("Windows temporary file could not be created safely") from exc
        temporary_identity: WindowsFileIdentity | None = None
        try:
            temporary_identity = _validate_handle(handle, directory=False, private=True, expected_size=0)
            _write_all(handle, content)
            win32file.FlushFileBuffers(handle)
            temporary_identity = _validate_handle(handle, directory=False, private=True, expected_size=len(content))
        except BaseException as exc:
            _close(handle)
            if temporary_identity is not None:
                _delete_if_identity(temporary, temporary_identity, private_parent=private_parent)
            if isinstance(exc, Exception) and not isinstance(exc, WindowsSecurityError):
                raise WindowsSecurityError("Windows temporary-file write failed") from exc
            raise
        else:
            _close(handle)
        try:
            _validate_optional_target(normalized, private_parent=private_parent)
            _move_replace(temporary, normalized)
            final = validate_private_file(
                normalized,
                expected_size=len(content),
                private_parent=private_parent,
            )
            if temporary_identity is None or final.key != temporary_identity.key:
                raise WindowsSecurityError("Windows replacement identity verification failed")
            temporary_identity = None
            return final
        finally:
            if temporary_identity is not None:
                _delete_if_identity(temporary, temporary_identity, private_parent=private_parent)


def preflight_artifact_destination(path: Path) -> Path:
    with pinned_parent(path, create=True, private=False) as normalized:
        _validate_optional_target(normalized, private_parent=False)
        cleanup_stale_files(normalized.parent, prefix=_ATTACHMENT_TEMP_PREFIX, private_parent=False)
        return normalized


def write_attachment(path: Path, content: bytes) -> WindowsFileIdentity:
    return atomic_write_private(
        path,
        content,
        prefix=_ATTACHMENT_TEMP_PREFIX,
        private_parent=False,
    )


def _find_matching_names(parent: Path, *, wildcard: str, pattern: re.Pattern[str]) -> list[str]:
    names: list[str] = []
    try:
        records = win32file.FindFilesIterator(os.fspath(parent / wildcard))
        for examined, record in enumerate(records, start=1):
            name = str(record[8])
            if pattern.fullmatch(name):
                names.append(name)
            if len(names) >= _MAX_STALE_CANDIDATES or examined >= _MAX_STALE_ENTRIES_EXAMINED:
                break
    except Exception as exc:
        if _missing_error(exc):
            return []
        raise WindowsSecurityError("Windows cleanup candidates could not be enumerated safely") from exc
    return names


def cleanup_stale_files(
    parent: Path,
    *,
    prefix: str,
    private_parent: bool,
    minimum_age_seconds: float = _STALE_AGE_SECONDS,
) -> int:
    normalized = _normalize(parent)
    cutoff = time.time() - minimum_age_seconds
    removed = 0
    pattern = _TEMP_FILE_PATTERNS.get(prefix)
    if pattern is None:
        raise ValueError("Unknown Windows temporary-file prefix")
    try:
        with pinned_parent(normalized / "placeholder", create=False, private=private_parent):
            names = _find_matching_names(normalized, wildcard=f"{prefix}*.tmp", pattern=pattern)
    except (FileNotFoundError, OSError, WindowsSecurityError):
        return 0
    for name in names:
        path = normalized / name
        try:
            if path.stat(follow_symlinks=False).st_mtime > cutoff:
                continue
            identity = validate_private_file(path, private_parent=private_parent)
        except (OSError, WindowsSecurityError):
            continue
        if _delete_if_identity(path, identity, private_parent=private_parent):
            removed += 1
    return removed


def create_private_temp_directory() -> tuple[Path, WindowsFileIdentity]:
    temporary_parent = _normalize(Path(tempfile.gettempdir()))
    container = temporary_parent / _RESULT_CONTAINER_NAME
    ensure_private_parent(container / "placeholder")
    with pinned_parent(container / "placeholder", create=False, private=True):
        cleanup_stale_result_roots()
        for _attempt in range(16):
            root = container / f"{_RESULT_ROOT_PREFIX}{secrets.token_hex(12)}"
            try:
                win32file.CreateDirectory(os.fspath(root), _private_security_attributes())
            except Exception as exc:
                if _exists_error(exc):
                    continue
                raise WindowsSecurityError("Windows result directory could not be created") from exc
            return root, validate_private_directory(root)
    raise WindowsSecurityError("A unique Windows result directory could not be allocated")


def remove_private_file(path: Path, identity: WindowsFileIdentity) -> bool:
    return _delete_if_identity(path, identity, private_parent=True)


def remove_private_directory(path: Path, identity: WindowsFileIdentity) -> bool:
    try:
        current = validate_private_directory(path)
    except (OSError, WindowsSecurityError):
        return False
    if current.key != identity.key:
        return False
    try:
        win32file.RemoveDirectory(os.fspath(path))
    except Exception:
        return False
    return True


def _bounded_entries(path: Path, limit: int) -> list[os.DirEntry[str]]:
    with os.scandir(path) as iterator:
        return [entry for _index, entry in zip(range(limit), iterator, strict=False)]


def _cleanup_result_root(root: Path) -> bool:
    lock_path = root / _RESULT_LOCK_NAME
    try:
        # Never create a marker while inspecting an unverified candidate root.
        expected_lock = validate_private_file(lock_path)
        # A live writer retains this LockFileEx range. Age alone must never make
        # an active long-running process eligible for cleanup.
        with secure_file_lock(lock_path, timeout=0):
            root_identity = validate_private_directory(root)
            lock_identity = validate_private_file(lock_path)
            if lock_identity.key != expected_lock.key:
                return False
            children = _bounded_entries(root, _MAX_STALE_CANDIDATES + 2)
            result_children = [child for child in children if child.name != _RESULT_LOCK_NAME]
            if len(children) > _MAX_STALE_CANDIDATES + 1:
                return False
            file_identities: list[tuple[Path, WindowsFileIdentity]] = []
            for child in result_children:
                if not _RESULT_FILE.fullmatch(child.name):
                    return False
                child_path = root / child.name
                file_identities.append((child_path, validate_private_file(child_path)))
    except (OSError, WindowsSecurityError):
        return False
    if not all(remove_private_file(child, identity) for child, identity in file_identities):
        return False
    if not remove_private_file(lock_path, lock_identity):
        return False
    return remove_private_directory(root, root_identity)


def cleanup_stale_result_roots(*, minimum_age_seconds: float = _STALE_AGE_SECONDS) -> int:
    parent = _normalize(Path(tempfile.gettempdir())) / _RESULT_CONTAINER_NAME
    try:
        validate_private_directory(parent)
    except (FileNotFoundError, OSError, WindowsSecurityError):
        return 0
    cutoff = time.time() - minimum_age_seconds
    try:
        names = _find_matching_names(parent, wildcard=f"{_RESULT_ROOT_PREFIX}*", pattern=_RESULT_ROOT)
    except WindowsSecurityError:
        return 0
    removed = 0
    for name in names:
        candidate = parent / name
        try:
            stale = candidate.stat(follow_symlinks=False).st_mtime <= cutoff
        except OSError:
            continue
        if stale and _cleanup_result_root(candidate):
            removed += 1
    return removed


@contextlib.contextmanager
def secure_file_lock(path: Path, *, timeout: float) -> Iterator[None]:
    normalized = _normalize(path)
    ensure_private_parent(normalized)
    try:
        existing = validate_private_file(normalized)
    except FileNotFoundError:
        try:
            create_private_file(normalized)
        except FileExistsError:
            existing = validate_private_file(normalized)
        else:
            existing = validate_private_file(normalized)
    if existing.links != 1:
        raise WindowsSecurityError("Windows lock identity is unsafe")
    lock = FileLock(
        os.fspath(normalized),
        timeout=timeout,
        mode=0o600,
        thread_local=False,
        fallback_to_soft=False,
        preserve_lock_file=True,
    )
    try:
        lock.acquire()
    except Timeout as exc:
        raise WindowsSecurityError("Windows application lock is busy") from exc
    except Exception as exc:
        raise WindowsSecurityError("Windows application lock could not be acquired safely") from exc
    try:
        validate_private_file(normalized)
        yield
    finally:
        lock.release()
