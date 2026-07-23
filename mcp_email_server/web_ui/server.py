from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import webbrowser
from typing import TextIO

import uvicorn

from mcp_email_server.bootstrap import freeze_process_bootstrap
from mcp_email_server.runtime import close_application_runtime
from mcp_email_server.web_ui.app import LocalUiState, create_local_ui_app

_BIND_HOST = "127.0.0.1"


def _bound_socket(port: int) -> socket.socket:
    if port < 0 or port > 65535:
        raise ValueError("UI port must be between 0 and 65535")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((_BIND_HOST, port))
        listener.listen(socket.SOMAXCONN)
        listener.set_inheritable(True)
    except Exception:
        listener.close()
        raise
    return listener


def _interactive_terminal() -> TextIO | None:
    """Return an attached terminal without opening a redirected output channel."""
    for stream in (sys.stderr, sys.stdout):
        if stream.isatty():
            return stream
    return None


def _handoff_launch_url(state: LocalUiState) -> None:
    terminal = _interactive_terminal()
    if terminal is None:
        raise RuntimeError(
            "The local UI could not hand off its one-time launch URL securely. "
            "Run this command in an interactive terminal or allow the default browser to open."
        )
    print("Open this one-time local management URL:", file=terminal, flush=True)
    print(state.launch_url, file=terminal, flush=True)


def run_local_ui(*, no_open: bool = False, port: int = 0) -> None:
    """Bind the foreground UI to exact IPv4 loopback and run until shutdown."""

    freeze_process_bootstrap()
    listener = _bound_socket(port)
    actual_port = listener.getsockname()[1]
    if not isinstance(actual_port, int):
        listener.close()
        raise TypeError("Could not determine the local UI port")
    state = LocalUiState(port=actual_port)
    application = create_local_ui_app(state)
    configuration = uvicorn.Config(
        application,
        host=_BIND_HOST,
        port=actual_port,
        access_log=False,
        log_level="warning",
        server_header=False,
        date_header=False,
        reload=False,
        workers=1,
    )
    server = uvicorn.Server(configuration)
    print(f"Local management UI is listening at {state.origin}{state.route_prefix}/", flush=True)
    try:
        if no_open:
            _handoff_launch_url(state)
        else:
            try:
                opened = webbrowser.open(state.launch_url, new=2, autoraise=True)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                _handoff_launch_url(state)
        with contextlib.suppress(KeyboardInterrupt):
            server.run(sockets=[listener])
    finally:
        listener.close()

        async def cleanup() -> None:
            await state.close()
            await close_application_runtime()

        asyncio.run(cleanup())
