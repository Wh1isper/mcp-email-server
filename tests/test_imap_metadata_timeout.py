from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import aioimaplib
import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient


class RecordingTransport(asyncio.WriteTransport):
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def abort(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


def client_and_protocol(timeout: float):
    client = EmailClient(
        EmailServer(user_name="user", password="secret", host="example.test", port=143, use_ssl=False),
        sender="user@example.test",
    )
    protocol = aioimaplib.IMAP4ClientProtocol(asyncio.get_running_loop())
    protocol.state = aioimaplib.SELECTED
    transport = RecordingTransport()
    protocol.transport = transport
    imap = object.__new__(aioimaplib.IMAP4)
    imap.protocol = protocol
    imap.timeout = timeout
    imap.select = AsyncMock(return_value=("OK", []))
    return client, imap, protocol, transport


@pytest.mark.asyncio
async def test_search_timeout_discards_real_protocol_without_logout():
    client, imap, protocol, transport = client_and_protocol(0.02)
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch("mcp_email_server.emails.classic._imap_login", AsyncMock()),
        patch("mcp_email_server.emails.classic._send_imap_id", AsyncMock()),
        pytest.raises(TimeoutError),
    ):
        await client.get_emails_metadata()
    assert len(transport.writes) == 1
    assert b"SEARCH" in transport.writes[0]
    assert transport.closed
    assert not protocol.pending_async_commands


@pytest.mark.asyncio
async def test_logout_timeout_releases_transport_after_successful_search():
    client, imap, protocol, transport = client_and_protocol(0.03)
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch("mcp_email_server.emails.classic._imap_login", AsyncMock()),
        patch("mcp_email_server.emails.classic._send_imap_id", AsyncMock()),
    ):
        task = asyncio.create_task(client.get_emails_metadata())
        while not transport.writes:
            await asyncio.sleep(0)
        tag = transport.writes[0].split()[0]
        protocol.data_received(b"* SEARCH\r\n" + tag + b" OK SEARCH completed\r\n")
        assert await task == (0, [])
    assert any(b"LOGOUT" in data for data in transport.writes)
    assert transport.closed
    assert protocol.pending_sync_command is None


@pytest.mark.asyncio
async def test_cancelled_search_closes_connection_without_logout():
    client, imap, protocol, transport = client_and_protocol(1)
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch("mcp_email_server.emails.classic._imap_login", AsyncMock()),
        patch("mcp_email_server.emails.classic._send_imap_id", AsyncMock()),
    ):
        task = asyncio.create_task(client.get_emails_metadata())
        while not transport.writes:
            await asyncio.sleep(0)
        command = protocol.pending_async_commands["SEARCH"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert transport.closed
    assert len(transport.writes) == 1
    assert command._timer.cancelled()
    assert not protocol.pending_async_commands


@pytest.mark.asyncio
async def test_search_within_budget_and_normal_logout():
    client, imap, protocol, transport = client_and_protocol(0.5)
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch("mcp_email_server.emails.classic._imap_login", AsyncMock()),
        patch("mcp_email_server.emails.classic._send_imap_id", AsyncMock()),
    ):
        task = asyncio.create_task(client.get_emails_metadata())
        while not transport.writes:
            await asyncio.sleep(0)
        await asyncio.sleep(0.03)
        tag = transport.writes[0].split()[0]
        protocol.data_received(b"* SEARCH\r\n" + tag + b" OK SEARCH completed\r\n")
        while len(transport.writes) < 2:
            await asyncio.sleep(0)
        tag = transport.writes[1].split()[0]
        protocol.data_received(b"* BYE logging out\r\n" + tag + b" OK LOGOUT completed\r\n")
        assert await task == (0, [])
    assert transport.closed
    assert b"LOGOUT" in transport.writes[1]


@pytest.mark.asyncio
async def test_refresh_connection_receives_configured_budget_over_60(monkeypatch):
    client, imap, _protocol, _transport = client_and_protocol(1)
    monkeypatch.setenv("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS", "120")
    connect = AsyncMock(return_value=imap)
    with (
        patch.object(client, "_connect_imap", connect),
        patch("mcp_email_server.emails.classic._imap_login", AsyncMock(side_effect=TimeoutError())),
        pytest.raises(TimeoutError),
    ):
        await client.get_mailbox_metadata_snapshot()
    connect.assert_awaited_once_with(timeout=120)


@pytest.mark.asyncio
async def test_adapter_converts_command_timeout_to_application_timeout():
    from mcp_email_server.adapters.metadata import _bounded_provider_call
    from mcp_email_server.application.metadata import MetadataProviderError
    from mcp_email_server.application.metadata import _bounded_provider_call as application_call

    command = aioimaplib.Command("FETCH", "A1", loop=asyncio.get_running_loop(), timeout=1)
    command.close(b"test", "KO")

    async def timed_out():
        raise aioimaplib.CommandTimeout(command)

    with pytest.raises(MetadataProviderError, match="provider request timed out") as error:
        await application_call(_bounded_provider_call(timed_out()))
    assert isinstance(error.value.diagnostic, TimeoutError)
    assert "FETCH" not in str(error.value)


@pytest.mark.asyncio
async def test_cancelled_flags_fetch_releases_partial_literal_and_timer():
    client, imap, protocol, transport = client_and_protocol(120)
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch("mcp_email_server.emails.classic._imap_login", AsyncMock()),
        patch("mcp_email_server.emails.classic._send_imap_id", AsyncMock()),
    ):
        task = asyncio.create_task(client.get_email_flags(["1"]))
        while not transport.writes:
            await asyncio.sleep(0)
        protocol.data_received(b"* 1 FETCH (UID 1 BODY[] {50000}\r\npartial")
        command = protocol.current_command
        assert command is not None
        assert not command._timer.cancelled()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
    assert transport.closed
    assert len(transport.writes) == 1
    assert command._timer.cancelled()
    assert protocol.current_command is None
    assert not protocol.pending_async_commands
