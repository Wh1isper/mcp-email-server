"""download_attachment save_path confinement (arbitrary-write hardening)."""

import os
from pathlib import Path

import pytest

from mcp_email_server.app import _resolve_download_path
from mcp_email_server.config import Settings


class TestResolveDownloadPath:
    def test_absolute_within_root_allowed(self, tmp_path):
        root = Path(os.path.realpath(tmp_path))
        target = root / "sub" / "file.pdf"
        assert _resolve_download_path(str(target), root) == target

    def test_relative_resolves_under_root(self, tmp_path):
        root = Path(os.path.realpath(tmp_path))
        assert _resolve_download_path("report.pdf", root) == root / "report.pdf"
        assert _resolve_download_path("nested/report.pdf", root) == root / "nested" / "report.pdf"

    def test_absolute_outside_root_rejected(self, tmp_path):
        root = Path(os.path.realpath(tmp_path)) / "downloads"
        root.mkdir()
        with pytest.raises(PermissionError, match="outside the permitted download"):
            _resolve_download_path("/etc/cron.d/evil", root)

    def test_dotdot_traversal_rejected(self, tmp_path):
        root = Path(os.path.realpath(tmp_path)) / "downloads"
        root.mkdir()
        with pytest.raises(PermissionError, match="outside the permitted download"):
            _resolve_download_path(str(root / ".." / "escape.txt"), root)

    def test_relative_dotdot_traversal_rejected(self, tmp_path):
        root = Path(os.path.realpath(tmp_path)) / "downloads"
        root.mkdir()
        with pytest.raises(PermissionError, match="outside the permitted download"):
            _resolve_download_path("../escape.txt", root)

    def test_symlink_escape_rejected(self, tmp_path):
        real = Path(os.path.realpath(tmp_path))
        root = real / "downloads"
        root.mkdir()
        outside = real / "outside"
        outside.mkdir()
        # A symlink inside root pointing outside must not become a write channel.
        (root / "link").symlink_to(outside)
        with pytest.raises(PermissionError, match="outside the permitted download"):
            _resolve_download_path(str(root / "link" / "pwned.txt"), root)

    def test_symlinked_root_itself_is_canonicalised(self, tmp_path):
        # If the root is reached via a symlink, a save under it still validates.
        real = Path(os.path.realpath(tmp_path))
        actual = real / "actual_downloads"
        actual.mkdir()
        result = _resolve_download_path(str(actual / "a.bin"), actual)
        assert result == actual / "a.bin"

    def test_root_itself_allowed(self, tmp_path):
        root = Path(os.path.realpath(tmp_path))
        assert _resolve_download_path(str(root), root) == root


class TestAttachmentDownloadRootConfig:
    def test_defaults_to_downloads(self, monkeypatch):
        monkeypatch.delenv("MCP_EMAIL_SERVER_ATTACHMENT_DOWNLOAD_DIR", raising=False)
        settings = Settings()
        assert settings.attachment_download_dir is None
        assert settings.attachment_download_root == Path(os.path.realpath(Path("~/Downloads").expanduser()))

    def test_field_sets_root(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MCP_EMAIL_SERVER_ATTACHMENT_DOWNLOAD_DIR", raising=False)
        settings = Settings()
        settings.attachment_download_dir = str(tmp_path)
        assert settings.attachment_download_root == Path(os.path.realpath(tmp_path))

    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MCP_EMAIL_SERVER_ATTACHMENT_DOWNLOAD_DIR", str(tmp_path))
        settings = Settings()
        assert settings.attachment_download_dir == str(tmp_path)
        assert settings.attachment_download_root == Path(os.path.realpath(tmp_path))

    def test_env_empty_resets_to_default(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_ATTACHMENT_DOWNLOAD_DIR", "   ")
        settings = Settings()
        assert settings.attachment_download_dir is None
        assert settings.attachment_download_root == Path(os.path.realpath(Path("~/Downloads").expanduser()))


class TestDownloadAttachmentTool:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self, monkeypatch):
        from unittest.mock import AsyncMock, patch

        from mcp_email_server.app import download_attachment

        settings = Settings()
        settings.enable_attachment_download = False
        handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=handler),
        ):
            with pytest.raises(PermissionError, match="disabled"):
                await download_attachment(account_name="a", email_id="1", attachment_name="x.pdf", save_path="x.pdf")
        handler.download_attachment.assert_not_called()

    @pytest.mark.asyncio
    async def test_escaping_path_rejected_before_handler(self, monkeypatch, tmp_path):
        from unittest.mock import AsyncMock, patch

        from mcp_email_server.app import download_attachment

        settings = Settings()
        settings.enable_attachment_download = True
        settings.attachment_download_dir = str(tmp_path)
        handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=handler),
        ):
            with pytest.raises(PermissionError, match="outside the permitted download"):
                await download_attachment(
                    account_name="a",
                    email_id="1",
                    attachment_name="x.pdf",
                    save_path="/etc/cron.d/evil",
                )
        handler.download_attachment.assert_not_called()

    @pytest.mark.asyncio
    async def test_confined_path_passed_to_handler(self, monkeypatch, tmp_path):
        from unittest.mock import AsyncMock, patch

        from mcp_email_server.app import download_attachment

        settings = Settings()
        settings.enable_attachment_download = True
        settings.attachment_download_dir = str(tmp_path)
        handler = AsyncMock()
        handler.download_attachment.return_value = None
        with (
            patch("mcp_email_server.app.get_settings", return_value=settings),
            patch("mcp_email_server.app.dispatch_handler", return_value=handler),
        ):
            await download_attachment(
                account_name="a", email_id="1", attachment_name="x.pdf", save_path="reports/x.pdf"
            )
        passed = handler.download_attachment.call_args.args[2]
        assert passed == str(Path(os.path.realpath(tmp_path)) / "reports" / "x.pdf")
