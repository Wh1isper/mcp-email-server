from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from mcp_email_server import stdio as stdio_module
from mcp_email_server.stdio import STDIO_FRAME_BYTES

REPOSITORY = Path(__file__).resolve().parents[1]
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "raw-stdio-contract", "version": "1"},
    },
}


class RawStdioProcess:
    def __init__(self, process: asyncio.subprocess.Process, stderr_path: Path) -> None:
        self.process = process
        self.stderr_path = stderr_path
        self.frames: list[dict[str, Any]] = []

    async def send(self, value: dict[str, Any]) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        stdin.write(payload)
        await stdin.drain()

    async def send_bytes(self, payload: bytes) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write(payload)
        await stdin.drain()

    async def read(self) -> dict[str, Any]:
        stdout = self.process.stdout
        assert stdout is not None
        raw = await asyncio.wait_for(stdout.readline(), timeout=10)
        assert raw.endswith(b"\n"), raw
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(decoded)
        assert isinstance(value, dict)
        assert value.get("jsonrpc") == "2.0"
        self.frames.append(value)
        return value

    async def response(self, request_id: int) -> dict[str, Any]:
        while True:
            frame = await self.read()
            if frame.get("id") == request_id:
                return frame

    async def initialize(self) -> dict[str, Any]:
        await self.send(INITIALIZE)
        response = await self.response(1)
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    async def close_stdin(self) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.close()
        await stdin.wait_closed()

    async def wait(self) -> int:
        return await asyncio.wait_for(self.process.wait(), timeout=10)

    def stderr(self) -> bytes:
        return self.stderr_path.read_bytes()


@asynccontextmanager
async def _server(tmp_path: Path, *, probe: bool = False) -> AsyncIterator[RawStdioProcess]:
    config = tmp_path / "config.toml"
    config.write_text('credential_storage = "plaintext"\n', encoding="utf-8")
    stderr_path = tmp_path / "stderr.log"
    stderr_handle = stderr_path.open("wb")
    environment = os.environ.copy()
    environment["MCP_EMAIL_SERVER_CONFIG_PATH"] = str(config)
    environment["TMPDIR"] = str(tmp_path)
    if probe:
        command = [
            sys.executable,
            str(REPOSITORY / "tests/fixtures/raw_stdio_probe_server.py"),
            str(tmp_path),
        ]
    else:
        executable = Path(sys.executable).with_name("mcp-email-server")
        command = [str(executable), "stdio"]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=REPOSITORY,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_handle,
    )
    server = RawStdioProcess(process, stderr_path)
    try:
        yield server
    finally:
        if process.returncode is None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
        stderr_handle.close()


@pytest.mark.asyncio
async def test_raw_stdio_initialization_catalog_stdout_purity_and_idle_eof(tmp_path: Path) -> None:
    expected = json.loads((REPOSITORY / "tests/fixtures/mcp_catalog.json").read_text(encoding="utf-8"))
    async with _server(tmp_path) as server:
        initialized = await server.initialize()
        assert initialized["result"]["protocolVersion"] == "2025-06-18"
        assert initialized["result"]["serverInfo"] == {
            "name": "email",
            "version": importlib.metadata.version("mcp-email-server"),
        }

        requests = (
            (2, "tools/list", "tools"),
            (3, "resources/list", "resources"),
            (4, "resources/templates/list", "resourceTemplates"),
            (5, "prompts/list", "prompts"),
        )
        results: dict[str, Any] = {}
        for request_id, method, result_key in requests:
            await server.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}})
            results[result_key] = (await server.response(request_id))["result"][result_key]

        assert results["tools"] == expected["tools"]
        assert results["resources"] == expected["resources"]
        assert results["resourceTemplates"] == expected["resource_templates"]
        assert results["prompts"] == expected["prompts"]
        await server.close_stdin()
        assert await server.wait() == 0
        assert all(frame["jsonrpc"] == "2.0" for frame in server.frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "recovered_frame"),
    [
        ([b"abcde", b"discarded-tail\nok\n"], b"ok\n"),
        ([b"abcde", b""], None),
    ],
)
async def test_bounded_reader_discards_chunked_oversized_frame_until_newline_or_eof(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[bytes],
    recovered_frame: bytes | None,
) -> None:
    remaining = iter(chunks)
    reads = 0

    async def read_chunk(_file_descriptor: int, *, use_readiness: bool) -> bytes:
        nonlocal reads
        del use_readiness
        reads += 1
        try:
            return next(remaining)
        except StopIteration:
            return b""

    monkeypatch.setattr(stdio_module, "STDIO_FRAME_BYTES", 4)
    monkeypatch.setattr(stdio_module, "_read_chunk", read_chunk)
    reader = stdio_module._BoundedFrameReader(0, use_readiness=False)

    assert await reader.read() is stdio_module._OVERSIZED
    assert reads == 2
    if recovered_frame is None:
        assert await reader.read() is None
        assert reads == 3
    else:
        assert await reader.read() == recovered_frame
        assert reads == 2


@pytest.mark.asyncio
async def test_raw_stdio_redacts_malformed_utf8_json_and_oversized_frames_then_recovers(tmp_path: Path) -> None:
    utf8_sentinel = b"MALFORMED_UTF8_SECRET_SENTINEL"
    json_sentinel = b"MALFORMED_JSON_SECRET_SENTINEL"
    oversized_sentinel = b"OVERSIZED_SECRET_SENTINEL"
    async with _server(tmp_path) as server:
        await server.send_bytes(utf8_sentinel + b"-\xff\n")
        malformed_utf8 = await server.read()
        assert malformed_utf8["method"] == "notifications/message"

        await server.send_bytes(json_sentinel + b"-not-json\n")
        malformed_json = await server.read()
        assert malformed_json["method"] == "notifications/message"

        padding = b"x" * (STDIO_FRAME_BYTES - len(oversized_sentinel) + 1)
        await server.send_bytes(oversized_sentinel + padding + b"\n")
        oversized = await server.read()
        assert oversized["method"] == "notifications/message"

        await server.initialize()
        await server.send({"jsonrpc": "2.0", "id": 6, "method": "ping", "params": {}})
        assert (await server.response(6))["result"] == {}
        await server.close_stdin()
        assert await server.wait() == 0

    diagnostics = server.stderr()
    assert utf8_sentinel not in diagnostics
    assert json_sentinel not in diagnostics
    assert oversized_sentinel not in diagnostics
    assert len(diagnostics) < 64 * 1_024


@pytest.mark.skipif(os.name != "posix", reason="Broken-pipe stdio behavior is POSIX-specific")
def test_raw_stdio_broken_stdout_does_not_wait_for_open_stdin(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('credential_storage = "plaintext"\n', encoding="utf-8")
    stderr_path = tmp_path / "broken-stdout-stderr.log"
    environment = os.environ.copy()
    environment["MCP_EMAIL_SERVER_CONFIG_PATH"] = str(config)
    executable = Path(sys.executable).with_name("mcp-email-server")

    with stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(  # noqa: S603 - exact test-owned executable and arguments
            [str(executable), "stdio"],
            cwd=REPOSITORY,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(json.dumps(INITIALIZE, separators=(",", ":")).encode() + b"\n")
            process.stdin.flush()
            initialized = json.loads(process.stdout.readline())
            assert initialized["id"] == 1

            process.stdout.close()
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            process.stdin.write(b'{"jsonrpc":"2.0","id":7,"method":"ping","params":{}}\n')
            process.stdin.flush()
            assert process.wait(timeout=5) != 0
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_raw_stdio_accepts_regular_file_redirection_and_devnull_eof(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('credential_storage = "plaintext"\n', encoding="utf-8")
    environment = os.environ.copy()
    environment["MCP_EMAIL_SERVER_CONFIG_PATH"] = str(config)
    executable = Path(sys.executable).with_name("mcp-email-server")

    devnull = subprocess.run(  # noqa: S603 - exact test-owned executable and arguments
        [str(executable), "stdio"],
        cwd=REPOSITORY,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert devnull.returncode == 0
    assert devnull.stdout == b""
    assert b"Traceback" not in devnull.stderr

    input_path = tmp_path / "protocol-input.ndjson"
    output_path = tmp_path / "protocol-output.ndjson"
    input_path.write_bytes(json.dumps(INITIALIZE, separators=(",", ":")).encode() + b"\n")
    with input_path.open("rb") as input_file, output_path.open("wb") as output_file:
        redirected = subprocess.run(  # noqa: S603 - exact test-owned executable and arguments
            [str(executable), "stdio"],
            cwd=REPOSITORY,
            env=environment,
            stdin=input_file,
            stdout=output_file,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    assert redirected.returncode == 0
    response = json.loads(output_path.read_bytes().splitlines()[0])
    assert response["id"] == 1
    assert response["result"]["protocolVersion"] == "2025-06-18"


async def _wait_for_path(path: Path) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for {path.name}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_raw_stdio_cancellation_reaches_tool_and_session_remains_usable(tmp_path: Path) -> None:
    async with _server(tmp_path, probe=True) as server:
        await server.initialize()
        await server.send({
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {"name": "list_mailboxes", "arguments": {"account_name": "blocked"}},
        })
        await _wait_for_path(tmp_path / "started")
        await server.send({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 40, "reason": "raw acceptance cancellation"},
        })
        cancelled = await server.response(40)
        assert cancelled["error"]["message"] == "Request cancelled"
        await _wait_for_path(tmp_path / "cancelled")

        await server.send({"jsonrpc": "2.0", "id": 41, "method": "ping", "params": {}})
        assert (await server.response(41))["result"] == {}
        await server.close_stdin()
        assert await server.wait() == 0

    artifact = Path((tmp_path / "artifact-path").read_text(encoding="utf-8"))
    assert not artifact.exists()
    assert not artifact.parent.exists()


@pytest.mark.asyncio
async def test_raw_stdio_eof_cancels_inflight_tool_and_cleans_runtime(tmp_path: Path) -> None:
    async with _server(tmp_path, probe=True) as server:
        await server.initialize()
        await server.send({
            "jsonrpc": "2.0",
            "id": 50,
            "method": "tools/call",
            "params": {"name": "list_mailboxes", "arguments": {"account_name": "blocked"}},
        })
        await _wait_for_path(tmp_path / "started")
        await server.close_stdin()
        assert await server.wait() == 0
        await _wait_for_path(tmp_path / "cancelled")

    artifact = Path((tmp_path / "artifact-path").read_text(encoding="utf-8"))
    assert not artifact.exists()
    assert not artifact.parent.exists()
