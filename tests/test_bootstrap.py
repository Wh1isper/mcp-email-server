from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mcp_email_server.bootstrap import (
    BOOTSTRAP_VERSION,
    BootstrapError,
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
    path.write_text('credential_storage = "plaintext"\n[[emails]]\naccount_name = "legacy"\n')
    database = parent / "catalog.sqlite3"

    selected = write_bootstrap(mode="legacy", db_path=database, path=path)

    content = path.read_text()
    assert selected.mode == "legacy"
    assert 'account_name = "legacy"' in content
    assert f"bootstrap_version = {BOOTSTRAP_VERSION}" in content
    assert f'db_location = "{database.as_posix()}"' in content
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


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


def test_legacy_writer_fence_rejects_managed_before_effect(tmp_path):
    parent = _private_directory(tmp_path / "private")
    path = parent / "config.toml"
    path.write_text(f'bootstrap_version = {BOOTSTRAP_VERSION}\nmode = "managed"\ndb_location = "catalog.sqlite3"\n')
    path.chmod(0o600)

    with pytest.raises(ManagedModeWriteError, match="config select legacy"):
        assert_legacy_writable("write legacy data", path)
