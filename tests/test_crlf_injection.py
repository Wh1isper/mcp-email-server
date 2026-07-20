"""CRLF / SMTP-envelope injection hardening for send_email and save_to_mailbox."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.app import _reject_crlf, save_to_mailbox, send_email


class TestRejectCrlfHelper:
    @pytest.mark.parametrize("bad", ["a@b.com\r\nRSET", "a@b.com\nBcc: x@y.com", "x\ry", "line1\nline2"])
    def test_rejects_newlines(self, bad):
        with pytest.raises(ValueError, match="must not contain newline"):
            _reject_crlf([bad], "Recipient address")

    def test_allows_clean_values_and_none(self):
        _reject_crlf(["a@b.com", "b@c.com", None], "Recipient address")  # no raise


def _mock_send_settings():
    settings = MagicMock()
    settings.has_scope.return_value = True
    settings.allowed_recipients = []
    return settings


class TestSendEmailCrlf:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"recipients": ["a@b.com\r\nRCPT TO:<evil@x.com>"]},
            {"recipients": ["a@b.com"], "cc": ["c@d.com\nBcc: evil@x.com"]},
            {"recipients": ["a@b.com"], "bcc": ["e@f.com\r\nDATA"]},
            {"recipients": ["a@b.com"], "reply_to": "r@s.com\r\nX-Injected: 1"},
            {"recipients": ["a@b.com"], "in_reply_to": "<id>\r\nX: y"},
            {"recipients": ["a@b.com"], "attachments": ["/data/ok\r\n/etc/passwd"]},
        ],
    )
    async def test_send_email_rejects_crlf(self, kwargs):
        handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=_mock_send_settings()),
            patch("mcp_email_server.app.dispatch_handler", return_value=handler),
        ):
            with pytest.raises(ValueError, match="must not contain newline"):
                await send_email(account_name="a", subject="s", body="b", **kwargs)
        handler.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_email_rejects_crlf_subject(self):
        handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=_mock_send_settings()),
            patch("mcp_email_server.app.dispatch_handler", return_value=handler),
        ):
            with pytest.raises(ValueError, match="must not contain newline"):
                await send_email(account_name="a", recipients=["a@b.com"], subject="Hi\r\nBcc: evil@x.com", body="b")
        handler.send_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_email_clean_passes(self):
        handler = AsyncMock()
        with (
            patch("mcp_email_server.app.get_settings", return_value=_mock_send_settings()),
            patch("mcp_email_server.app.dispatch_handler", return_value=handler),
        ):
            await send_email(account_name="a", recipients=["a@b.com"], subject="s", body="b", bcc=["c@d.com"])
        handler.send_email.assert_called_once()


class TestSaveToMailboxCrlf:
    @pytest.mark.asyncio
    async def test_save_to_mailbox_rejects_crlf_bcc(self):
        handler = AsyncMock()
        handler.save_to_mailbox.return_value = "<id>|uid:1"
        with (
            patch("mcp_email_server.app.get_settings", return_value=_mock_send_settings()),
            patch("mcp_email_server.app.dispatch_handler", return_value=handler),
        ):
            with pytest.raises(ValueError, match="must not contain newline"):
                await save_to_mailbox(
                    account_name="a",
                    recipients=["a@b.com"],
                    subject="s",
                    body="b",
                    mailbox="Drafts",
                    bcc=["e@f.com\r\nRSET"],
                )
        handler.save_to_mailbox.assert_not_called()
