from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
EXPECTED_CATALOG = REPOSITORY / "tests/fixtures/mcp_catalog.json"
_RUNTIME_EXCLUSIONS = (
    "/app/.env",
    "/app/.git",
    "/app/README.md",
    "/app/config.toml",
    "/app/mcp_email_server",
    "/app/pyproject.toml",
    "/app/tests",
    "/app/uv.lock",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(  # noqa: S603 - fixed Docker executable with maintainer-supplied image tag
        command,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"Container verification command failed ({process.returncode}): {' '.join(command)}\n{process.stderr}"
        )
    return process


def _response(frames: list[dict[str, Any]], request_id: int) -> dict[str, Any]:
    for frame in frames:
        if frame.get("id") == request_id:
            return frame
    raise RuntimeError(f"Container did not return JSON-RPC response id {request_id}")


def verify_container(image: str, expected_version: str) -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker is required to verify the container image")

    version = _run([docker, "run", "--rm", image, "--version"]).stdout.strip()
    _require(version == expected_version, f"Container version {version!r} does not match {expected_version!r}")

    absent_paths = " ".join(_RUNTIME_EXCLUSIONS)
    runtime_check = f'for path in {absent_paths}; do test ! -e "$path" || exit 21; done'
    _run([docker, "run", "--rm", "--entrypoint", "/bin/sh", image, "-c", runtime_check])

    requests = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "container-verification", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    payload = "".join(json.dumps(request, separators=(",", ":")) + "\n" for request in requests)
    protocol = _run([docker, "run", "--rm", "-i", image], input_text=payload)
    try:
        frames = [json.loads(line) for line in protocol.stdout.splitlines() if line]
    except json.JSONDecodeError as exc:
        raise RuntimeError("Container wrote non-JSON content to MCP stdout") from exc
    _require(all(isinstance(frame, dict) for frame in frames), "Container returned a non-object JSON-RPC frame")

    initialized = _response(frames, 1)
    _require("error" not in initialized, f"Container initialize failed: {initialized.get('error')!r}")
    result = initialized.get("result")
    _require(isinstance(result, dict), "Container initialize response has no object result")
    server_info = result.get("serverInfo")
    _require(isinstance(server_info, dict), "Container initialize response has no serverInfo")
    _require(server_info.get("version") == expected_version, "Container serverInfo version is incorrect")

    listed = _response(frames, 2)
    _require("error" not in listed, f"Container tools/list failed: {listed.get('error')!r}")
    listed_result = listed.get("result")
    _require(isinstance(listed_result, dict), "Container tools/list response has no object result")
    expected = json.loads(EXPECTED_CATALOG.read_text(encoding="utf-8"))
    _require(listed_result.get("tools") == expected["tools"], "Container MCP tool catalog differs from the snapshot")

    print(f"Container verified: image={image} version={expected_version} tools={len(expected['tools'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the release container image and its raw MCP stdio contract.")
    parser.add_argument("--image", required=True, help="Local container image tag to verify.")
    parser.add_argument("--expected-version", required=True, help="Expected application version.")
    arguments = parser.parse_args()
    verify_container(arguments.image, arguments.expected_version)


if __name__ == "__main__":
    main()
