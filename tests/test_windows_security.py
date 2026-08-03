from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import mcp_email_server.managed as managed_module
import mcp_email_server.windows_security as windows_security_module
from mcp_email_server.bootstrap import BootstrapError, read_bootstrap, write_bootstrap
from mcp_email_server.large_results import LocalLargeResultWriter
from mcp_email_server.managed import (
    ManagedCatalog,
    ManagedCatalogError,
    ManagedCatalogSecurityError,
    ManagedSqliteSecretStore,
)
from mcp_email_server.windows_security import (
    _ATTACHMENT_TEMP_PREFIX,
    WindowsSecurityError,
    atomic_write_private,
    cleanup_stale_files,
    cleanup_stale_result_roots,
    ensure_private_parent,
    preflight_artifact_destination,
    secure_file_lock,
    validate_private_directory,
    validate_private_file,
    windows_security_supported,
    write_attachment,
    write_private_new,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows NTFS security contract")

if os.name == "nt":  # pragma: win32 cover
    import ntsecuritycon
    import win32security
else:
    ntsecuritycon: Any = None
    win32security: Any = None

_GENERIC_ACE_RIGHTS = [ntsecuritycon.GENERIC_READ, ntsecuritycon.GENERIC_EXECUTE] if os.name == "nt" else []


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    ensure_private_parent(root / "placeholder")
    validate_private_directory(root)
    return root


def _symlink_unavailable(exc: OSError) -> None:
    if os.getenv("MCP_EMAIL_SERVER_REQUIRE_WINDOWS_SYMLINK_TESTS") == "1":
        pytest.fail(f"native Windows CI must support the symlink security proof: {exc}")
    pytest.skip(f"file symlink privilege unavailable: {exc}")


def _wait_for(path: Path, process: subprocess.Popen[str], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"subprocess exited before checkpoint: stdout={stdout!r}, stderr={stderr!r}")
        time.sleep(0.02)
    process.kill()
    pytest.fail("subprocess did not reach checkpoint")


def _python_process(script: str, *arguments: Path | str) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - fixed current interpreter and test-owned script
        [sys.executable, "-c", script, *(os.fspath(argument) for argument in arguments)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_windows_managed_bootstrap_catalog_and_private_sqlite_secret_round_trip(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    catalog = ManagedCatalog.initialize(root / "catalog.sqlite3")
    config = root / "config.toml"
    selected = write_bootstrap(mode="managed", db_path=catalog.path, path=config)

    assert selected.mode == "managed"
    assert read_bootstrap(config).db_path == catalog.path
    assert catalog.catalog_revision() == 1

    assert isinstance(catalog.secret_store, ManagedSqliteSecretStore)
    locator = f"windows-native-{time.time_ns()}"
    sentinel = f"windows-credential-{time.time_ns()}"
    catalog.secret_store.put(locator, sentinel)
    assert catalog.secret_store.get(locator) == sentinel
    assert catalog.secret_store.delete(locator)


def test_windows_wal_sidecars_are_rehardened_after_last_connection_recreates_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private_root(tmp_path)
    catalog = ManagedCatalog.initialize(root / "catalog.sqlite3")
    raw = sqlite3.connect(catalog.path)
    raw.execute("PRAGMA journal_mode = WAL").fetchone()
    raw.execute("UPDATE catalog SET revision = revision WHERE id = 'local'")
    raw.commit()
    sidecars = [Path(f"{catalog.path}{suffix}") for suffix in ("-wal", "-shm")]
    assert all(sidecar.exists() for sidecar in sidecars)
    for sidecar in sidecars:
        windows_security_module.harden_private_file(sidecar)

    original_validate = managed_module._validate_sidecars
    validation_count = 0
    raw_closed = False

    def close_after_second_validation(path: Path) -> dict[str, tuple[int, int]]:
        nonlocal validation_count, raw_closed
        result = original_validate(path)
        validation_count += 1
        if validation_count == 2:
            raw.close()
            raw_closed = True
        return result

    monkeypatch.setattr(managed_module, "_validate_sidecars", close_after_second_validation)
    try:
        with managed_module._connect(catalog.path) as connection:
            assert connection.execute("SELECT revision FROM catalog").fetchone()[0] == 1
    finally:
        if not raw_closed:
            raw.close()
    assert validation_count >= 2


def test_windows_post_close_sidecar_hardening_remains_under_application_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private_root(tmp_path)
    catalog = ManagedCatalog.initialize(root / "catalog.sqlite3")
    original_lock = managed_module._application_path_lock
    original_harden = managed_module._harden_windows_sidecars
    lock_depth = 0
    harden_calls = 0

    @contextlib.contextmanager
    def tracked_lock(path: Path):
        nonlocal lock_depth
        with original_lock(path):
            lock_depth += 1
            try:
                yield
            finally:
                lock_depth -= 1

    def checked_harden(path: Path) -> None:
        nonlocal harden_calls
        assert lock_depth > 0
        harden_calls += 1
        original_harden(path)

    monkeypatch.setattr(managed_module, "_application_path_lock", tracked_lock)
    monkeypatch.setattr(managed_module, "_harden_windows_sidecars", checked_harden)
    with managed_module._connect(catalog.path) as connection:
        assert connection.execute("SELECT revision FROM catalog").fetchone()[0] == 1
    assert harden_calls >= 2


def test_windows_post_commit_reconciliation_contention_does_not_report_mutation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private_root(tmp_path)
    catalog = ManagedCatalog.initialize(root / "catalog.sqlite3")
    original_lock = managed_module._application_path_lock
    lock_calls = 0

    @contextlib.contextmanager
    def busy_on_reconciliation(path: Path):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            raise ManagedCatalogError("Managed catalog lock is busy")
        with original_lock(path):
            yield

    monkeypatch.setattr(managed_module, "_application_path_lock", busy_on_reconciliation)
    with managed_module._connect(catalog.path) as connection:
        connection.execute("UPDATE catalog SET revision = 2 WHERE id = 'local'")
        connection.commit()

    with sqlite3.connect(catalog.path) as verification:
        assert verification.execute("SELECT revision FROM catalog WHERE id = 'local'").fetchone()[0] == 2
    assert lock_calls == 2


def test_windows_backend_uses_real_local_ntfs(tmp_path: Path) -> None:
    assert windows_security_supported()
    root = _private_root(tmp_path)
    identity = validate_private_directory(root)

    assert identity.volume_serial > 0
    assert identity.file_index > 0


def test_windows_private_creation_rejects_file_and_directory_reparse_points(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    target = root / "target.txt"
    atomic_write_private(target, b"preserve")
    file_link = root / "file-link.txt"
    try:
        file_link.symlink_to(target)
    except OSError as exc:
        _symlink_unavailable(exc)

    with pytest.raises(WindowsSecurityError, match="reparse"):
        validate_private_file(file_link)
    assert target.read_bytes() == b"preserve"

    real_directory = root / "real-directory"
    ensure_private_parent(real_directory / "placeholder")
    directory_link = root / "directory-link"
    try:
        directory_link.symlink_to(real_directory, target_is_directory=True)
    except OSError as exc:
        _symlink_unavailable(exc)
    with pytest.raises(WindowsSecurityError, match="reparse"):
        validate_private_directory(directory_link)


def test_windows_validation_does_not_repair_an_unprotected_directory_dacl(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    candidate = root / "unprotected"
    candidate.mkdir()
    before = win32security.GetNamedSecurityInfo(
        os.fspath(candidate),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    ).GetSecurityDescriptorControl()[0]
    assert not before & win32security.SE_DACL_PROTECTED

    with pytest.raises(WindowsSecurityError, match="protected"):
        validate_private_directory(candidate)

    after = win32security.GetNamedSecurityInfo(
        os.fspath(candidate),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    ).GetSecurityDescriptorControl()[0]
    assert not after & win32security.SE_DACL_PROTECTED


def test_windows_junction_is_rejected_without_developer_mode(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    target = root / "junction-target"
    ensure_private_parent(target / "placeholder")
    junction = root / "junction"
    command = Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe"
    result = subprocess.run(  # noqa: S603 - fixed Windows built-in with test-owned paths
        [os.fspath(command), "/c", "mklink", "/J", os.fspath(junction), os.fspath(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"junction creation failed: {result.stdout} {result.stderr}")

    with pytest.raises(WindowsSecurityError, match="reparse"):
        validate_private_directory(junction)


def test_windows_hard_link_and_permissive_dacl_are_rejected(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    target = root / "target.txt"
    atomic_write_private(target, b"preserve")
    hardlink = root / "hardlink.txt"
    os.link(target, hardlink)
    with pytest.raises(WindowsSecurityError, match="hard link"):
        validate_private_file(target)
    hardlink.unlink()

    descriptor = win32security.GetNamedSecurityInfo(
        os.fspath(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        0,
        ntsecuritycon.FILE_GENERIC_READ,
        everyone,
    )
    win32security.SetNamedSecurityInfo(
        os.fspath(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    with pytest.raises(WindowsSecurityError, match="another principal"):
        validate_private_file(target)


def test_windows_pinned_shared_ancestor_can_contain_a_private_managed_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    descriptor = win32security.GetNamedSecurityInfo(
        os.fspath(shared),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    rights = ntsecuritycon.FILE_ADD_SUBDIRECTORY | ntsecuritycon.FILE_DELETE_CHILD | ntsecuritycon.GENERIC_WRITE
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, 0, rights, everyone)
    win32security.SetNamedSecurityInfo(
        os.fspath(shared),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )

    target = shared / "private" / "catalog.sqlite3"
    ensure_private_parent(target)
    atomic_write_private(target, b"private")

    validate_private_directory(target.parent)
    assert validate_private_file(target).size == len(b"private")


@pytest.mark.parametrize("rights", _GENERIC_ACE_RIGHTS)
def test_windows_raw_generic_ace_masks_are_rejected(tmp_path: Path, rights: int) -> None:
    root = _private_root(tmp_path)
    target = root / f"generic-{rights}.txt"
    atomic_write_private(target, b"private")
    descriptor = win32security.GetNamedSecurityInfo(
        os.fspath(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, 0, rights, everyone)
    win32security.SetNamedSecurityInfo(
        os.fspath(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )

    with pytest.raises(WindowsSecurityError, match="another principal"):
        validate_private_file(target)


def test_windows_foreign_owner_is_rejected(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    target = root / "owner.txt"
    atomic_write_private(target, b"owner")
    administrators = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
    try:
        win32security.SetNamedSecurityInfo(
            os.fspath(target),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION,
            administrators,
            None,
            None,
            None,
        )
    except OSError as exc:
        pytest.skip(f"runner cannot assign an alternate token owner: {exc}")
    with pytest.raises(WindowsSecurityError, match="ownership"):
        validate_private_file(target)


def test_windows_lock_contends_and_releases_after_process_termination(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    lock_path = root / "catalog.lock"
    checkpoint = root / "locked.marker"
    script = """
import sys, time
from pathlib import Path
from mcp_email_server.windows_security import secure_file_lock
with secure_file_lock(Path(sys.argv[1]), timeout=5):
    Path(sys.argv[2]).write_text('locked')
    time.sleep(60)
"""
    process = _python_process(script, lock_path, checkpoint)
    try:
        _wait_for(checkpoint, process)
        with pytest.raises(WindowsSecurityError, match="busy"):
            with secure_file_lock(lock_path, timeout=0.2):
                pytest.fail("contended lock must not be acquired")
    finally:
        process.kill()
        process.wait(timeout=10)

    with secure_file_lock(lock_path, timeout=2):
        assert validate_private_file(lock_path).links == 1


def test_windows_lock_can_be_released_from_a_different_worker_thread(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    lock_path = root / "cross-thread.lock"
    lock = secure_file_lock(lock_path, timeout=1)
    errors: list[BaseException] = []

    def enter() -> None:
        try:
            lock.__enter__()
        except BaseException as exc:
            errors.append(exc)

    def leave() -> None:
        try:
            lock.__exit__(None, None, None)
        except BaseException as exc:
            errors.append(exc)

    enter_thread = threading.Thread(target=enter)
    enter_thread.start()
    enter_thread.join(timeout=5)
    assert not enter_thread.is_alive()
    assert not errors
    with pytest.raises(WindowsSecurityError, match="busy"):
        with secure_file_lock(lock_path, timeout=0):
            pytest.fail("the retained owner lock must still be held")

    leave_thread = threading.Thread(target=leave)
    leave_thread.start()
    leave_thread.join(timeout=5)
    assert not leave_thread.is_alive()
    assert not errors
    with secure_file_lock(lock_path, timeout=1):
        pass


def test_windows_concurrent_replacement_is_complete_and_identity_checked(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    target = root / "concurrent.bin"
    payloads = [bytes([index]) * (128 * 1024) for index in range(1, 9)]
    atomic_write_private(target, payloads[0])
    errors: list[BaseException] = []

    def replace(payload: bytes) -> None:
        try:
            write_attachment(target, payload)
        except WindowsSecurityError as exc:
            # A writer may lose final-identity verification to a later complete
            # replacement; it must fail rather than claim its bytes won.
            errors.append(exc)

    threads = [threading.Thread(target=replace, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert target.read_bytes() in payloads
    assert validate_private_file(target).size == len(payloads[0])
    assert all("identity" in str(error).lower() or "opened" in str(error).lower() for error in errors)


def _run_crash_replace(target: Path, checkpoint: Path, stage: str, payload: bytes) -> subprocess.Popen[str]:
    script = """
import sys, time
from pathlib import Path
import mcp_email_server.windows_security as ws
path, marker, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
payload = bytes.fromhex(sys.argv[4])
original = ws.win32file.MoveFileEx
def controlled(source, destination, flags):
    if stage == 'pre':
        marker.write_text('ready')
        time.sleep(60)
    result = original(source, destination, flags)
    if stage == 'post':
        marker.write_text('ready')
        time.sleep(60)
    return result
ws.win32file.MoveFileEx = controlled
ws.atomic_write_private(path, payload)
"""
    return _python_process(script, target, checkpoint, stage, payload.hex())


@pytest.mark.parametrize("stage", ["pre", "post"])
def test_windows_crash_boundary_preserves_complete_old_or_new_and_cleans_stale_temp(
    tmp_path: Path,
    stage: str,
) -> None:
    root = _private_root(tmp_path)
    target = root / "crash.bin"
    old = b"old-content" * 4096
    new = b"new-content" * 4096
    atomic_write_private(target, old)
    checkpoint = root / f"{stage}.marker"
    process = _run_crash_replace(target, checkpoint, stage, new)
    try:
        _wait_for(checkpoint, process)
    finally:
        process.kill()
        process.wait(timeout=10)

    assert target.read_bytes() == (old if stage == "pre" else new)
    assert validate_private_file(target).size == len(old if stage == "pre" else new)
    if stage == "pre":
        remnants = list(root.glob(".mcp-email-bootstrap-*.tmp"))
        assert remnants
        removed = cleanup_stale_files(
            root,
            prefix=".mcp-email-bootstrap-",
            private_parent=True,
            minimum_age_seconds=-1,
        )
        assert removed == len(remnants)
        assert not list(root.glob(".mcp-email-bootstrap-*.tmp"))


@pytest.mark.parametrize(
    ("api_name", "expected_message"),
    [
        ("WriteFile", "temporary-file write failed"),
        ("FlushFileBuffers", "temporary-file write failed"),
        ("MoveFileEx", "write-through replacement failed"),
    ],
)
def test_windows_write_failures_are_typed_and_remove_partial_temporaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_name: str,
    expected_message: str,
) -> None:
    root = _private_root(tmp_path)
    target = root / "failure.bin"
    atomic_write_private(target, b"old")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, "injected Win32 failure")

    monkeypatch.setattr(windows_security_module.win32file, api_name, fail)
    with pytest.raises(WindowsSecurityError, match=expected_message):
        atomic_write_private(target, b"new")

    assert target.read_bytes() == b"old"
    assert not list(root.glob(".mcp-email-bootstrap-*.tmp"))


def test_windows_atomic_observers_never_see_partial_content(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    target = root / "atomic.bin"
    old = b"A" * (256 * 1024)
    new = b"B" * (256 * 1024)
    atomic_write_private(target, old)
    stop = threading.Event()
    observed: list[bytes] = []

    def reader() -> None:
        while not stop.is_set():
            observed.append(target.read_bytes())

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(10):
            write_attachment(target, new)
            write_attachment(target, old)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert observed
    assert set(observed) <= {old, new}


def test_windows_cleanup_candidate_enumeration_has_a_hard_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    examined = 0

    def records(_pattern: str):
        nonlocal examined
        while True:
            examined += 1
            yield (0, None, None, None, 0, 0, 0, 0, f"invalid-{examined}.tmp", "")

    monkeypatch.setattr(windows_security_module.win32file, "FindFilesIterator", records)
    names = windows_security_module._find_matching_names(
        tmp_path,
        wildcard=f"{_ATTACHMENT_TEMP_PREFIX}*.tmp",
        pattern=windows_security_module._TEMP_FILE_PATTERNS[_ATTACHMENT_TEMP_PREFIX],
    )
    assert names == []
    assert examined == windows_security_module._MAX_STALE_ENTRIES_EXAMINED


def test_windows_stale_cleanup_does_not_follow_substituted_reparse_entry(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    outside = root / "outside.txt"
    atomic_write_private(outside, b"preserve")
    remnant = root / f"{_ATTACHMENT_TEMP_PREFIX}{'a' * 32}.tmp"
    try:
        remnant.symlink_to(outside)
    except OSError as exc:
        _symlink_unavailable(exc)

    assert (
        cleanup_stale_files(
            root,
            prefix=_ATTACHMENT_TEMP_PREFIX,
            private_parent=False,
            minimum_age_seconds=-1,
        )
        == 0
    )
    assert outside.read_bytes() == b"preserve"
    assert remnant.is_symlink()


def test_windows_stale_cleanup_requires_exact_provenance_and_scans_past_unrelated_entries(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    for index in range(80):
        (root / f"unrelated-{index:03d}.txt").write_text("unrelated", encoding="utf-8")
    unrelated = root / f"{_ATTACHMENT_TEMP_PREFIX}not-ours.tmp"
    write_private_new(unrelated, b"preserve")
    remnant = root / f"{_ATTACHMENT_TEMP_PREFIX}{'b' * 32}.tmp"
    write_private_new(remnant, b"remove")

    assert (
        cleanup_stale_files(
            root,
            prefix=_ATTACHMENT_TEMP_PREFIX,
            private_parent=True,
            minimum_age_seconds=-1,
        )
        == 1
    )
    assert unrelated.read_bytes() == b"preserve"
    assert not remnant.exists()


def test_windows_spill_cleanup_scans_past_unrelated_temp_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_parent = _private_root(tmp_path)
    monkeypatch.setattr(windows_security_module.tempfile, "gettempdir", lambda: os.fspath(temporary_parent))
    container = temporary_parent / windows_security_module._RESULT_CONTAINER_NAME
    ensure_private_parent(container / "placeholder")
    for index in range(80):
        (container / f"unrelated-{index:03d}").mkdir()
    unrelated_root = container / "result-not-ours"
    unrelated_root.mkdir()
    spill_root, _identity = windows_security_module.create_private_temp_directory()
    write_private_new(spill_root / ".owner.lock", b"")
    write_private_new(spill_root / f"email-content-{'c' * 16}.json", b"private")

    assert cleanup_stale_result_roots(minimum_age_seconds=-1) == 1
    assert not spill_root.exists()
    assert unrelated_root.exists()


def test_windows_killed_spill_root_is_removed_by_bounded_validated_cleanup(tmp_path: Path) -> None:
    _private_root(tmp_path)
    script = """
import asyncio, os
from mcp_email_server.large_results import LocalLargeResultWriter
writer = LocalLargeResultWriter()
reference = asyncio.run(writer.write(prefix='email-content', content=b'private'))
print(reference.output_file_path, flush=True)
os._exit(0)
"""
    process = _python_process(script)
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stderr
    artifact = Path(stdout.strip())
    root = artifact.parent
    assert artifact.exists()

    assert cleanup_stale_result_roots(minimum_age_seconds=-1) >= 1
    assert not artifact.exists()
    assert not root.exists()


@pytest.mark.asyncio
async def test_windows_large_result_writer_round_trip(tmp_path: Path) -> None:
    _private_root(tmp_path)
    writer = LocalLargeResultWriter()
    reference = await writer.write(prefix="email-content", content=b"private")
    artifact = Path(reference.output_file_path)
    assert artifact.read_bytes() == b"private"
    assert validate_private_file(artifact).size == len(b"private")
    cleanup_stale_result_roots(minimum_age_seconds=-1)
    assert artifact.exists(), "a live owner lock must defeat age-based cleanup"
    await writer.aclose()
    assert not artifact.exists()


def test_windows_managed_and_bootstrap_probes_reject_network_paths_and_reparse_parents(tmp_path: Path) -> None:
    unc_catalog = Path(r"\\127.0.0.1\unreachable-share\catalog.sqlite3")
    with pytest.raises(ManagedCatalogSecurityError):
        ManagedCatalog(unc_catalog).catalog_revision()
    with pytest.raises(BootstrapError):
        read_bootstrap(Path(r"\\127.0.0.1\unreachable-share\config.toml"))

    root = _private_root(tmp_path)
    network_parent = root / "network-parent"
    try:
        network_parent.symlink_to(Path(r"\\127.0.0.1\unreachable-share"), target_is_directory=True)
    except OSError as exc:
        _symlink_unavailable(exc)
    with pytest.raises(ManagedCatalogSecurityError):
        ManagedCatalog(network_parent / "catalog.sqlite3").catalog_revision()


def test_windows_nonfixed_drive_is_rejected_before_volume_metadata_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        windows_security_module.win32file,
        "GetDriveType",
        lambda _root: windows_security_module.win32con.DRIVE_REMOTE,
    )

    def unexpected_volume_probe(_root: str) -> object:
        pytest.fail("a non-fixed drive must be rejected before volume metadata access")

    monkeypatch.setattr(windows_security_module.win32api, "GetVolumeInformation", unexpected_volume_probe)
    with pytest.raises(WindowsSecurityError, match="local fixed"):
        preflight_artifact_destination(tmp_path / "remote.bin")


def test_windows_direct_volume_root_storage_is_rejected(tmp_path: Path) -> None:
    target = Path(tmp_path.anchor) / f"mcp-email-root-{time.time_ns()}.bin"
    with pytest.raises(WindowsSecurityError, match="volume-root"):
        preflight_artifact_destination(target)
    assert not target.exists()


def test_windows_unsupported_namespaces_and_streams_fail_before_creation(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    candidates = [
        Path(r"\\server\share\attachment.bin"),
        Path(r"\\.\C:\attachment.bin"),
        root / "attachment.bin:stream",
    ]
    for candidate in candidates:
        with pytest.raises(WindowsSecurityError):
            preflight_artifact_destination(candidate)
    assert list(root.iterdir()) == []
