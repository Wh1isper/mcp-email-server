from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mcp_email_server.config import EmailServer, EmailSettings, ProviderSettings, delete_settings

_HERE = Path(__file__).resolve().parent

os.environ["MCP_EMAIL_SERVER_CONFIG_PATH"] = (_HERE / "config.toml").as_posix()
os.environ["MCP_EMAIL_SERVER_LOG_LEVEL"] = "DEBUG"


@pytest.fixture(autouse=True)
def patch_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory):
    delete_settings()
    yield


@pytest.fixture
def email_server():
    """Fixture for a test EmailServer."""
    return EmailServer(
        user_name="test_user",
        password="test_password",
        host="test.example.com",
        port=993,
        use_ssl=True,
    )


@pytest.fixture
def email_settings():
    """Fixture for test EmailSettings."""
    return EmailSettings(
        account_name="test_account",
        full_name="Test User",
        email_address="test@example.com",
        incoming=EmailServer(
            user_name="test_user",
            password="test_password",
            host="imap.example.com",
            port=993,
            use_ssl=True,
        ),
        outgoing=EmailServer(
            user_name="test_user",
            password="test_password",
            host="smtp.example.com",
            port=465,
            use_ssl=True,
        ),
    )


@pytest.fixture
def provider_settings():
    """Fixture for test ProviderSettings."""
    return ProviderSettings(
        account_name="test_provider",
        provider_name="test_provider",
        api_key="test_api_key",
    )


@pytest.fixture
def mock_imap():
    """Fixture for a mocked IMAP client."""
    mock_imap = AsyncMock()
    mock_imap._client_task = asyncio.Future()
    mock_imap._client_task.set_result(None)
    mock_imap.wait_hello_from_server = AsyncMock()
    mock_imap.login = AsyncMock()
    mock_imap.select = AsyncMock()
    mock_imap.search = AsyncMock(return_value=(None, [b"1 2 3"]))
    mock_imap.fetch = AsyncMock(return_value=(None, [b"HEADER", bytearray(b"EMAIL CONTENT")]))
    mock_imap.logout = AsyncMock()
    return mock_imap


@pytest.fixture
def mock_smtp():
    """Fixture for a mocked SMTP client."""
    mock_smtp = AsyncMock()
    mock_smtp.__aenter__.return_value = mock_smtp
    mock_smtp.__aexit__.return_value = None
    mock_smtp.login = AsyncMock()
    mock_smtp.send_message = AsyncMock()
    return mock_smtp


@pytest.fixture
def sample_email_data():
    """Fixture for sample email data."""
    now = datetime.now()
    return {
        "subject": "Test Subject",
        "from": "sender@example.com",
        "body": "Test Body",
        "date": now,
        "attachments": ["attachment.pdf"],
    }
