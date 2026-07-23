"""Embedded loopback-only React management adapter."""

from mcp_email_server.web_ui.app import LocalUiState, create_local_ui_app
from mcp_email_server.web_ui.server import run_local_ui

__all__ = ["LocalUiState", "create_local_ui_app", "run_local_ui"]
