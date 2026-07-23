from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import sys
import threading
from collections.abc import Callable
from typing import Final, TypeVar

import anyio
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

from mcp_email_server.application.limits import APPLICATION_LIMITS

STDIO_FRAME_BYTES: Final = APPLICATION_LIMITS.mcp_stdio_frame_bytes
_READ_CHUNK_BYTES: Final = 64 * 1_024
_OVERSIZED = object()
_Result = TypeVar("_Result")


async def _daemon_io(function: Callable[..., _Result], *arguments: object) -> _Result:
    """Run blocking non-POSIX stdio in an abandonable daemon thread."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_Result] = loop.create_future()

    def complete(result: _Result | None, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)  # pyright: ignore[reportArgumentType]

    def worker() -> None:
        try:
            result = function(*arguments)
        except BaseException as exc:
            loop.call_soon_threadsafe(complete, None, exc)
        else:
            loop.call_soon_threadsafe(complete, result, None)

    threading.Thread(target=worker, name="mcp-stdio-io", daemon=True).start()
    return await future


def _supports_readiness(file_descriptor: int) -> bool:
    mode = os.fstat(file_descriptor).st_mode
    return os.name == "posix" and (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode))


async def _read_chunk(file_descriptor: int, *, use_readiness: bool) -> bytes:
    if use_readiness:
        while True:
            await anyio.wait_readable(file_descriptor)
            try:
                return os.read(file_descriptor, _READ_CHUNK_BYTES)
            except BlockingIOError:
                continue
    return await _daemon_io(os.read, file_descriptor, _READ_CHUNK_BYTES)


class _BoundedFrameReader:
    def __init__(self, file_descriptor: int, *, use_readiness: bool) -> None:
        self._file_descriptor = file_descriptor
        self._use_readiness = use_readiness
        self._buffer = bytearray()
        self._discarding = False

    async def read(self) -> bytes | object | None:
        while True:
            newline = self._buffer.find(b"\n")
            if self._discarding:
                if newline >= 0:
                    del self._buffer[: newline + 1]
                    self._discarding = False
                    return _OVERSIZED
                self._buffer.clear()
            elif newline >= 0:
                frame = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return _OVERSIZED if len(frame) > STDIO_FRAME_BYTES else frame
            elif len(self._buffer) > STDIO_FRAME_BYTES:
                self._buffer.clear()
                self._discarding = True

            chunk = await _read_chunk(self._file_descriptor, use_readiness=self._use_readiness)
            if chunk:
                self._buffer.extend(chunk)
                continue
            if self._discarding:
                self._discarding = False
                return _OVERSIZED
            if not self._buffer:
                return None
            frame = bytes(self._buffer)
            self._buffer.clear()
            return _OVERSIZED if len(frame) > STDIO_FRAME_BYTES else frame


async def _stdin_reader(
    send_stream: ObjectSendStream[SessionMessage | Exception],
    eof: anyio.Event,
    file_descriptor: int,
    use_readiness: bool,
) -> None:
    reader = _BoundedFrameReader(file_descriptor, use_readiness=use_readiness)
    try:
        async with send_stream:
            while True:
                raw = await reader.read()
                if raw is None:
                    return
                if raw is _OVERSIZED:
                    await send_stream.send(ValueError("Invalid or oversized JSON-RPC frame"))
                    continue
                if not isinstance(raw, bytes):
                    raise TypeError("Bounded stdio reader returned an invalid frame type")
                try:
                    text = raw.decode("utf-8", errors="strict")
                    message = types.JSONRPCMessage.model_validate_json(text)
                except (UnicodeDecodeError, ValueError):
                    await send_stream.send(ValueError("Invalid or oversized JSON-RPC frame"))
                    continue
                await send_stream.send(SessionMessage(message))
    finally:
        eof.set()


async def _write_payload(file_descriptor: int, payload: bytes, *, use_readiness: bool) -> None:
    remaining = memoryview(payload)
    while remaining:
        if use_readiness:
            await anyio.wait_writable(file_descriptor)
            try:
                written = os.write(file_descriptor, remaining)
            except BlockingIOError:
                continue
        else:
            written = await _daemon_io(os.write, file_descriptor, remaining)
        if written <= 0:
            raise BrokenPipeError("stdio output closed")
        remaining = remaining[written:]


async def _stdout_writer(
    receive_stream: ObjectReceiveStream[SessionMessage],
    file_descriptor: int,
    use_readiness: bool,
) -> None:
    async with receive_stream:
        async for session_message in receive_stream:
            payload = session_message.message.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8") + b"\n"
            await _write_payload(file_descriptor, payload, use_readiness=use_readiness)


async def _run_server(
    server: FastMCP,
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    write_stream: MemoryObjectSendStream[SessionMessage],
    done: anyio.Event,
) -> None:
    try:
        async with read_stream, write_stream:
            await server._mcp_server.run(  # pyright: ignore[reportPrivateUsage]
                read_stream,
                write_stream,
                server._mcp_server.create_initialization_options(),  # pyright: ignore[reportPrivateUsage]
            )
    finally:
        done.set()


async def run_bounded_stdio_async(server: FastMCP) -> None:
    """Serve newline-delimited MCP frames with bounded, cancellation-aware I/O."""
    stdin_descriptor = sys.stdin.fileno()
    stdout_descriptor = sys.stdout.fileno()
    stdin_readiness = _supports_readiness(stdin_descriptor)
    stdout_readiness = _supports_readiness(stdout_descriptor)
    original_blocking: tuple[bool | None, bool | None] = (None, None)
    if stdin_readiness:
        original_blocking = (os.get_blocking(stdin_descriptor), original_blocking[1])
        os.set_blocking(stdin_descriptor, False)
    if stdout_readiness:
        original_blocking = (original_blocking[0], os.get_blocking(stdout_descriptor))
        os.set_blocking(stdout_descriptor, False)

    incoming_send, incoming_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    outgoing_send, outgoing_receive = anyio.create_memory_object_stream[SessionMessage](0)
    eof = anyio.Event()
    server_done = anyio.Event()

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_stdin_reader, incoming_send, eof, stdin_descriptor, stdin_readiness)
            task_group.start_soon(_stdout_writer, outgoing_receive, stdout_descriptor, stdout_readiness)
            task_group.start_soon(_run_server, server, incoming_receive, outgoing_send, server_done)
            await eof.wait()
            with anyio.move_on_after(0.25):
                await server_done.wait()
            task_group.cancel_scope.cancel()
    finally:
        if original_blocking[0] is not None:
            with contextlib.suppress(OSError):
                os.set_blocking(stdin_descriptor, original_blocking[0])
        if original_blocking[1] is not None:
            with contextlib.suppress(OSError):
                os.set_blocking(stdout_descriptor, original_blocking[1])


def run_bounded_stdio(server: FastMCP) -> None:
    """Run the bounded stdio adapter from the synchronous CLI."""
    anyio.run(run_bounded_stdio_async, server)
