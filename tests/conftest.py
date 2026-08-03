from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent

import os
import shutil
import tempfile
import uuid

_WINDOWS_TEST_CONFIG_ROOT: Path | None = None
if os.name == "nt" and "MCP_EMAIL_SERVER_CONFIG_PATH" not in os.environ:
    from mcp_email_server.windows_security import ensure_private_parent

    _WINDOWS_TEST_CONFIG_ROOT = Path(tempfile.gettempdir()) / f"mcp-email-server-tests-{uuid.uuid4().hex}"
    _TEST_CONFIG_PATH = _WINDOWS_TEST_CONFIG_ROOT / "config.toml"
    ensure_private_parent(_TEST_CONFIG_PATH)
else:
    _TEST_CONFIG_PATH = Path(os.environ.get("MCP_EMAIL_SERVER_CONFIG_PATH", _HERE / "config.toml"))

os.environ.setdefault("MCP_EMAIL_SERVER_CONFIG_PATH", _TEST_CONFIG_PATH.as_posix())
os.environ["MCP_EMAIL_SERVER_LOG_LEVEL"] = "DEBUG"
# Guardrail: no test may reach a real OS keyring or hang CI waiting on D-Bus. Set at
# *import time* (not via an autouse fixture) because the existing autouse patch_env
# fixture below calls delete_settings() before every test, pytest does not guarantee
# ordering between independent same-scope autouse fixtures, and delete_settings()
# performs best-effort keyring cleanup — a fixture-based guard could apply too late.
# Individual tests override this via monkeypatch.setenv(...).
os.environ["MCP_EMAIL_SERVER_CREDENTIAL_STORAGE"] = "plaintext"

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends.fail import Keyring as FailKeyring
from keyring.errors import PasswordDeleteError

from mcp_email_server import keyring_store
from mcp_email_server.config import EmailServer, EmailSettings, ProviderSettings, delete_settings


def _harden_windows_test_directory(path: Path) -> None:
    if os.name != "nt":
        return
    from mcp_email_server.windows_security import (
        _apply_private_security,
        _close,
        _open_path,
        validate_private_directory,
    )

    handle = _open_path(path, directory=True, security_write=True)
    try:
        _apply_private_security(handle)
    finally:
        _close(handle)
    validate_private_directory(path)


@pytest.fixture(autouse=True)
def patch_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _harden_windows_test_directory(tmp_path)
    delete_settings()
    yield
    _TEST_CONFIG_PATH.with_name("config.bootstrap.toml.lock").unlink(missing_ok=True)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session, exitstatus
    if _WINDOWS_TEST_CONFIG_ROOT is not None:
        shutil.rmtree(_WINDOWS_TEST_CONFIG_ROOT, ignore_errors=True)


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


class _CompletedAwaitable:
    def __await__(self):
        yield from ()


@pytest.fixture
def completed_awaitable():
    """Return a reusable awaitable that is not bound to an event loop."""
    return _CompletedAwaitable()


@pytest.fixture
def mock_imap(completed_awaitable):
    """Fixture for a mocked IMAP client."""
    mock_imap = AsyncMock()
    mock_imap._client_task = completed_awaitable
    mock_imap.wait_hello_from_server = AsyncMock()
    mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    mock_imap.select = AsyncMock(return_value=("OK", []))
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


class _RecordingKeyring(KeyringBackend):
    """In-memory keyring that records every get/set/delete call for assertions."""

    priority = 1

    def __init__(self):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str]] = []

    def set_password(self, service, username, password):
        self.calls.append(("set", service, username))
        self._store[(service, username)] = password

    def get_password(self, service, username):
        self.calls.append(("get", service, username))
        return self._store.get((service, username))

    def delete_password(self, service, username):
        self.calls.append(("delete", service, username))
        key = (service, username)
        if key not in self._store:
            raise PasswordDeleteError(f"No such password for service '{service}', username '{username}'")
        del self._store[key]


@pytest.fixture
def fake_keyring():
    """Installs an in-memory keyring backend and yields it (with a `.calls` log).

    Backend install is decoupled from credential_storage mode selection — pick the
    mode a test needs separately via monkeypatch.setenv(...).
    """
    previous = keyring.get_keyring()
    backend = _RecordingKeyring()
    keyring.set_keyring(backend)
    keyring_store.keyring_usable.cache_clear()
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
        keyring_store.keyring_usable.cache_clear()


@pytest.fixture
def broken_keyring():
    """Installs a backend that raises NoKeyringError on every operation.

    Used both to exercise designed failure paths and as a no-I/O tripwire: any
    accidental keyring call in a test that shouldn't make one raises immediately.
    """
    previous = keyring.get_keyring()
    backend = FailKeyring()
    keyring.set_keyring(backend)
    keyring_store.keyring_usable.cache_clear()
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
        keyring_store.keyring_usable.cache_clear()
