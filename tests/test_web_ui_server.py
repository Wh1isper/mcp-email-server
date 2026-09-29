from __future__ import annotations

import importlib.metadata
import io
import socket
import sys
from unittest.mock import MagicMock

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from mcp_email_server import bootstrap as bootstrap_module
from mcp_email_server.application.management import ManagementStatus
from mcp_email_server.bootstrap import write_bootstrap
from mcp_email_server.cli import app
from mcp_email_server.web_ui import server as server_module


def test_global_version_check_is_bounded() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == importlib.metadata.version("mcp-email-server")


def test_ui_cli_exposes_only_no_open_and_port(monkeypatch) -> None:
    run = MagicMock()
    monkeypatch.setattr("mcp_email_server.web_ui.run_local_ui", run)
    runner = CliRunner()

    result = runner.invoke(app, ["ui", "--no-open", "--port", "0"])
    command = get_command(app)
    assert isinstance(command, click.Group)
    ui_command = command.commands["ui"]
    options = {
        option
        for parameter in ui_command.params
        if isinstance(parameter, click.Option)
        for option in (*parameter.opts, *parameter.secondary_opts)
    }

    assert result.exit_code == 0
    run.assert_called_once_with(no_open=True, port=0)
    assert options == {"--no-open", "--port"}


class _RecordingListener:
    def __init__(self, family: int, kind: int) -> None:
        self.options: list[tuple[int, int, int]] = []
        self.bound: tuple[str, int] | None = None

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def bind(self, address: tuple[str, int]) -> None:
        self.bound = address

    def listen(self, backlog: int) -> None:
        pass

    def set_inheritable(self, inheritable: bool) -> None:
        pass

    def close(self) -> None:
        pass


def _record_listener_options(monkeypatch: pytest.MonkeyPatch, platform: str) -> _RecordingListener:
    created: list[_RecordingListener] = []

    def create(family: int, kind: int) -> _RecordingListener:
        listener = _RecordingListener(family, kind)
        created.append(listener)
        return listener

    monkeypatch.setattr(server_module.sys, "platform", platform)
    monkeypatch.setattr(server_module.socket, "socket", create)
    server_module._bound_socket(0)
    assert len(created) == 1
    assert created[0].bound == ("127.0.0.1", 0)
    return created[0]


def test_windows_listener_uses_exclusive_address_and_never_reuse(monkeypatch) -> None:
    exclusive = -5
    monkeypatch.setattr(server_module.socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)

    listener = _record_listener_options(monkeypatch, "win32")

    assert listener.options == [(server_module.socket.SOL_SOCKET, exclusive, 1)]
    assert all(option != server_module.socket.SO_REUSEADDR for _, option, _ in listener.options)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows socket semantics")
def test_windows_listener_rejects_competing_address_reuse() -> None:
    with server_module._bound_socket(0) as listener:
        assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
        assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competitor:
            competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError) as error:
                competitor.bind(listener.getsockname())
            assert error.value.winerror in {10013, 10048}  # WSAEACCES or WSAEADDRINUSE


def test_posix_listener_keeps_address_reuse(monkeypatch) -> None:
    listener = _record_listener_options(monkeypatch, "linux")

    assert listener.options == [(server_module.socket.SOL_SOCKET, server_module.socket.SO_REUSEADDR, 1)]


def test_server_prebinds_exact_ipv4_loopback_opens_fragment_and_hides_token(monkeypatch, capsys) -> None:
    observed: dict[str, object] = {}
    freeze = MagicMock()

    class FakeServer:
        def __init__(self, configuration) -> None:
            freeze.assert_called_once_with()
            observed["configuration"] = configuration

        def run(self, *, sockets) -> None:
            listener = sockets[0]
            observed["address"] = listener.getsockname()
            observed["state"] = observed["configuration"].app.state.local_ui

    opened = MagicMock(return_value=True)
    monkeypatch.setattr(server_module, "freeze_process_bootstrap", freeze)
    monkeypatch.setattr(server_module.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server_module.webbrowser, "open", opened)

    server_module.run_local_ui(port=0)

    state = observed["state"]
    configuration = observed["configuration"]
    assert observed["address"][0] == "127.0.0.1"
    assert observed["address"][1] > 0
    assert configuration.host == "127.0.0.1"
    assert configuration.port == observed["address"][1]
    assert configuration.access_log is False
    assert configuration.reload is False
    assert configuration.workers == 1
    opened_url = opened.call_args.args[0]
    assert opened_url.startswith(f"http://127.0.0.1:{observed['address'][1]}{state.route_prefix}/#bootstrap=")
    token = opened_url.split("#bootstrap=", maxsplit=1)[1]
    output = capsys.readouterr().out
    assert token not in output
    assert "#bootstrap=" not in output


def test_no_open_hands_bootstrap_only_to_interactive_terminal(monkeypatch, capsys) -> None:
    observed: dict[str, object] = {}

    class FakeServer:
        def __init__(self, configuration) -> None:
            observed["state"] = configuration.app.state.local_ui

        def run(self, *, sockets) -> None:
            assert sockets[0].getsockname()[0] == "127.0.0.1"

    terminal = io.StringIO()
    opened = MagicMock()
    monkeypatch.setattr(server_module.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server_module.webbrowser, "open", opened)
    monkeypatch.setattr(server_module, "_interactive_terminal", lambda: terminal)

    server_module.run_local_ui(no_open=True, port=0)

    state = observed["state"]
    handoff = terminal.getvalue()
    assert handoff.startswith("Open this one-time local management URL:\n")
    assert f"{state.origin}{state.route_prefix}/#bootstrap=" in handoff
    token = handoff.split("#bootstrap=", maxsplit=1)[1].strip()
    assert token
    assert token not in capsys.readouterr().out
    opened.assert_not_called()


def test_no_open_fails_before_serving_without_interactive_terminal(monkeypatch, capsys) -> None:
    ran = MagicMock()

    class FakeServer:
        def __init__(self, _configuration) -> None:
            pass

        def run(self, *, sockets) -> None:
            ran(sockets)

    monkeypatch.setattr(server_module.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server_module, "_interactive_terminal", lambda: None)

    with pytest.raises(RuntimeError, match="interactive terminal"):
        server_module.run_local_ui(no_open=True, port=0)

    ran.assert_not_called()
    assert "#bootstrap=" not in capsys.readouterr().out


@pytest.mark.parametrize("catalog_state", ["missing", "corrupt"])
def test_ui_freezes_running_selection_during_unavailable_catalog_recovery(
    monkeypatch,
    tmp_path,
    catalog_state: str,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    config_path = parent / "config.toml"
    database = parent / "catalog.sqlite3"
    original = b"not a sqlite database"
    if catalog_state == "corrupt":
        database.write_bytes(original)
        database.chmod(0o600)
    monkeypatch.setenv("MCP_EMAIL_SERVER_CONFIG_PATH", config_path.as_posix())
    monkeypatch.setattr(bootstrap_module, "_PROCESS_BOOTSTRAP", None)
    write_bootstrap(mode="managed", db_path=database, path=config_path)
    observed: dict[str, ManagementStatus] = {}

    class FakeServer:
        def __init__(self, configuration) -> None:
            self.state = configuration.app.state.local_ui

        def run(self, *, sockets) -> None:
            assert sockets[0].getsockname()[0] == "127.0.0.1"
            before = self.state.management.catalog.status()
            self.state.management.catalog.select(
                "legacy",
                expected_bootstrap_revision=before.bootstrap_revision,
            )
            observed["before"] = before
            observed["after"] = self.state.management.catalog.status()

    monkeypatch.setattr(server_module.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server_module.webbrowser, "open", MagicMock(return_value=True))

    server_module.run_local_ui(port=0)

    before = observed["before"]
    after = observed["after"]
    assert before.mode == "managed"
    assert before.restart_required is False
    assert before.catalog_problem == "selected_catalog_unavailable"
    assert after.mode == "legacy"
    assert after.restart_required is True
    if catalog_state == "missing":
        assert not database.exists()
    else:
        assert database.read_bytes() == original


def test_browser_launch_failure_uses_interactive_terminal_handoff(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeServer:
        def __init__(self, configuration) -> None:
            observed["state"] = configuration.app.state.local_ui

        def run(self, *, sockets) -> None:
            assert sockets[0].getsockname()[0] == "127.0.0.1"

    terminal = io.StringIO()
    monkeypatch.setattr(server_module.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server_module.webbrowser, "open", MagicMock(return_value=False))
    monkeypatch.setattr(server_module, "_interactive_terminal", lambda: terminal)

    server_module.run_local_ui(port=0)

    state = observed["state"]
    assert f"{state.origin}{state.route_prefix}/#bootstrap=" in terminal.getvalue()
