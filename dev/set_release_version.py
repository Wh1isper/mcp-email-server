from __future__ import annotations

import argparse
import re
from pathlib import Path

_VERSION = re.compile(r"(?:v)?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)\Z")
_PROJECT_VERSION = re.compile(r'^version = "(?P<version>[^"]+)"$', re.MULTILINE)
_VERSIONED_FILES = (
    Path("pyproject.toml"),
    Path(".claude-plugin/marketplace.json"),
    Path("plugins/mcp-email-server/.codex-plugin/plugin.json"),
    Path("plugins/mcp-email-server/.claude-plugin/plugin.json"),
    Path("plugins/mcp-email-server/skills/safe-email-operations/references/installation.md"),
)


def _normalize_version(value: str) -> str:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError("Release version must use vX.Y.Z or X.Y.Z with canonical non-negative integers")
    return ".".join((match["major"], match["minor"], match["patch"]))


def update_release_version(root: Path, release: str) -> str:
    """Synchronize application, plugin, marketplace, and pinned-install versions."""

    version = _normalize_version(release)
    project_path = root / "pyproject.toml"
    project = project_path.read_text(encoding="utf-8")
    match = _PROJECT_VERSION.search(project)
    if match is None:
        raise RuntimeError("pyproject.toml has no unambiguous project version")
    current = match["version"]
    if _normalize_version(current) != current:
        raise RuntimeError("The current project version is not canonical X.Y.Z")

    updates: dict[Path, str] = {}
    for relative in _VERSIONED_FILES:
        path = root / relative
        content = path.read_text(encoding="utf-8")
        if current not in content:
            raise RuntimeError(f"{relative.as_posix()} does not contain the current release version")
        updates[path] = content.replace(f"v{current}", f"v{version}").replace(current, version)
    for path, content in updates.items():
        path.write_text(content, encoding="utf-8")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize release version metadata.")
    parser.add_argument("version", help="Release version in vX.Y.Z or X.Y.Z form.")
    arguments = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    version = update_release_version(repository, arguments.version)
    print(version)


if __name__ == "__main__":
    main()
