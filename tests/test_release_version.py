from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dev.set_release_version import _VERSIONED_FILES, update_release_version

REPOSITORY = Path(__file__).resolve().parents[1]


def _version_tree(tmp_path: Path) -> Path:
    for relative in _VERSIONED_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, target)
    return tmp_path


def test_release_version_updates_all_application_and_plugin_metadata(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)

    assert update_release_version(root, "v1.2.3") == "1.2.3"
    for relative in _VERSIONED_FILES:
        content = (root / relative).read_text(encoding="utf-8")
        assert "0.0.1" not in content
        assert "1.2.3" in content

    assert update_release_version(root, "1.2.4") == "1.2.4"
    for relative in _VERSIONED_FILES:
        content = (root / relative).read_text(encoding="utf-8")
        assert "1.2.3" not in content
        assert "1.2.4" in content


@pytest.mark.parametrize("version", ["", "v1", "1.2", "01.2.3", "1.02.3", "1.2.03", "1.2.3.dev1"])
def test_release_version_rejects_noncanonical_tags_without_changes(tmp_path: Path, version: str) -> None:
    root = _version_tree(tmp_path)
    before = {relative: (root / relative).read_bytes() for relative in _VERSIONED_FILES}

    with pytest.raises(ValueError, match=r"vX\.Y\.Z"):
        update_release_version(root, version)

    assert {relative: (root / relative).read_bytes() for relative in _VERSIONED_FILES} == before


def test_release_version_preflights_every_file_before_writing(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)
    missing = root / _VERSIONED_FILES[-1]
    missing.write_text(missing.read_text(encoding="utf-8").replace("0.0.1", "unversioned"), encoding="utf-8")
    before = {relative: (root / relative).read_bytes() for relative in _VERSIONED_FILES}

    with pytest.raises(RuntimeError, match="does not contain"):
        update_release_version(root, "v1.2.3")

    assert {relative: (root / relative).read_bytes() for relative in _VERSIONED_FILES} == before
