from __future__ import annotations

import argparse
import re
from pathlib import Path

_VERSION = re.compile(r"(?:v)?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)\Z")
_PROJECT_VERSION = re.compile(r'^version = "(?P<version>[^"]+)"$', re.MULTILINE)
_LOCKED_PROJECT_VERSION = re.compile(
    r'(?m)^(?P<prefix>\[\[package\]\]\nname = "mcp-email-server"\nversion = ")'
    r'(?P<version>[^"]+)'
    r'(?P<suffix>"\nsource = \{ editable = "\." \})$'
)
_VERSIONED_FILES = (Path("pyproject.toml"), Path("uv.lock"))


def _normalize_version(value: str) -> str:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError("Release version must use vX.Y.Z or X.Y.Z with canonical non-negative integers")
    return ".".join((match["major"], match["minor"], match["patch"]))


def update_release_version(root: Path, release: str) -> str:
    """Stamp application package metadata without changing plugin versions."""

    version = _normalize_version(release)
    project_path = root / "pyproject.toml"
    lock_path = root / "uv.lock"
    project = project_path.read_text(encoding="utf-8")
    lock = lock_path.read_text(encoding="utf-8")

    project_match = _PROJECT_VERSION.search(project)
    lock_match = _LOCKED_PROJECT_VERSION.search(lock)
    if project_match is None:
        raise RuntimeError("pyproject.toml has no unambiguous project version")
    if lock_match is None:
        raise RuntimeError("uv.lock has no unambiguous editable mcp-email-server package version")

    current = project_match["version"]
    locked = lock_match["version"]
    if _normalize_version(current) != current:
        raise RuntimeError("The current project version is not canonical X.Y.Z")
    if locked != current:
        raise RuntimeError("uv.lock project version does not match pyproject.toml")

    updated_project = project[: project_match.start("version")] + version + project[project_match.end("version") :]
    updated_lock = lock[: lock_match.start("version")] + version + lock[lock_match.end("version") :]
    project_path.write_text(updated_project, encoding="utf-8")
    lock_path.write_text(updated_lock, encoding="utf-8")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description="Stamp application release metadata without changing the plugin.")
    parser.add_argument("version", help="Release version in vX.Y.Z or X.Y.Z form.")
    arguments = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    version = update_release_version(repository, arguments.version)
    print(version)


if __name__ == "__main__":
    main()
