"""Tests for the Scher send_email extensions:

- custom Message-ID via the ``message_id`` argument (with ``<...>`` normalization)
- ``MCP_EMAIL_SERVER_REDIRECT_TO`` env-based test-mode redirection

Uses example.com / fake credentials throughout — never real production data.
"""

from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import REDIRECT_ENV_VAR, EmailClient, _normalize_msgid


@pytest.fixture
def email_client():
    server = EmailServer(
        user_name="test_user",
        password="test_password",
        host="smtp.example.com",
        port=465,
        use_ssl=True,
    )
    return EmailClient(server, sender="Test User <test@example.com>")


@pytest.fixture
def smtp_mock():
    """Patch aiosmtplib.SMTP to a no-op async context manager that captures messages."""
    mock_smtp = AsyncMock()
    mock_smtp.__aenter__.return_value = mock_smtp
    mock_smtp.__aexit__.return_value = None
    mock_smtp.login = AsyncMock()
    mock_smtp.send_message = AsyncMock()
    with patch("aiosmtplib.SMTP", return_value=mock_smtp):
        yield mock_smtp


# ===========================================================================
# _normalize_msgid
# ===========================================================================


class TestNormalizeMsgid:
    def test_adds_angle_brackets(self):
        assert _normalize_msgid("abc@example.com") == "<abc@example.com>"

    def test_keeps_brackets_if_present(self):
        assert _normalize_msgid("<abc@example.com>") == "<abc@example.com>"

    def test_strips_whitespace(self):
        assert _normalize_msgid("   <abc@example.com>  ") == "<abc@example.com>"

    def test_adds_missing_trailing_bracket(self):
        assert _normalize_msgid("<abc@example.com") == "<abc@example.com>"

    def test_adds_missing_leading_bracket(self):
        assert _normalize_msgid("abc@example.com>") == "<abc@example.com>"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _normalize_msgid("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _normalize_msgid("   ")

    def test_internal_whitespace_raises(self):
        with pytest.raises(ValueError, match="whitespace"):
            _normalize_msgid("with spaces@foo")

    def test_embedded_angle_brackets_raise(self):
        with pytest.raises(ValueError, match="angle brackets"):
            _normalize_msgid("contains<bad>chars@foo")

    def test_tab_inside_raises(self):
        with pytest.raises(ValueError, match="whitespace"):
            _normalize_msgid("a\tb@foo")

    def test_control_char_raises(self):
        with pytest.raises(ValueError, match="control characters"):
            _normalize_msgid("a\x01b@foo")


# ===========================================================================
# send_email with message_id
# ===========================================================================


class TestSendEmailMessageId:
    @pytest.mark.asyncio
    async def test_custom_message_id_used(self, email_client, smtp_mock):
        """When message_id is supplied, it appears as the Message-Id header."""
        await email_client.send_email(
            recipients=["alice@example.com"],
            subject="Hi",
            body="Body",
            message_id="custom-id-42@example.com",
        )
        msg = smtp_mock.send_message.call_args[0][0]
        assert msg["Message-Id"] == "<custom-id-42@example.com>"

    @pytest.mark.asyncio
    async def test_custom_message_id_already_bracketed(self, email_client, smtp_mock):
        """Brackets in the input are preserved as-is."""
        await email_client.send_email(
            recipients=["alice@example.com"],
            subject="Hi",
            body="Body",
            message_id="<already-wrapped@example.com>",
        )
        msg = smtp_mock.send_message.call_args[0][0]
        assert msg["Message-Id"] == "<already-wrapped@example.com>"

    @pytest.mark.asyncio
    async def test_no_message_id_uses_autogen(self, email_client, smtp_mock):
        """Default behavior is unchanged — Message-Id auto-generated."""
        await email_client.send_email(
            recipients=["alice@example.com"],
            subject="Hi",
            body="Body",
        )
        msg = smtp_mock.send_message.call_args[0][0]
        assert msg["Message-Id"] is not None
        # Auto-generated id should be wrapped already.
        assert msg["Message-Id"].startswith("<")
        assert msg["Message-Id"].endswith(">")


# ===========================================================================
# send_email with MCP_EMAIL_SERVER_REDIRECT_TO
# ===========================================================================


class TestRedirectTo:
    @pytest.mark.asyncio
    async def test_redirect_overrides_recipients(self, email_client, smtp_mock, monkeypatch):
        """When the env var is set, the To/Cc/Bcc envelope is replaced."""
        monkeypatch.setenv(REDIRECT_ENV_VAR, "sink@example.com")

        await email_client.send_email(
            recipients=["alice@example.com", "bob@example.com"],
            subject="Hi",
            body="Body",
            cc=["cc1@example.com"],
            bcc=["bcc1@example.com"],
        )

        msg = smtp_mock.send_message.call_args[0][0]
        # To header should be just the sink.
        assert msg["To"] == "sink@example.com"
        # No Cc on the message (we cleared cc list).
        assert msg["Cc"] is None
        # Visible originals (To/Cc) preserved for audit.
        assert msg["X-Original-To"] == "alice@example.com, bob@example.com"
        assert msg["X-Original-Cc"] == "cc1@example.com"
        # Bcc is intentionally NOT preserved — keeps the hidden address hidden
        # even from the redirect inbox.
        assert msg["X-Original-Bcc"] is None

        # SMTP envelope should also be the sink only.
        smtp_recipients = smtp_mock.send_message.call_args.kwargs.get("recipients")
        assert smtp_recipients == ["sink@example.com"]

    @pytest.mark.asyncio
    async def test_redirect_unset_passes_through(self, email_client, smtp_mock, monkeypatch):
        """Default (env unset) behavior is unchanged."""
        monkeypatch.delenv(REDIRECT_ENV_VAR, raising=False)

        await email_client.send_email(
            recipients=["alice@example.com"],
            subject="Hi",
            body="Body",
            cc=["cc1@example.com"],
        )

        msg = smtp_mock.send_message.call_args[0][0]
        assert msg["To"] == "alice@example.com"
        assert msg["Cc"] == "cc1@example.com"
        assert msg["X-Original-To"] is None
        assert msg["X-Original-Cc"] is None

        smtp_recipients = smtp_mock.send_message.call_args.kwargs.get("recipients")
        assert smtp_recipients == ["alice@example.com", "cc1@example.com"]

    @pytest.mark.asyncio
    async def test_redirect_whitespace_value_raises(self, email_client, smtp_mock, monkeypatch):
        """A whitespace-only env var is treated as a configuration error.

        Operators who set REDIRECT_TO intend redirection; a mistyped blank
        value silently delivering to production is the worst-case outcome.
        We fail loud instead.
        """
        monkeypatch.setenv(REDIRECT_ENV_VAR, "   ")

        with pytest.raises(ValueError, match="empty/whitespace-only"):
            await email_client.send_email(
                recipients=["alice@example.com"],
                subject="Hi",
                body="Body",
            )

        # SMTP must never have been invoked.
        smtp_mock.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_redirect_empty_string_raises(self, email_client, smtp_mock, monkeypatch):
        """An empty-string env var is treated the same as whitespace-only."""
        monkeypatch.setenv(REDIRECT_ENV_VAR, "")

        with pytest.raises(ValueError, match="empty/whitespace-only"):
            await email_client.send_email(
                recipients=["alice@example.com"],
                subject="Hi",
                body="Body",
            )

        smtp_mock.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_redirect_does_not_leak_to_bcc(self, email_client, smtp_mock, monkeypatch):
        """BCC must also be redirected — never leak production addresses in test mode.

        Verifies two leak paths: (a) the SMTP envelope must not include the
        original BCC address; (b) no X-Original-Bcc header is written, since
        that would expose the address to the redirect inbox.
        """
        monkeypatch.setenv(REDIRECT_ENV_VAR, "sink@example.com")

        await email_client.send_email(
            recipients=["alice@example.com"],
            subject="Hi",
            body="Body",
            bcc=["leak-target@example.com"],
        )

        # (a) SMTP envelope contains only the sink.
        smtp_recipients = smtp_mock.send_message.call_args.kwargs.get("recipients")
        assert "leak-target@example.com" not in smtp_recipients
        assert smtp_recipients == ["sink@example.com"]

        # (b) No X-Original-Bcc header — original address must not appear in
        # any header on the message that lands in the redirect inbox.
        msg = smtp_mock.send_message.call_args[0][0]
        assert msg["X-Original-Bcc"] is None
        msg_bytes = msg.as_bytes()
        assert b"leak-target@example.com" not in msg_bytes
