from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
FRONTEND = REPOSITORY / "frontend"
DISTRIBUTION = FRONTEND / "dist"
STATIC = REPOSITORY / "mcp_email_server" / "web_ui" / "static"
MANIFEST = FRONTEND / "embedded-assets.json"
ASSET_REFERENCE = re.compile(r"(?:src|href)=[\"']\./([^\"']+)[\"']")
_GENERATED_DIRECTORIES = {"coverage", "dist", "node_modules", "playwright-report", "test-results"}
_MANIFEST_VERSION = 1


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _validate_assets(files: dict[str, bytes], *, packaged: bool) -> dict[str, bytes]:
    index = files.get("index.html")
    if index is None:
        raise RuntimeError("frontend index.html is missing")
    references = set(ASSET_REFERENCE.findall(index.decode("utf-8")))
    if not references:
        raise RuntimeError("frontend index references no packaged assets")
    required = {"index.html", *references}
    if packaged:
        required.add("THIRD_PARTY_NOTICES.md")
    missing = required - files.keys()
    if missing:
        raise RuntimeError(f"frontend package references missing assets: {sorted(missing)}")
    unexpected = set(files) - required
    if unexpected:
        raise RuntimeError(f"frontend package contains stale or unreferenced files: {sorted(unexpected)}")
    if any(path.endswith(".map") for path in files):
        raise RuntimeError("frontend source maps must not be packaged")
    lowered_names = "\n".join(files).lower()
    if "mcp-app" in lowered_names or "serviceworker" in lowered_names or "service-worker" in lowered_names:
        raise RuntimeError("frontend build contains an out-of-scope runtime surface")
    if b"https://" in index or b"http://" in index:
        raise RuntimeError("frontend index references a remote runtime asset")
    return files


def _validated_distribution() -> dict[str, bytes]:
    files = _validate_assets(_files(DISTRIBUTION), packaged=False)
    files["THIRD_PARTY_NOTICES.md"] = (FRONTEND / "THIRD_PARTY_NOTICES.md").read_bytes()
    return files


def _validated_static() -> dict[str, bytes]:
    return _validate_assets(_files(STATIC), packaged=True)


def _frontend_sources() -> dict[str, bytes]:
    sources: dict[str, bytes] = {}
    for path in sorted(FRONTEND.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(FRONTEND)
        if relative == MANIFEST.relative_to(FRONTEND):
            continue
        if relative.parts[0] in _GENERATED_DIRECTORIES or path.name.endswith(".tsbuildinfo"):
            continue
        sources[f"frontend/{relative.as_posix()}"] = path.read_bytes()
    sources["dev/build_frontend.py"] = Path(__file__).read_bytes()
    return sources


def _digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _asset_hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())}


def _write_manifest(files: dict[str, bytes]) -> None:
    content = json.dumps(
        {
            "version": _MANIFEST_VERSION,
            "source_sha256": _digest(_frontend_sources()),
            "assets": _asset_hashes(files),
        },
        indent=4,
        sort_keys=True,
    )
    temporary = MANIFEST.with_suffix(".json.tmp")
    temporary.write_text(f"{content}\n")
    temporary.replace(MANIFEST)


def _verify_manifest(files: dict[str, bytes]) -> None:
    try:
        manifest = json.loads(MANIFEST.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("embedded frontend manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"version", "source_sha256", "assets"}:
        raise RuntimeError("embedded frontend manifest has an invalid shape")
    if manifest["version"] != _MANIFEST_VERSION:
        raise RuntimeError("embedded frontend manifest version is unsupported")
    if manifest["source_sha256"] != _digest(_frontend_sources()):
        raise RuntimeError("frontend sources differ from the staged asset manifest; run `make frontend`")
    if manifest["assets"] != _asset_hashes(files):
        raise RuntimeError("packaged frontend assets differ from the staged asset manifest; run `make frontend`")


def _stage(files: dict[str, bytes]) -> None:
    STATIC.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="web-ui-static-", dir=STATIC.parent) as temporary:
        staged = Path(temporary) / "static"
        for name, content in files.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        backup = STATIC.with_name("static.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if STATIC.exists():
            STATIC.rename(backup)
        staged.rename(STATIC)
        if backup.exists():
            shutil.rmtree(backup)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or verify embedded local management UI assets.")
    parser.add_argument("--check", action="store_true", help="Verify staged assets without invoking Node.")
    arguments = parser.parse_args()

    if not arguments.check:
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("npm is required to rebuild the frontend")
        subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)  # noqa: S603 - maintainer tool
        subprocess.run([npm, "run", "check"], cwd=FRONTEND, check=True)  # noqa: S603 - maintainer tool
        subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)  # noqa: S603 - maintainer tool
        expected = _validated_distribution()
        _stage(expected)
        actual = _validated_static()
        if actual != expected:
            raise RuntimeError("packaged frontend assets differ from the validated frontend build")
        _write_manifest(actual)

    actual = _validated_static()
    _verify_manifest(actual)
    print(f"Embedded frontend assets verified: sha256={_digest(actual)}")


if __name__ == "__main__":
    main()
