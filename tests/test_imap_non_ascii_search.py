from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aioimaplib
import pytest
from pydantic import SecretStr, ValidationError

from mcp_email_server.config import (
    EmailServer,
    EmailSettings,
    imap_non_ascii_search_mode,
    resolve_imap_non_ascii_search,
)
from mcp_email_server.emails.classic import (
    EmailClient,
    _ImapSearchLiteral,
    _quote_imap_search_text,
    _uid_search,
)


class _RecordingTransport(asyncio.WriteTransport):
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
        self.writes.append(bytes(data))

    def abort(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _client(host: str, *, mode: str | None = None) -> EmailClient:
    return EmailClient(
        EmailServer(
            user_name="user@example.test",
            password=SecretStr("secret"),
            host=host,
            port=993,
        ),
        sender="User <user@example.test>",
        imap_non_ascii_search=mode,  # type: ignore[arg-type]
    )


def _connected_imap() -> AsyncMock:
    imap = AsyncMock()
    imap.protocol = None
    imap._client_task = asyncio.Future()
    imap._client_task.set_result(None)
    imap.wait_hello_from_server = AsyncMock()
    imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    imap.select = AsyncMock(return_value=("OK", []))
    imap.uid_search = AsyncMock(return_value=("OK", [b""]))
    imap.logout = AsyncMock()
    return imap


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("淘宝", '"淘宝"'),
        ('say"淘宝', '"say\\"淘宝"'),
        ("C:\\路径", '"C:\\\\路径"'),
        ('a"b\\淘', '"a\\"b\\\\淘"'),
    ],
)
def test_quoted_non_ascii_search_escapes_quotes_and_backslashes(value: str, expected: str) -> None:
    encoded = EmailClient._sanitize_imap_value(value, non_ascii="quoted")

    assert encoded == expected
    assert encoded == _quote_imap_search_text(value)
    assert isinstance(encoded, str)


@pytest.mark.parametrize("value", ["bad\rvalue", "bad\nvalue", "bad\r\nvalue"])
def test_quoted_search_rejects_cr_and_lf(value: str) -> None:
    with pytest.raises(ValueError, match="CR or LF"):
        EmailClient._sanitize_imap_value(value, non_ascii="quoted")


def test_quoted_search_still_rejects_other_controls() -> None:
    with pytest.raises(ValueError, match="control characters"):
        EmailClient._sanitize_imap_value("bad\x00value", non_ascii="quoted")


def test_literal_mode_keeps_synchronizing_literal_and_control_error() -> None:
    literal = EmailClient._sanitize_imap_value("淘宝", non_ascii="literal")

    assert isinstance(literal, _ImapSearchLiteral)
    assert literal.data == "淘宝".encode()
    with pytest.raises(ValueError, match="control characters"):
        EmailClient._sanitize_imap_value("bad\r\nvalue", non_ascii="literal")


@pytest.mark.parametrize(
    ("host", "configured", "env", "expected"),
    [
        ("imap.qq.com", None, None, "quoted"),
        ("IMAP.QQ.COM.", None, None, "quoted"),
        (" imap.qq.com ", None, None, "quoted"),
        ("imap.example.com", None, None, "literal"),
        ("imap.exmail.qq.com", None, None, "literal"),
        ("imap.qq.com", None, "literal", "literal"),
        ("imap.example.com", None, "quoted", "quoted"),
        ("imap.qq.com", "literal", "quoted", "literal"),
        ("imap.example.com", "quoted", "literal", "quoted"),
        ("imap.example.com", "auto", "quoted", "literal"),
        ("imap.qq.com", "auto", "literal", "quoted"),
    ],
)
def test_non_ascii_search_mode_selection(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    configured: str | None,
    env: str | None,
    expected: str,
) -> None:
    monkeypatch.delenv("MCP_EMAIL_SERVER_IMAP_NON_ASCII_SEARCH", raising=False)
    if env is not None:
        monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_NON_ASCII_SEARCH", env)

    assert resolve_imap_non_ascii_search(host, configured) == expected  # type: ignore[arg-type]


def test_invalid_non_ascii_search_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_NON_ASCII_SEARCH", "raw")

    with pytest.raises(ValueError, match=r"one of: literal, quoted, auto"):
        imap_non_ascii_search_mode()
    with pytest.raises(ValidationError):
        EmailSettings(
            account_name="work",
            full_name="Work",
            email_address="user@example.test",
            imap_non_ascii_search="raw",  # type: ignore[arg-type]
            incoming=EmailServer(
                user_name="user@example.test",
                password=SecretStr("secret"),
                host="imap.qq.com",
                port=993,
            ),
        )


@pytest.mark.asyncio
async def test_qq_auto_search_sends_quoted_utf8_through_uid_search() -> None:
    client = _client("imap.qq.com")
    imap = _connected_imap()

    with patch.object(client, "imap_class", return_value=imap):
        total, emails = await client.get_emails_metadata(subject='say"淘宝')

    assert (total, emails) == (0, [])
    imap.uid_search.assert_awaited_once_with("SUBJECT", '"say\\"淘宝"', charset="UTF-8")


@pytest.mark.asyncio
async def test_non_qq_auto_search_keeps_synchronizing_literal() -> None:
    client = _client("imap.example.com")
    imap = _connected_imap()
    literal_search = AsyncMock(return_value=("OK", [b""]))

    with (
        patch.object(client, "imap_class", return_value=imap),
        patch("mcp_email_server.emails.classic._uid_search", literal_search),
    ):
        await client.get_emails_metadata(subject="淘宝")

    criteria = literal_search.await_args.args[1]
    assert criteria == ["SUBJECT", _ImapSearchLiteral("淘宝".encode())]
    imap.uid_search.assert_not_called()


@pytest.mark.asyncio
async def test_quoted_mode_crlf_fails_before_search() -> None:
    client = _client("imap.example.com", mode="quoted")
    imap = _connected_imap()

    with (
        patch.object(client, "imap_class", return_value=imap),
        pytest.raises(ValueError, match="CR or LF"),
    ):
        await client.get_emails_metadata(subject="淘宝\n")

    imap.uid_search.assert_not_called()
    imap.logout.assert_awaited()


@pytest.mark.asyncio
async def test_quoted_utf8_search_is_one_line_without_a_synchronizing_literal() -> None:
    protocol = aioimaplib.IMAP4ClientProtocol(asyncio.get_running_loop())
    protocol.state = aioimaplib.SELECTED
    transport = _RecordingTransport()
    protocol.transport = transport
    imap = object.__new__(aioimaplib.IMAP4)
    imap.protocol = protocol
    imap.timeout = 1.0
    quoted = _quote_imap_search_text('a"淘宝')

    search_task = asyncio.create_task(_uid_search(imap, ["SUBJECT", quoted]))
    async with asyncio.timeout(1.0):
        while not transport.writes:
            await asyncio.sleep(0)
    tag = transport.writes[0].split(maxsplit=1)[0]
    protocol.data_received(b"* SEARCH 7\r\n" + tag + b" OK SEARCH completed\r\n")
    response = await search_task

    assert response.result == "OK"
    wire = transport.writes[0]
    assert wire.endswith(b'UID SEARCH CHARSET UTF-8 SUBJECT "a\\"\xe6\xb7\x98\xe5\xae\x9d"\r\n')
    assert b"{" not in wire
    assert len(transport.writes) == 1
