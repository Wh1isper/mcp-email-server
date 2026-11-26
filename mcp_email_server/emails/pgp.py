"""Bounded, non-interactive GnuPG access for the local read path."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path

import anyio

from mcp_email_server.application.limits import APPLICATION_LIMITS

_TIMEOUT_SECONDS = 3.0
_OUTPUT_BYTES = APPLICATION_LIMITS.total_attachment_bytes
_STATUS_BYTES = 64 * 1024
_PIPE_CHUNK_BYTES = 64 * 1024


def _gpg_executable() -> Path | None:
    """Use an absolute PATH entry, never an implicit or explicit CWD search."""
    name = "gpg.exe" if os.name == "nt" else "gpg"
    cwd = Path.cwd().resolve()
    for entry in os.get_exec_path():
        directory = Path(entry)
        if not directory.is_absolute():
            continue
        directory = directory.resolve()
        if directory == cwd:
            continue
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            # Keep the executable name; some installations symlink it to a
            # versioned binary. On Windows only the .exe name is searched.
            return candidate
    return None


class _OutputLimitExceeded(Exception):
    pass


async def _read_pipe(pipe: asyncio.StreamReader, maximum: int) -> bytes:
    data = bytearray()
    while chunk := await pipe.read(min(_PIPE_CHUNK_BYTES, maximum - len(data) + 1)):
        if len(data) + len(chunk) > maximum:
            raise _OutputLimitExceeded
        data.extend(chunk)
    return bytes(data)


async def _write_pipe(pipe: asyncio.StreamWriter, ciphertext: bytes) -> None:
    try:
        for offset in range(0, len(ciphertext), _PIPE_CHUNK_BYTES):
            pipe.write(ciphertext[offset : offset + _PIPE_CHUNK_BYTES])
            await pipe.drain()
    finally:
        pipe.close()


def _integrity_verified(status: bytes) -> bool:
    records = [line.removeprefix(b"[GNUPG:] ").split() for line in status.splitlines() if line.startswith(b"[GNUPG:] ")]
    if records.count([b"DECRYPTION_OKAY"]) != 1 or records.count([b"END_DECRYPTION"]) != 1:
        return False
    if any(fields and fields[0] in {b"BADMDC", b"ERRMDC", b"DECRYPTION_FAILED"} for fields in records):
        return False
    info = [fields for fields in records if len(fields) >= 3 and fields[0] == b"DECRYPTION_INFO"]
    if len(info) != 1:
        return False
    # Refuse legacy unauthenticated packets. AEAD may report MDC method 0.
    _, mdc, _cipher, *extra = info[0]
    return (mdc.isdigit() and mdc != b"0") or bool(extra and extra[0].isdigit() and extra[0] != b"0")


async def _decrypt(process: asyncio.subprocess.Process, ciphertext: bytes) -> bytes | None:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        await process.wait()
        return None
    writer = asyncio.create_task(_write_pipe(process.stdin, ciphertext))
    stdout = asyncio.create_task(_read_pipe(process.stdout, _OUTPUT_BYTES))
    stderr = asyncio.create_task(_read_pipe(process.stderr, _STATUS_BYTES))
    try:
        await asyncio.gather(writer, stdout, stderr)
        if await process.wait() == 0 and _integrity_verified(stderr.result()):
            return stdout.result()
        return None
    finally:
        # asyncio exposes no StreamReader.close() on subprocess pipes. Close
        # the owning transport so a helper inheriting a pipe cannot hold our
        # cleanup open after the direct child has exited.
        with anyio.CancelScope(shield=True):
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            for task in (writer, stdout, stderr):
                task.cancel()
            process._transport.close()  # pyright: ignore[reportAttributeAccessIssue]
            await asyncio.gather(writer, stdout, stderr, return_exceptions=True)
            await process.wait()


async def decrypt_pgp(ciphertext: bytes) -> bytes | None:
    """Return integrity-checked bytes, or no result; never expose diagnostics."""
    try:
        executable = _gpg_executable()
        if executable is None:
            return None
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "--no-options",
                "--batch",
                "--no-tty",
                "--pinentry-mode",
                "error",
                "--max-output",
                str(_OUTPUT_BYTES),
                "--status-fd",
                "2",
                "--output",
                "-",
                "--decrypt",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return await _decrypt(process, ciphertext)
    except (OSError, RuntimeError, TimeoutError, _OutputLimitExceeded):
        return None
