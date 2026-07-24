from __future__ import annotations

import os
import stat
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from mcp_email_server.bootstrap import (
    BOOTSTRAP_VERSION,
    BootstrapError,
    BootstrapRevisionError,
    ManagedModeWriteError,
    assert_legacy_writable,
    read_bootstrap,
    write_bootstrap,
)


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def test_missing_bootstrap_defaults_to_legacy_without_writing(tmp_path):
    path = tmp_path / "missing" / "config.toml"

    bootstrap = read_bootstrap(path)

    assert bootstrap.mode == "legacy"
    assert bootstrap.exists is False
    assert not path.parent.exists()


def test_pre_managed_toml_defaults_to_legacy(tmp_path):
    path = tmp_path / "config.toml"
    content = 'credential_storage = "plaintext"\n[[emails]]\naccount_name = "legacy"\n'
    path.write_text(content)

    bootstrap = read_bootstrap(path)

    assert bootstrap.mode == "legacy"
    assert path.read_text() == content


@pytest.mark.parametrize(
    "content",
    [
        "not valid [ toml",
        'mode = "unknown"\n',
        'mode = "managed"\nbootstrap_version = 1\n',
        'mode = "managed"\nbootstrap_version = 999\ndb_location = "db.sqlite3"\n',
        'mode = "managed"\nbootstrap_version = "1"\ndb_location = "db.sqlite3"\n',
    ],
)
def test_invalid_existing_bootstrap_fails_closed(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    path.chmod(0o600)

    with pytest.raises(BootstrapError):
        read_bootstrap(path)


def test_managed_relative_database_is_resolved_from_bootstrap_parent(tmp_path):
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    path.write_text(
        f'bootstrap_version = {BOOTSTRAP_VERSION}\nmode = "managed"\ndb_location = "state/catalog.sqlite3"\n'
    )
    path.chmod(0o600)

    bootstrap = read_bootstrap(path)

    assert bootstrap.db_path == parent / "state" / "catalog.sqlite3"


def test_write_bootstrap_preserves_legacy_rows_and_is_private(tmp_path):
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    legacy_index = parent / "db.sqlite3"
    path.write_text(
        f'credential_storage = "plaintext"\ndb_location = "{legacy_index.as_posix()}"\n'
        '[[emails]]\naccount_name = "legacy"\n'
    )
    database = parent / "catalog.sqlite3"

    historical = read_bootstrap(path)
    selected = write_bootstrap(mode="legacy", db_path=database, path=path)

    content = path.read_text()
    stored = tomllib.loads(content)
    assert historical.db_path is None
    assert selected.mode == "legacy"
    assert selected.db_path == database
    assert 'account_name = "legacy"' in content
    assert stored["bootstrap_version"] == BOOTSTRAP_VERSION
    assert stored["db_location"] == legacy_index.as_posix()
    assert stored["managed_selection"] is True
    assert stored["managed_db_location"] == database.as_posix()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_revision_only_legacy_write_never_promotes_historical_db_location(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    legacy_index = parent / "db.sqlite3"
    path.write_text(f'mode = "legacy"\ndb_location = "{legacy_index.as_posix()}"\n')
    path.chmod(0o600)

    before = read_bootstrap(path)
    written = write_bootstrap(mode="legacy", path=path, expected_revision=0)
    after = read_bootstrap(path)

    assert before.db_path is None
    assert written.db_path is None
    assert after.db_path is None
    assert tomllib.loads(path.read_text())["managed_selection"] is False


def test_revisioned_pre_release_db_location_fallback_remains_compatible(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    database = parent / "pre-release.sqlite3"
    path.write_text(
        f"bootstrap_version = {BOOTSTRAP_VERSION}\nbootstrap_revision = 2\n"
        f'mode = "legacy"\ndb_location = "{database.as_posix()}"\n'
    )
    path.chmod(0o600)

    assert read_bootstrap(path).db_path == database


def test_bootstrap_write_uses_revisioned_compare_and_swap(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"

    first = write_bootstrap(mode="legacy", path=path, expected_revision=0)
    second = write_bootstrap(
        mode="legacy",
        db_path=parent / "catalog.sqlite3",
        path=path,
        expected_revision=first.revision,
    )

    assert first.revision == 1
    assert second.revision == 2
    before_conflict = path.read_bytes()
    with pytest.raises(BootstrapError, match="changed"):
        write_bootstrap(mode="managed", path=path, expected_revision=first.revision)
    assert path.read_bytes() == before_conflict
    assert read_bootstrap(path).revision == second.revision


def test_empty_install_compare_and_swap_rejects_concurrent_legacy_file(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    observed = read_bootstrap(path)
    assert observed.exists is False
    assert observed.revision == 0

    legacy_content = 'credential_storage = "plaintext"\n[[emails]]\naccount_name = "legacy"\n'
    path.write_text(legacy_content)

    with pytest.raises(BootstrapRevisionError, match="existence changed"):
        write_bootstrap(
            mode="legacy",
            db_path=parent / "catalog.sqlite3",
            path=path,
            expected_revision=observed.revision,
            expected_exists=False,
        )
    assert path.read_text() == legacy_content


def test_concurrent_bootstrap_selectors_have_one_compare_and_swap_winner(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    barrier = Barrier(2)

    def select(database_name: str):
        barrier.wait(timeout=5)
        try:
            return write_bootstrap(
                mode="legacy",
                db_path=parent / database_name,
                path=path,
                expected_revision=0,
            )
        except BootstrapRevisionError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(select, ("first.sqlite3", "second.sqlite3")))

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, BootstrapRevisionError) for outcome in outcomes) == 1
    assert read_bootstrap(path).revision == 1


@pytest.mark.parametrize("unsafe_part", ["file", "parent"])
def test_legacy_mode_selected_catalog_requires_secure_bootstrap_authority(tmp_path, unsafe_part: str):
    parent = tmp_path / "selected"
    parent.mkdir(mode=0o700)
    path = parent / "config.toml"
    path.write_text(
        f"bootstrap_version = {BOOTSTRAP_VERSION}\n"
        'bootstrap_revision = 1\nmode = "legacy"\nmanaged_db_location = "catalog.sqlite3"\n'
    )
    path.chmod(0o600)
    if unsafe_part == "file":
        path.chmod(0o644)
    else:
        parent.chmod(0o755)

    if os.name == "posix":
        with pytest.raises(BootstrapError, match="group or other"):
            read_bootstrap(path)


def test_legacy_mode_selected_catalog_fails_closed_without_secure_filesystem_primitives(monkeypatch, tmp_path):
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    path.write_text(
        f"bootstrap_version = {BOOTSTRAP_VERSION}\n"
        'bootstrap_revision = 1\nmode = "legacy"\nmanaged_db_location = "catalog.sqlite3"\n'
    )
    path.chmod(0o600)
    monkeypatch.setattr("mcp_email_server.bootstrap._SECURE_BOOTSTRAP_FILES_SUPPORTED", False)

    with pytest.raises(BootstrapError, match="platform cannot enforce"):
        read_bootstrap(path)


def test_managed_bootstrap_requires_private_file(tmp_path):
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    path.write_text(f'bootstrap_version = {BOOTSTRAP_VERSION}\nmode = "managed"\ndb_location = "catalog.sqlite3"\n')
    path.chmod(0o644)

    if os.name == "posix":
        with pytest.raises(BootstrapError, match="group or other"):
            read_bootstrap(path)


def test_managed_bootstrap_requires_private_parent(tmp_path):
    parent = tmp_path / "permissive"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "config.toml"
    path.write_text(f'bootstrap_version = {BOOTSTRAP_VERSION}\nmode = "managed"\ndb_location = "catalog.sqlite3"\n')
    path.chmod(0o600)

    if os.name == "posix":
        with pytest.raises(BootstrapError, match=r"parent.*group or other"):
            read_bootstrap(path)


def test_bootstrap_write_rejects_unsafe_ancestor_before_file_creation(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    private = unsafe / "private"
    private.mkdir(mode=0o700)
    path = private / "config.toml"

    with pytest.raises(BootstrapError, match="ancestor permissions"):
        write_bootstrap(mode="legacy", path=path)

    assert not path.exists()


def test_bootstrap_management_fails_closed_without_secure_filesystem_primitives(monkeypatch, tmp_path):
    parent = tmp_path / "private"
    path = parent / "config.toml"
    monkeypatch.setattr("mcp_email_server.bootstrap._SECURE_BOOTSTRAP_FILES_SUPPORTED", False)

    with pytest.raises(BootstrapError, match="platform cannot enforce"):
        write_bootstrap(mode="legacy", path=path)

    assert not path.exists()
    assert not parent.exists()


def test_legacy_writer_fence_rejects_managed_before_effect(tmp_path):
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    path.write_text(f'bootstrap_version = {BOOTSTRAP_VERSION}\nmode = "managed"\ndb_location = "catalog.sqlite3"\n')
    path.chmod(0o600)

    with pytest.raises(ManagedModeWriteError, match="config select legacy"):
        assert_legacy_writable("write legacy data", path)
