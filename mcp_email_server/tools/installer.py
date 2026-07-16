import json
import os
import platform
import shutil
import sys
from pathlib import Path

from jinja2 import Template

_HERE = Path(__file__).parent
CLAUDE_DESKTOP_CONFIG_TEMPLATE = _HERE / "claude_desktop_config.json"
SERVER_NAME = "mcp-email-server"
LEGACY_SERVER_NAMES = ("mcp_email_server", "zerolib-email")

system = platform.system()
if system == "Darwin":
    CLAUDE_DESKTOP_CONFIG_PATH = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
elif system == "Windows":
    CLAUDE_DESKTOP_CONFIG_PATH = os.path.join(os.environ["APPDATA"], "Claude", "claude_desktop_config.json")
else:
    CLAUDE_DESKTOP_CONFIG_PATH = None


def get_endpoint_path() -> str:
    """
    Find the path to the mcp-email-server script.
    Similar to the 'which' command in Unix-like systems.

    Returns:
        str: The full path to the mcp-email-server script
    """
    # First try using shutil.which to find the script in PATH
    script_path = shutil.which("mcp-email-server")
    if script_path:
        return script_path

    # If not found in PATH, try to find it in the current Python environment
    # This handles cases where the script is installed but not in PATH
    bin_dir = Path(sys.executable).parent
    possible_paths = [
        bin_dir / "mcp-email-server",
        bin_dir / "mcp-email-server.exe",  # For Windows
    ]

    for path in possible_paths:
        if path.exists():
            return str(path)

    # If we can't find it, return the script name and hope it's in PATH when executed
    return "mcp-email-server"


def install_claude_desktop():
    # Read the template config
    template_content = CLAUDE_DESKTOP_CONFIG_TEMPLATE.read_text()
    rendered_content = Template(template_content).render(ENTRYPOINT=get_endpoint_path())
    template_config = json.loads(rendered_content)
    if not CLAUDE_DESKTOP_CONFIG_PATH:
        raise NotImplementedError

    # Read the existing config file or create an empty JSON object
    try:
        with open(CLAUDE_DESKTOP_CONFIG_PATH) as f:
            existing_config = json.load(f)
    except FileNotFoundError:
        existing_config = {}

    # Merge the template config into the existing config
    if "mcpServers" not in existing_config:
        existing_config["mcpServers"] = {}
    for server_name in LEGACY_SERVER_NAMES:
        existing_config["mcpServers"].pop(server_name, None)
    existing_config["mcpServers"].update(template_config["mcpServers"])

    # Write the merged config back to the Claude config file
    os.makedirs(os.path.dirname(CLAUDE_DESKTOP_CONFIG_PATH), exist_ok=True)
    with open(CLAUDE_DESKTOP_CONFIG_PATH, "w") as f:
        json.dump(existing_config, f, indent=4)


def uninstall_claude_desktop():
    if not CLAUDE_DESKTOP_CONFIG_PATH:
        raise NotImplementedError
    try:
        with open(CLAUDE_DESKTOP_CONFIG_PATH) as f:
            existing_config = json.load(f)
    except FileNotFoundError:
        return

    if "mcpServers" not in existing_config:
        return

    for server_name in (SERVER_NAME, *LEGACY_SERVER_NAMES):
        existing_config["mcpServers"].pop(server_name, None)

    with open(CLAUDE_DESKTOP_CONFIG_PATH, "w") as f:
        json.dump(existing_config, f, indent=4)


def is_installed() -> bool:
    """
    Check if the MCP email server is installed in the Claude desktop configuration.

    Returns:
        bool: True if installed, False otherwise
    """
    if not CLAUDE_DESKTOP_CONFIG_PATH:
        return False

    try:
        with open(CLAUDE_DESKTOP_CONFIG_PATH) as f:
            config = json.load(f)

        return "mcpServers" in config and any(
            server_name in config["mcpServers"] for server_name in (SERVER_NAME, *LEGACY_SERVER_NAMES)
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def need_update() -> bool:
    """
    Check if the installed configuration needs to be updated.

    Returns:
        bool: True if an update is needed, False otherwise
    """
    if not is_installed():
        return True

    config_path = CLAUDE_DESKTOP_CONFIG_PATH
    if config_path is None:
        return True

    try:
        # Get the template config
        template_content = CLAUDE_DESKTOP_CONFIG_TEMPLATE.read_text()
        rendered_content = Template(template_content).render(ENTRYPOINT=get_endpoint_path())
        template_config = json.loads(rendered_content)

        # Get the installed config
        with open(config_path) as f:
            installed_config = json.load(f)

        # Compare the relevant parts of the configs
        if any(server_name in installed_config["mcpServers"] for server_name in LEGACY_SERVER_NAMES):
            return True

        template_server = template_config["mcpServers"][SERVER_NAME]
        installed_server = installed_config["mcpServers"][SERVER_NAME]

        # Check if any key configuration elements differ
        return (
            template_server.get("command") != installed_server.get("command")
            or template_server.get("args") != installed_server.get("args")
            or template_server.get("env") != installed_server.get("env")
        )
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        # If any error occurs during comparison, suggest an update
        return True


def get_claude_desktop_config() -> str:
    if not CLAUDE_DESKTOP_CONFIG_PATH:
        raise NotImplementedError

    with open(CLAUDE_DESKTOP_CONFIG_PATH) as f:
        return f.read()
