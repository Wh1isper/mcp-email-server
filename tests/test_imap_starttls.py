"""Tests for IMAP STARTTLS support and ConnectionSecurity enum."""

from __future__ import annotations

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import ConnectionSecurity, EmailServer, EmailSettings


class TestConnectionSecurity:
    """Tests for the ConnectionSecurity enum and EmailServer model."""

    def test_default_security_is_tls(self):
        server = EmailServer(user_name="u", password="p", host="h", port=993)
        assert server.security == ConnectionSecurity.TLS

    def test_explicit_security_tls(self):
        server = EmailServer(user_name="u", password="p", host="h", port=993, security="tls")
        assert server.security == ConnectionSecurity.TLS

    def test_explicit_security_starttls(self):
        server = EmailServer(user_name="u", password="p", host="h", port=143, security="starttls")
        assert server.security == ConnectionSecurity.STARTTLS

    def test_explicit_security_none(self):
        server = EmailServer(user_name="u", password="p", host="h", port=143, security="none")
        assert server.security == ConnectionSecurity.NONE

    def test_legacy_use_ssl_true(self):
        server = EmailServer(user_name="u", password="p", host="h", port=993, use_ssl=True)
        assert server.security == ConnectionSecurity.TLS

    def test_legacy_use_ssl_false_start_ssl_true(self):
        server = EmailServer(user_name="u", password="p", host="h", port=143, use_ssl=False, start_ssl=True)
        assert server.security == ConnectionSecurity.STARTTLS

    def test_legacy_use_ssl_false_start_ssl_false(self):
        server = EmailServer(user_name="u", password="p", host="h", port=143, use_ssl=False, start_ssl=False)
        assert server.security == ConnectionSecurity.NONE

    def test_legacy_use_ssl_false_only(self):
        server = EmailServer(user_name="u", password="p", host="h", port=143, use_ssl=False)
        assert server.security == ConnectionSecurity.NONE

    def test_legacy_start_ssl_true_only(self):
        server = EmailServer(user_name="u", password="p", host="h", port=143, start_ssl=True)
        # use_ssl defaults to None (not set), start_ssl=True → STARTTLS
        assert server.security == ConnectionSecurity.STARTTLS

    def test_invalid_use_ssl_and_start_ssl_both_true(self):
        with pytest.raises(ValueError, match="cannot both be true"):
            EmailServer(user_name="u", password="p", host="h", port=993, use_ssl=True, start_ssl=True)

    def test_security_enum_values(self):
        assert ConnectionSecurity.TLS.value == "tls"
        assert ConnectionSecurity.STARTTLS.value == "starttls"
        assert ConnectionSecurity.NONE.value == "none"

    def test_verify_ssl_default_true(self):
        server = EmailServer(user_name="u", password="p", host="h", port=993)
        assert server.verify_ssl is True

    def test_verify_ssl_false(self):
        server = EmailServer(user_name="u", password="p", host="h", port=993, verify_ssl=False)
        assert server.verify_ssl is False

    def test_masked_preserves_security(self):
        server = EmailServer(user_name="u", password="secret", host="h", port=143, security="starttls")
        masked = server.masked()
        assert masked.security == ConnectionSecurity.STARTTLS
        assert masked.password == "*" * 8


class TestEmailSettingsInit:
    """Tests for EmailSettings.init() with new security parameters."""

    def test_init_with_security_params(self):
        settings = EmailSettings.init(
            account_name="test",
            full_name="Test",
            email_address="test@example.com",
            user_name="test",
            password="pass",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            imap_port=143,
            imap_security=ConnectionSecurity.STARTTLS,
            smtp_port=587,
            smtp_security=ConnectionSecurity.STARTTLS,
        )
        assert settings.incoming.security == ConnectionSecurity.STARTTLS
        assert settings.outgoing.security == ConnectionSecurity.STARTTLS

    def test_init_with_legacy_params(self):
        settings = EmailSettings.init(
            account_name="test",
            full_name="Test",
            email_address="test@example.com",
            user_name="test",
            password="pass",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            imap_ssl=False,
            smtp_ssl=False,
            smtp_start_ssl=True,
        )
        # imap_ssl=False → use_ssl=False → security=NONE (no start_ssl for IMAP in legacy)
        assert settings.incoming.security == ConnectionSecurity.NONE
        # smtp_ssl=False + smtp_start_ssl=True → STARTTLS
        assert settings.outgoing.security == ConnectionSecurity.STARTTLS

    def test_init_default_is_tls(self):
        settings = EmailSettings.init(
            account_name="test",
            full_name="Test",
            email_address="test@example.com",
            user_name="test",
            password="pass",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
        )
        assert settings.incoming.security == ConnectionSecurity.TLS
        assert settings.outgoing.security == ConnectionSecurity.TLS

    def test_init_with_verify_ssl(self):
        settings = EmailSettings.init(
            account_name="test",
            full_name="Test",
            email_address="test@example.com",
            user_name="test",
            password="pass",
            imap_host="localhost",
            smtp_host="localhost",
            imap_verify_ssl=False,
            smtp_verify_ssl=False,
        )
        assert settings.incoming.verify_ssl is False
        assert settings.outgoing.verify_ssl is False


class TestEmailSettingsFromEnv:
    """Tests for EmailSettings.from_env() with new security env vars."""

    def test_from_env_with_security_vars(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SECURITY", "starttls")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_SECURITY", "starttls")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_PORT", "143")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_PORT", "587")

        settings = EmailSettings.from_env()
        assert settings is not None
        assert settings.incoming.security == ConnectionSecurity.STARTTLS
        assert settings.outgoing.security == ConnectionSecurity.STARTTLS

    def test_from_env_with_legacy_ssl_vars(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SSL", "false")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_SSL", "false")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_START_SSL", "true")

        settings = EmailSettings.from_env()
        assert settings is not None
        assert settings.incoming.security == ConnectionSecurity.NONE
        assert settings.outgoing.security == ConnectionSecurity.STARTTLS

    def test_from_env_security_takes_precedence_over_legacy(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.example.com")
        # New env var takes precedence
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SECURITY", "starttls")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SSL", "true")  # Should be ignored

        settings = EmailSettings.from_env()
        assert settings is not None
        assert settings.incoming.security == ConnectionSecurity.STARTTLS

    def test_from_env_with_imap_verify_ssl(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "localhost")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "localhost")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_VERIFY_SSL", "false")

        settings = EmailSettings.from_env()
        assert settings is not None
        assert settings.incoming.verify_ssl is False

    def test_from_env_invalid_security_value_uses_default(self, monkeypatch):
        monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "pass")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SECURITY", "invalid_value")

        settings = EmailSettings.from_env()
        assert settings is not None
        assert settings.incoming.security == ConnectionSecurity.TLS  # Falls back to default


class TestImapConnection:
    """Tests for IMAP connection creation with different security modes."""

    @pytest.mark.asyncio
    async def test_create_imap_connection_tls(self):
        server = EmailServer(user_name="u", password="p", host="localhost", port=993, security="tls")

        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib:
            mock_imap = AsyncMock()
            mock_imap._client_task = asyncio.Future()
            mock_imap._client_task.set_result(None)
            mock_imap.wait_hello_from_server = AsyncMock()
            mock_aioimaplib.IMAP4_SSL.return_value = mock_imap

            from mcp_email_server.emails.classic import _create_imap_connection

            result = await _create_imap_connection(server)

            mock_aioimaplib.IMAP4_SSL.assert_called_once()
            assert result is mock_imap

    @pytest.mark.asyncio
    async def test_create_imap_connection_none(self):
        server = EmailServer(user_name="u", password="p", host="localhost", port=143, security="none")

        with patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib:
            mock_imap = AsyncMock()
            mock_imap._client_task = asyncio.Future()
            mock_imap._client_task.set_result(None)
            mock_imap.wait_hello_from_server = AsyncMock()
            mock_aioimaplib.IMAP4.return_value = mock_imap

            from mcp_email_server.emails.classic import _create_imap_connection

            result = await _create_imap_connection(server)

            mock_aioimaplib.IMAP4.assert_called_once_with("localhost", 143)
            assert result is mock_imap

    @pytest.mark.asyncio
    async def test_create_imap_connection_starttls(self):
        server = EmailServer(user_name="u", password="p", host="localhost", port=143, security="starttls")

        with (
            patch("mcp_email_server.emails.classic.aioimaplib") as mock_aioimaplib,
            patch("mcp_email_server.emails.classic._imap_starttls") as mock_starttls,
        ):
            mock_imap = AsyncMock()
            mock_imap._client_task = asyncio.Future()
            mock_imap._client_task.set_result(None)
            mock_imap.wait_hello_from_server = AsyncMock()
            mock_aioimaplib.IMAP4.return_value = mock_imap

            from mcp_email_server.emails.classic import _create_imap_connection

            result = await _create_imap_connection(server)

            mock_aioimaplib.IMAP4.assert_called_once_with("localhost", 143)
            mock_starttls.assert_called_once()
            assert result is mock_imap


class TestImapStarttls:
    """Tests for the _imap_starttls function."""

    @pytest.mark.asyncio
    async def test_starttls_succeeds(self):
        from mcp_email_server.emails.classic import _imap_starttls

        mock_imap = MagicMock()
        mock_protocol = MagicMock()
        mock_protocol.capabilities = {"STARTTLS", "IMAP4rev1"}
        mock_protocol.new_tag.return_value = "A001"
        mock_protocol.loop = asyncio.get_event_loop()
        mock_protocol.transport = MagicMock()

        # Mock execute to return OK
        mock_response = MagicMock()
        mock_response.result = "OK"
        mock_protocol.execute = AsyncMock(return_value=mock_response)
        mock_protocol.capability = AsyncMock()

        mock_imap.protocol = mock_protocol

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        mock_tls_transport = MagicMock()

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.start_tls = AsyncMock(return_value=mock_tls_transport)

            await _imap_starttls(mock_imap, ssl_ctx, "localhost")

            # Verify STARTTLS command was sent
            mock_protocol.execute.assert_called_once()
            cmd = mock_protocol.execute.call_args[0][0]
            assert cmd.name == "STARTTLS"

            # Verify transport was upgraded
            mock_loop.return_value.start_tls.assert_called_once()

            # Verify capabilities were re-fetched
            mock_protocol.capability.assert_called_once()

            # Verify transport was replaced
            assert mock_imap.protocol.transport is mock_tls_transport

    @pytest.mark.asyncio
    async def test_starttls_no_capability_raises(self):
        from mcp_email_server.emails.classic import _imap_starttls

        mock_imap = MagicMock()
        mock_imap.protocol.capabilities = {"IMAP4rev1"}  # No STARTTLS

        ssl_ctx = ssl.create_default_context()

        with pytest.raises(OSError, match="does not advertise STARTTLS"):
            await _imap_starttls(mock_imap, ssl_ctx, "localhost")

    @pytest.mark.asyncio
    async def test_starttls_command_fails_raises(self):
        from mcp_email_server.emails.classic import _imap_starttls

        mock_imap = MagicMock()
        mock_protocol = MagicMock()
        mock_protocol.capabilities = {"STARTTLS", "IMAP4rev1"}
        mock_protocol.new_tag.return_value = "A001"
        mock_protocol.loop = asyncio.get_event_loop()

        mock_response = MagicMock()
        mock_response.result = "NO"
        mock_protocol.execute = AsyncMock(return_value=mock_response)
        mock_imap.protocol = mock_protocol

        ssl_ctx = ssl.create_default_context()

        with pytest.raises(OSError, match="STARTTLS command failed"):
            await _imap_starttls(mock_imap, ssl_ctx, "localhost")


class TestEmailClientSecurity:
    """Tests for EmailClient with different security modes."""

    def test_client_tls_smtp_settings(self):
        from mcp_email_server.emails.classic import EmailClient

        server = EmailServer(user_name="u", password="p", host="h", port=993, security="tls")
        client = EmailClient(server)
        assert client.smtp_use_tls is True
        assert client.smtp_start_tls is False

    def test_client_starttls_smtp_settings(self):
        from mcp_email_server.emails.classic import EmailClient

        server = EmailServer(user_name="u", password="p", host="h", port=587, security="starttls")
        client = EmailClient(server)
        assert client.smtp_use_tls is False
        assert client.smtp_start_tls is True

    def test_client_none_smtp_settings(self):
        from mcp_email_server.emails.classic import EmailClient

        server = EmailServer(user_name="u", password="p", host="h", port=25, security="none")
        client = EmailClient(server)
        assert client.smtp_use_tls is False
        assert client.smtp_start_tls is False

    def test_client_legacy_compat_smtp_settings(self):
        from mcp_email_server.emails.classic import EmailClient

        server = EmailServer(user_name="u", password="p", host="h", port=587, use_ssl=False, start_ssl=True)
        client = EmailClient(server)
        assert client.smtp_use_tls is False
        assert client.smtp_start_tls is True


class TestImapSslContext:
    """Tests for IMAP SSL context creation."""

    def test_create_imap_ssl_context_verified(self):
        from mcp_email_server.emails.classic import _create_imap_ssl_context

        ctx = _create_imap_ssl_context(verify_ssl=True)
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_create_imap_ssl_context_unverified(self):
        from mcp_email_server.emails.classic import _create_imap_ssl_context

        ctx = _create_imap_ssl_context(verify_ssl=False)
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False


class TestTomlBackwardCompat:
    """Tests for backward compatibility with existing TOML configs."""

    def test_legacy_toml_format_incoming(self):
        """Simulate loading a config with old use_ssl/start_ssl fields."""
        server = EmailServer.model_validate({
            "user_name": "user",
            "password": "pass",
            "host": "127.0.0.1",
            "port": 1143,
            "use_ssl": False,
            "start_ssl": True,
        })
        assert server.security == ConnectionSecurity.STARTTLS

    def test_new_toml_format(self):
        """Simulate loading a config with new security field."""
        server = EmailServer.model_validate({
            "user_name": "user",
            "password": "pass",
            "host": "127.0.0.1",
            "port": 1143,
            "security": "starttls",
            "verify_ssl": False,
        })
        assert server.security == ConnectionSecurity.STARTTLS
        assert server.verify_ssl is False

    def test_legacy_format_without_explicit_start_ssl(self):
        """Old configs without start_ssl should default to TLS."""
        server = EmailServer.model_validate({
            "user_name": "user",
            "password": "pass",
            "host": "imap.gmail.com",
            "port": 993,
            "use_ssl": True,
        })
        assert server.security == ConnectionSecurity.TLS
