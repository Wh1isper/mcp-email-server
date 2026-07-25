from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from dev.set_release_version import _LOCKED_PROJECT_VERSION, _VERSIONED_FILES, update_release_version

REPOSITORY = Path(__file__).resolve().parents[1]


def _version_tree(tmp_path: Path) -> Path:
    for relative in _VERSIONED_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, target)
    return tmp_path


def _application_versions(root: Path) -> tuple[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    lock = (root / "uv.lock").read_text(encoding="utf-8")
    match = _LOCKED_PROJECT_VERSION.search(lock)
    assert match is not None
    return project, match["version"]


def test_release_version_updates_only_application_package_metadata(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)
    plugin_path = Path("plugins/mcp-email-server/.codex-plugin/plugin.json")
    staged_plugin = root / plugin_path
    staged_plugin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPOSITORY / plugin_path, staged_plugin)
    plugin_before = staged_plugin.read_bytes()
    current, locked = _application_versions(root)
    assert locked == current
    major, minor, patch = (int(part) for part in current.split("."))
    first = f"{major + 1}.{minor + 1}.{patch + 1}"
    second = f"{major + 1}.{minor + 1}.{patch + 2}"

    assert update_release_version(root, f"v{first}") == first
    assert _application_versions(root) == (first, first)

    assert update_release_version(root, second) == second
    assert _application_versions(root) == (second, second)
    assert staged_plugin.read_bytes() == plugin_before


@pytest.mark.parametrize("version", ["", "v1", "1.2", "01.2.3", "1.02.3", "1.2.03", "1.2.3.dev1"])
def test_release_version_rejects_noncanonical_tags_without_changes(tmp_path: Path, version: str) -> None:
    root = _version_tree(tmp_path)
    before = {relative: (root / relative).read_bytes() for relative in _VERSIONED_FILES}

    with pytest.raises(ValueError, match=r"vX\.Y\.Z"):
        update_release_version(root, version)

    assert {relative: (root / relative).read_bytes() for relative in _VERSIONED_FILES} == before


def test_release_version_preflights_lock_consistency_before_writing(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)
    lock_path = root / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    match = _LOCKED_PROJECT_VERSION.search(lock)
    assert match is not None
    lock_path.write_text(lock[: match.start("version")] + "9.9.9" + lock[match.end("version") :], encoding="utf-8")
    before = {relative: (root / relative).read_bytes() for relative in _VERSIONED_FILES}

    with pytest.raises(RuntimeError, match="does not match"):
        update_release_version(root, "v1.2.3")

    assert {relative: (root / relative).read_bytes() for relative in _VERSIONED_FILES} == before
