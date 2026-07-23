"""Compatibility entry point for the embedded local management UI.

The legacy Gradio implementation was intentionally removed. New code should
import :func:`mcp_email_server.web_ui.run_local_ui` directly.
"""

from mcp_email_server.web_ui import run_local_ui


def main() -> None:
    """Run the loopback-only UI with its secure default lifecycle."""

    run_local_ui()
