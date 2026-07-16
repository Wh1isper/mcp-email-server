import json
from pathlib import Path

import pytest

from mcp_email_server.tools import installer


def _write_template(path: Path) -> None:
    path.write_text(
        json.dumps({
            "mcpServers": {
                "mcp-email-server": {
                    "command": "{{ ENTRYPOINT }}",
                    "args": ["stdio"],
                    "env": {"MCP_EMAIL_SERVER_LOG_LEVEL": "INFO"},
                }
            }
        })
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_get_endpoint_path_prefers_path_executable(monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda command: f"/usr/local/bin/{command}")

    assert installer.get_endpoint_path() == "/usr/local/bin/mcp-email-server"


def test_get_endpoint_path_uses_current_python_bin(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    endpoint = bin_dir / "mcp-email-server"
    endpoint.write_text("#!/bin/sh\n")

    monkeypatch.setattr(installer.shutil, "which", lambda command: None)
    monkeypatch.setattr(installer.sys, "executable", str(bin_dir / "python"))

    assert installer.get_endpoint_path() == str(endpoint)


def test_get_endpoint_path_uses_windows_executable_fallback(monkeypatch, tmp_path):
    bin_dir = tmp_path / "Scripts"
    bin_dir.mkdir()
    endpoint = bin_dir / "mcp-email-server.exe"
    endpoint.write_text("binary")

    monkeypatch.setattr(installer.shutil, "which", lambda command: None)
    monkeypatch.setattr(installer.sys, "executable", str(bin_dir / "python.exe"))

    assert installer.get_endpoint_path() == str(endpoint)


def test_get_endpoint_path_falls_back_to_script_name(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.shutil, "which", lambda command: None)
    monkeypatch.setattr(installer.sys, "executable", str(tmp_path / "python"))

    assert installer.get_endpoint_path() == "mcp-email-server"


def test_install_claude_desktop_creates_config(monkeypatch, tmp_path):
    config_path = tmp_path / "Claude" / "claude_desktop_config.json"
    template_path = tmp_path / "claude_desktop_config_template.json"
    _write_template(template_path)

    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_TEMPLATE", template_path)
    monkeypatch.setattr(installer, "get_endpoint_path", lambda: "/usr/local/bin/mcp-email-server")

    installer.install_claude_desktop()

    config = _read_json(config_path)
    assert config == {
        "mcpServers": {
            "mcp-email-server": {
                "command": "/usr/local/bin/mcp-email-server",
                "args": ["stdio"],
                "env": {"MCP_EMAIL_SERVER_LOG_LEVEL": "INFO"},
            }
        }
    }


def test_install_claude_desktop_merges_existing_config(monkeypatch, tmp_path):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({
            "globalShortcut": "Ctrl+Space",
            "mcpServers": {
                "other-server": {
                    "command": "other-command",
                    "args": [],
                }
            },
        })
    )
    template_path = tmp_path / "claude_desktop_config_template.json"
    _write_template(template_path)

    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_TEMPLATE", template_path)
    monkeypatch.setattr(installer, "get_endpoint_path", lambda: "/opt/bin/mcp-email-server")

    installer.install_claude_desktop()

    config = _read_json(config_path)
    assert config["globalShortcut"] == "Ctrl+Space"
    assert config["mcpServers"]["other-server"] == {"command": "other-command", "args": []}
    assert config["mcpServers"]["mcp-email-server"]["command"] == "/opt/bin/mcp-email-server"


def test_install_claude_desktop_replaces_legacy_server(monkeypatch, tmp_path):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({
            "mcpServers": {
                "mcp-email-server": {"command": "stale-new-command"},
                "mcp_email_server": {"command": "old-underscore-command"},
                "zerolib-email": {"command": "old-brand-command"},
                "other-server": {"command": "other-command"},
            }
        })
    )
    template_path = tmp_path / "claude_desktop_config_template.json"
    _write_template(template_path)

    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_TEMPLATE", template_path)
    monkeypatch.setattr(installer, "get_endpoint_path", lambda: "/opt/bin/mcp-email-server")

    installer.install_claude_desktop()

    servers = _read_json(config_path)["mcpServers"]
    assert "mcp_email_server" not in servers
    assert "zerolib-email" not in servers
    assert servers["mcp-email-server"]["command"] == "/opt/bin/mcp-email-server"
    assert servers["other-server"] == {"command": "other-command"}


def test_install_claude_desktop_raises_when_platform_is_unsupported(monkeypatch):
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", None)

    with pytest.raises(NotImplementedError):
        installer.install_claude_desktop()


def test_uninstall_claude_desktop_removes_email_server(monkeypatch, tmp_path):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({
            "mcpServers": {
                "mcp-email-server": {"command": "mcp-email-server"},
                "mcp_email_server": {"command": "legacy-underscore-command"},
                "zerolib-email": {"command": "legacy-brand-command"},
                "other-server": {"command": "other-command"},
            }
        })
    )
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))

    installer.uninstall_claude_desktop()

    assert _read_json(config_path) == {"mcpServers": {"other-server": {"command": "other-command"}}}


def test_uninstall_claude_desktop_ignores_missing_config(monkeypatch, tmp_path):
    config_path = tmp_path / "missing.json"
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))

    installer.uninstall_claude_desktop()

    assert not config_path.exists()


def test_uninstall_claude_desktop_ignores_config_without_mcp_servers(monkeypatch, tmp_path):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(json.dumps({"theme": "dark"}))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))

    installer.uninstall_claude_desktop()

    assert _read_json(config_path) == {"theme": "dark"}


def test_uninstall_claude_desktop_raises_when_platform_is_unsupported(monkeypatch):
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", None)

    with pytest.raises(NotImplementedError):
        installer.uninstall_claude_desktop()


def test_is_installed_returns_true_when_email_server_exists(monkeypatch, tmp_path):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(json.dumps({"mcpServers": {"mcp-email-server": {"command": "mcp-email-server"}}}))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))

    assert installer.is_installed() is True


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"mcpServers": {"other-server": {}}})])
def test_is_installed_returns_false_for_missing_invalid_or_unrelated_config(monkeypatch, tmp_path, content):
    config_path = tmp_path / "claude_desktop_config.json"
    if content is not None:
        config_path.write_text(content)
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))

    assert installer.is_installed() is False


@pytest.mark.parametrize("server_name", installer.LEGACY_SERVER_NAMES)
def test_is_installed_recognizes_legacy_server(monkeypatch, tmp_path, server_name):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(json.dumps({"mcpServers": {server_name: {"command": "mcp-email-server"}}}))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))

    assert installer.is_installed() is True


def test_is_installed_returns_false_when_platform_is_unsupported(monkeypatch):
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", None)

    assert installer.is_installed() is False


def test_need_update_returns_true_when_not_installed(monkeypatch):
    monkeypatch.setattr(installer, "is_installed", lambda: False)

    assert installer.need_update() is True


def test_need_update_returns_true_when_config_path_is_missing_after_install_check(monkeypatch):
    monkeypatch.setattr(installer, "is_installed", lambda: True)
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", None)

    assert installer.need_update() is True


@pytest.mark.parametrize("server_name", installer.LEGACY_SERVER_NAMES)
def test_need_update_returns_true_for_legacy_server(monkeypatch, tmp_path, server_name):
    config_path = tmp_path / "claude_desktop_config.json"
    template_path = tmp_path / "claude_desktop_config_template.json"
    _write_template(template_path)
    config_path.write_text(json.dumps({"mcpServers": {server_name: {"command": "mcp-email-server"}}}))

    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_TEMPLATE", template_path)
    monkeypatch.setattr(installer, "is_installed", lambda: True)

    assert installer.need_update() is True


def test_need_update_returns_false_when_installed_config_matches_template(monkeypatch, tmp_path):
    config_path = tmp_path / "claude_desktop_config.json"
    template_path = tmp_path / "claude_desktop_config_template.json"
    _write_template(template_path)
    config_path.write_text(
        json.dumps({
            "mcpServers": {
                "mcp-email-server": {
                    "command": "/usr/local/bin/mcp-email-server",
                    "args": ["stdio"],
                    "env": {"MCP_EMAIL_SERVER_LOG_LEVEL": "INFO"},
                }
            }
        })
    )

    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_TEMPLATE", template_path)
    monkeypatch.setattr(installer, "get_endpoint_path", lambda: "/usr/local/bin/mcp-email-server")
    monkeypatch.setattr(installer, "is_installed", lambda: True)

    assert installer.need_update() is False


@pytest.mark.parametrize(
    "installed_server",
    [
        {"command": "/old/bin/mcp-email-server", "args": ["stdio"], "env": {"MCP_EMAIL_SERVER_LOG_LEVEL": "INFO"}},
        {"command": "/usr/local/bin/mcp-email-server", "args": ["sse"], "env": {"MCP_EMAIL_SERVER_LOG_LEVEL": "INFO"}},
        {
            "command": "/usr/local/bin/mcp-email-server",
            "args": ["stdio"],
            "env": {"MCP_EMAIL_SERVER_LOG_LEVEL": "DEBUG"},
        },
    ],
)
def test_need_update_returns_true_when_installed_config_differs(monkeypatch, tmp_path, installed_server):
    config_path = tmp_path / "claude_desktop_config.json"
    template_path = tmp_path / "claude_desktop_config_template.json"
    _write_template(template_path)
    config_path.write_text(json.dumps({"mcpServers": {"mcp-email-server": installed_server}}))

    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_TEMPLATE", template_path)
    monkeypatch.setattr(installer, "get_endpoint_path", lambda: "/usr/local/bin/mcp-email-server")
    monkeypatch.setattr(installer, "is_installed", lambda: True)

    assert installer.need_update() is True


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"mcpServers": {}})])
def test_need_update_returns_true_when_config_cannot_be_compared(monkeypatch, tmp_path, content):
    config_path = tmp_path / "claude_desktop_config.json"
    template_path = tmp_path / "claude_desktop_config_template.json"
    _write_template(template_path)
    if content is not None:
        config_path.write_text(content)

    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_TEMPLATE", template_path)
    monkeypatch.setattr(installer, "get_endpoint_path", lambda: "/usr/local/bin/mcp-email-server")
    monkeypatch.setattr(installer, "is_installed", lambda: True)

    assert installer.need_update() is True


def test_get_claude_desktop_config_returns_file_content(monkeypatch, tmp_path):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text('{"mcpServers": {}}')
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", str(config_path))

    assert installer.get_claude_desktop_config() == '{"mcpServers": {}}'


def test_get_claude_desktop_config_raises_when_platform_is_unsupported(monkeypatch):
    monkeypatch.setattr(installer, "CLAUDE_DESKTOP_CONFIG_PATH", None)

    with pytest.raises(NotImplementedError):
        installer.get_claude_desktop_config()
