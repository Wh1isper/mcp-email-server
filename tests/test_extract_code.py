# Test fixtures intentionally use full-width CJK punctuation (e.g. a full-width
# colon between the keyword and the code) to mirror real verification emails,
# so silence the ambiguous-character lint for this file.
# ruff: noqa: RUF001
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.app import extract_verification_code
from mcp_email_server.emails.extract import extract_code
from mcp_email_server.emails.models import EmailBodyResponse, EmailContentBatchResponse


class TestExtractCode:
    def test_english(self):
        assert extract_code("Your verification code is 654321") == "654321"
        assert extract_code("confirmation code: 998877") == "998877"
        assert extract_code("Your OTP: 7890") == "7890"
        assert extract_code("passcode: 5678") == "5678"

    def test_chinese(self):
        assert extract_code("您的验证码：123456") == "123456"
        assert extract_code("确认码：ABCD12") == "ABCD12"
        # dual-delimiter case
        assert extract_code("您好，您的验证码是：654321。请勿泄露。") == "654321"
        assert extract_code("您的验证码为：987654") == "987654"

    def test_japanese_korean(self):
        assert extract_code("認証コード：345678") == "345678"
        assert extract_code("인증 코드: 456789") == "456789"
        assert extract_code("認証コードは 456789 です") == "456789"

    def test_code_is_pattern(self):
        assert extract_code("Your code is 112233") == "112233"
        assert extract_code("code: ABCDEF") == "ABCDEF"

    def test_standalone_digits(self):
        assert extract_code("Please enter 5678 to verify") == "5678"
        assert extract_code(" 12345678 ") == "12345678"

    def test_no_code(self):
        assert extract_code("Hello, this is a regular email") is None
        assert extract_code("") is None
        assert extract_code("Short 12") is None

    def test_does_not_capture_word_after_label(self):
        assert extract_code("verification code Your email is ready") is None
        assert extract_code("Your verification code Your code is 824593. Valid for 10 minutes.") == "824593"
        assert extract_code("verification code is 987654") == "987654"

    def test_rejects_years(self):
        assert extract_code("Order Confirmation #ABC-12345 sent on 2026-04-11") is None
        assert extract_code("Thanks for joining us in 2024!") is None
        assert extract_code("Subject mentions code 2026 year") is None

    def test_rejects_yyyymmdd_dates(self):
        assert extract_code("QA LANG JA 20260411") is None
        assert extract_code("reference 20260101 please") is None

    def test_five_to_seven_digits_in_year_range_still_extracted(self):
        assert extract_code("code is 12345") == "12345"
        assert extract_code("code is 123456") == "123456"


def _batch(body: str, subject: str = "Login") -> EmailContentBatchResponse:
    return EmailContentBatchResponse(
        emails=[
            EmailBodyResponse(
                email_id="123",
                subject=subject,
                sender="noreply@example.com",
                recipients=["me@example.com"],
                date=datetime.now(timezone.utc),
                body=body,
                attachments=[],
            )
        ],
        requested_count=1,
        retrieved_count=1,
        failed_ids=[],
    )


class TestExtractVerificationCodeTool:
    @pytest.mark.asyncio
    async def test_extracts_from_body(self):
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = _batch("Your verification code is 654321")

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await extract_verification_code(account_name="work", email_id="123")

        assert result.found is True
        assert result.code == "654321"
        assert result.email_id == "123"
        assert result.sender == "noreply@example.com"
        mock_handler.get_emails_content.assert_called_once_with(["123"], "INBOX")

    @pytest.mark.asyncio
    async def test_falls_back_to_subject(self):
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = _batch("Nothing useful here.", subject="Your code: 778899")

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await extract_verification_code(account_name="work", email_id="123")

        assert result.found is True
        assert result.code == "778899"

    @pytest.mark.asyncio
    async def test_no_code_found(self):
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = _batch("Just a regular newsletter.", subject="Hello")

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await extract_verification_code(account_name="work", email_id="123")

        assert result.found is False
        assert result.code is None

    @pytest.mark.asyncio
    async def test_email_not_found(self):
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = EmailContentBatchResponse(
            emails=[], requested_count=1, retrieved_count=0, failed_ids=["123"]
        )

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await extract_verification_code(account_name="work", email_id="123", mailbox="Spam")

        assert result.found is False
        assert result.code is None
        mock_handler.get_emails_content.assert_called_once_with(["123"], "Spam")
