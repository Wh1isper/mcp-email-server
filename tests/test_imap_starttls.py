import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import EmailClient, _create_starttls_ssl_context, _imap_starttls


class Response:
    def __init__(self, result: str, lines: list[bytes] | None = None):
        self.result = result
        self.lines = lines or []


def _make_imap(capabilities=("IMAP4rev1", "STARTTLS")):
    imap = AsyncMock()
    imap._client_task = asyncio.Future()
    imap._client_task.set_result(None)
    imap.wait_hello_from_server = AsyncMock()
    imap.protocol = MagicMock()
    imap.protocol.capabilities = set(capabilities)
    imap.protocol.loop = asyncio.get_event_loop()
    imap.protocol.new_tag.return_value = "A001"
    imap.protocol.execute = AsyncMock(return_value=Response("OK"))
    imap.protocol.capability = AsyncMock()
    imap.protocol.transport = MagicMock()
    return imap


def test_email_settings_init_accepts_imap_start_ssl():
    settings = EmailSettings.init(
        account_name="test",
        full_name="Test User",
        email_address="test@example.com",
        user_name="test@example.com",
        password="secret",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_ssl=False,
        imap_start_ssl=True,
        smtp_host="127.0.0.1",
    )

    assert settings.incoming.use_ssl is False
    assert settings.incoming.start_ssl is True


def test_email_settings_from_env_accepts_imap_start_ssl(monkeypatch):
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_PORT", "1143")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SSL", "false")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_START_SSL", "true")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "127.0.0.1")

    settings = EmailSettings.from_env()

    assert settings is not None
    assert settings.incoming.use_ssl is False
    assert settings.incoming.start_ssl is True


def test_create_starttls_ssl_context_verify_false_is_permissive():
    ctx = _create_starttls_ssl_context(False)

    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.asyncio
async def test_imap_starttls_upgrades_transport_and_refreshes_capabilities():
    imap = _make_imap()
    tls_transport = MagicMock()

    with patch("asyncio.get_running_loop") as mock_get_loop:
        mock_loop = MagicMock()
        mock_loop.start_tls = AsyncMock(return_value=tls_transport)
        mock_get_loop.return_value = mock_loop

        await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")

    imap.protocol.execute.assert_awaited_once()
    mock_loop.start_tls.assert_awaited_once()
    assert imap.protocol.transport is tls_transport
    imap.protocol.capability.assert_awaited_once()


@pytest.mark.asyncio
async def test_imap_starttls_requires_server_capability():
    imap = _make_imap(capabilities=("IMAP4rev1",))

    with pytest.raises(OSError, match="does not advertise STARTTLS"):
        await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")


@pytest.mark.asyncio
async def test_imap_starttls_raises_when_command_fails():
    imap = _make_imap()
    imap.protocol.execute = AsyncMock(return_value=Response("NO"))

    with pytest.raises(OSError, match="STARTTLS command failed: NO"):
        await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")


@pytest.mark.asyncio
async def test_connect_imap_uses_implicit_tls_when_use_ssl_true():
    server = EmailServer(
        user_name="user",
        password="secret",
        host="imap.example.com",
        port=993,
        use_ssl=True,
        verify_ssl=False,
    )
    mock_imap = _make_imap()

    with patch("mcp_email_server.emails.classic.aioimaplib.IMAP4_SSL", return_value=mock_imap) as mock_ssl:
        result = await EmailClient._connect_imap_server(server)

    assert result is mock_imap
    mock_ssl.assert_called_once()
    assert mock_ssl.call_args.kwargs["ssl_context"].verify_mode == ssl.CERT_NONE
    mock_imap.wait_hello_from_server.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_imap_uses_plain_connection_without_starttls():
    server = EmailServer(
        user_name="user",
        password="secret",
        host="imap.example.com",
        port=143,
        use_ssl=False,
        start_ssl=False,
    )
    mock_imap = _make_imap()

    with patch("mcp_email_server.emails.classic.aioimaplib.IMAP4", return_value=mock_imap) as mock_plain:
        with patch("mcp_email_server.emails.classic._imap_starttls", new=AsyncMock()) as mock_starttls:
            result = await EmailClient._connect_imap_server(server)

    assert result is mock_imap
    mock_plain.assert_called_once_with("imap.example.com", 143)
    mock_starttls.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_imap_uses_starttls_when_configured():
    server = EmailServer(
        user_name="user",
        password="secret",
        host="127.0.0.1",
        port=1143,
        use_ssl=False,
        start_ssl=True,
        verify_ssl=False,
    )
    mock_imap = _make_imap()

    with patch("mcp_email_server.emails.classic.aioimaplib.IMAP4", return_value=mock_imap) as mock_plain:
        with patch("mcp_email_server.emails.classic._imap_starttls", new=AsyncMock()) as mock_starttls:
            result = await EmailClient._connect_imap_server(server)

    assert result is mock_imap
    mock_plain.assert_called_once_with("127.0.0.1", 1143)
    mock_starttls.assert_awaited_once()
    starttls_context = mock_starttls.await_args.args[1]
    assert starttls_context.verify_mode == ssl.CERT_NONE


@pytest.mark.asyncio
async def test_append_to_mailbox_uses_incoming_starttls_path():
    outgoing = EmailServer(user_name="user", password="secret", host="smtp.example.com", port=465)
    incoming = EmailServer(
        user_name="user",
        password="secret",
        host="127.0.0.1",
        port=1143,
        use_ssl=False,
        start_ssl=True,
        verify_ssl=False,
    )
    client = EmailClient(outgoing)
    mock_imap = _make_imap()
    mock_imap.login = AsyncMock(return_value=Response("OK"))
    mock_imap.select = AsyncMock(return_value=("OK", []))
    mock_imap.append = AsyncMock(return_value=("OK", []))
    mock_imap.logout = AsyncMock()

    with patch("mcp_email_server.emails.classic.aioimaplib.IMAP4", return_value=mock_imap):
        with patch("mcp_email_server.emails.classic._imap_starttls", new=AsyncMock()) as mock_starttls:
            result = await client.append_to_mailbox(MagicMock(as_bytes=lambda: b"message"), incoming, "Drafts")

    assert result == "unknown"
    mock_starttls.assert_awaited_once()
    mock_imap.append.assert_awaited_once()
