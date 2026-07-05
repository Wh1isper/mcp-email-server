import asyncio
import email
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient, _create_smtp_ssl_context, _html_to_text, _imap_login


@pytest.fixture
def email_server():
    return EmailServer(
        user_name="test_user",
        password="test_password",
        host="imap.example.com",
        port=993,
        use_ssl=True,
    )


@pytest.fixture
def email_client(email_server):
    return EmailClient(email_server, sender="Test User <test@example.com>")


class TestImapLogin:
    @pytest.mark.asyncio
    async def test_imap_login_ok_returns_none(self):
        imap = AsyncMock()
        imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))

        await _imap_login(imap, "user@example.com", "secret")

        imap.login.assert_awaited_once_with("user@example.com", "secret")

    @pytest.mark.asyncio
    async def test_imap_login_no_raises_connection_error_with_detail(self):
        imap = AsyncMock()
        imap.login = AsyncMock(return_value=MagicMock(result="NO", lines=[b"Incorrect login credentials"]))

        with pytest.raises(ConnectionError) as exc_info:
            await _imap_login(imap, "user@example.com", "secret")

        message = str(exc_info.value)
        assert "user@example.com" in message
        assert "NO" in message
        assert "Incorrect login credentials" in message

    @pytest.mark.asyncio
    async def test_imap_login_decodes_non_utf8_detail_with_replacement(self):
        imap = AsyncMock()
        imap.login = AsyncMock(return_value=MagicMock(result="BAD", lines=[b"bad byte: \xff"]))

        with pytest.raises(ConnectionError) as exc_info:
            await _imap_login(imap, "user@example.com", "secret")

        assert "bad byte:" in str(exc_info.value)


class TestEmailClient:
    def test_init(self, email_server):
        """Test initialization of EmailClient."""
        client = EmailClient(email_server)
        assert client.email_server == email_server
        assert client.sender == email_server.user_name
        assert client.smtp_use_tls is True
        assert client.smtp_start_tls is False

        # Test with custom sender
        custom_sender = "Custom <custom@example.com>"
        client = EmailClient(email_server, sender=custom_sender)
        assert client.sender == custom_sender

    def test_parse_email_data_plain(self):
        """Test parsing plain text email."""
        # Create a simple plain text email
        msg = MIMEText("This is a test email body")
        msg["Subject"] = "Test Subject"
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Date"] = email.utils.formatdate()

        raw_email = msg.as_bytes()

        client = EmailClient(MagicMock())
        result = client._parse_email_data(raw_email)

        assert result["subject"] == "Test Subject"
        assert result["from"] == "sender@example.com"
        assert result["body"] == "This is a test email body"
        assert isinstance(result["date"], datetime)
        assert result["attachments"] == []

    def _make_raw_email(self, body: str) -> bytes:
        msg = MIMEText(body)
        msg["Subject"] = "Long"
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Date"] = email.utils.formatdate()
        return msg.as_bytes()

    def test_parse_email_data_truncates_to_max_body_length(self):
        """A body longer than max_body_length is truncated with the ...[TRUNCATED] marker."""
        raw_email = self._make_raw_email("x" * 100)
        client = EmailClient(MagicMock())

        result = client._parse_email_data(raw_email, max_body_length=10)
        assert result["body"] == "x" * 10 + "...[TRUNCATED]"

    def test_parse_email_data_no_marker_when_body_fits(self):
        """A body within the window is returned verbatim, with no marker appended."""
        raw_email = self._make_raw_email("x" * 10)
        client = EmailClient(MagicMock())

        result = client._parse_email_data(raw_email, max_body_length=10)
        assert result["body"] == "x" * 10

    def test_parse_email_data_body_offset_paging(self):
        """body_offset returns a contiguous, non-overlapping window for paging long bodies."""
        body = "abcdefghij"  # 10 chars
        raw_email = self._make_raw_email(body)
        client = EmailClient(MagicMock())

        # First page: chars [0:4], more remains -> marker appended.
        first = client._parse_email_data(raw_email, body_offset=0, max_body_length=4)
        assert first["body"] == "abcd...[TRUNCATED]"

        # Second page: chars [4:8], more remains -> marker appended.
        second = client._parse_email_data(raw_email, body_offset=4, max_body_length=4)
        assert second["body"] == "efgh...[TRUNCATED]"

        # Final page: chars [8:10], nothing remains -> no marker.
        third = client._parse_email_data(raw_email, body_offset=8, max_body_length=4)
        assert third["body"] == "ij"

        # Reassembling the un-marked chunks reproduces the original body.
        assert body == "abcd" + "efgh" + "ij"

    def test_parse_email_data_offset_past_end_returns_empty(self):
        """An offset at or beyond the body length yields an empty body and no marker."""
        raw_email = self._make_raw_email("abc")
        client = EmailClient(MagicMock())

        result = client._parse_email_data(raw_email, body_offset=10, max_body_length=20000)
        assert result["body"] == ""

    def test_parse_email_data_rejects_negative_body_offset(self):
        """body_offset must be non-negative, matching the public tool constraint."""
        raw_email = self._make_raw_email("abc")
        client = EmailClient(MagicMock())

        with pytest.raises(ValueError, match="body_offset must be >= 0"):
            client._parse_email_data(raw_email, body_offset=-1, max_body_length=10)

    def test_parse_email_data_rejects_non_positive_max_body_length(self):
        """max_body_length must be positive, matching the public tool constraint."""
        raw_email = self._make_raw_email("abc")
        client = EmailClient(MagicMock())

        with pytest.raises(ValueError, match="max_body_length must be >= 1"):
            client._parse_email_data(raw_email, body_offset=0, max_body_length=0)

    def test_html_to_text_removes_scripts_and_preserves_readable_text(self):
        """HTML fallback extraction uses an HTML parser for readable plain text."""
        html = """
        <html>
          <head><style>.hidden { display: none; }</style><script>alert('x')</script></head>
          <body>
            <h1>Title &amp; Updates</h1>
            <p>Hello&nbsp;<strong>there</strong></p>
            <div>Line<br>Break</div>
            <ul><li>One</li><li>Two</li></ul>
          </body>
        </html>
        """

        result = _html_to_text(html)

        assert "alert" not in result
        assert "display" not in result
        assert "Title & Updates" in result
        assert "Hello" in result
        assert "there" in result
        assert "Line" in result
        assert "Break" in result
        assert "One" in result
        assert "Two" in result

    def test_html_to_text_preserves_useful_link_urls(self):
        """HTML fallback preserves useful anchor hrefs in readable text."""
        html = """
        <p>Click <a href="https://example.com/verify">here</a> to verify.</p>
        <p>Visit <a href="https://example.com/help">https://example.com/help</a>.</p>
        <p>Textless <a href="https://example.com/textless"></a></p>
        """

        result = _html_to_text(html)

        assert "here (https://example.com/verify)" in result
        assert "https://example.com/help (https://example.com/help)" not in result
        assert "https://example.com/help" in result
        assert "https://example.com/textless" in result

    def test_html_to_text_skips_unsafe_or_non_content_links(self):
        """HTML fallback skips hrefs that should not be exposed as useful content links."""
        html = """
        <p><a href="#section">section</a></p>
        <p><a href="mailto:help@example.com">email us</a></p>
        <p><a href="javascript:alert('x')">click</a></p>
        <p><a href="java&#10;script:alert('x')">obfuscated</a></p>
        <p><a href="">empty</a></p>
        """

        result = _html_to_text(html)

        assert "section" in result
        assert "email us" in result
        assert "click" in result
        assert "obfuscated" in result
        assert "empty" in result
        assert "#section" not in result
        assert "mailto:" not in result
        assert "javascript:" not in result
        assert "alert" not in result

    def test_parse_email_data_html_single_part_falls_back_to_text(self):
        """Single-part HTML emails are converted to plain text."""
        msg = MIMEText("<html><body><p>Hello&nbsp;<b>world</b></p><script>x()</script></body></html>", "html", "utf-8")
        msg["Subject"] = "HTML Subject"
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Date"] = email.utils.formatdate()

        client = EmailClient(MagicMock())
        result = client._parse_email_data(msg.as_bytes())

        assert result["subject"] == "HTML Subject"
        assert "Hello" in result["body"]
        assert "world" in result["body"]
        assert "script" not in result["body"]
        assert "x()" not in result["body"]

    def test_parse_email_data_html_fallback_when_plain_text_missing(self):
        """Multipart emails use HTML fallback when text/plain is absent."""
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "HTML Only"
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Date"] = email.utils.formatdate()
        msg.attach(MIMEText("<div>First</div><div>Second &amp; third</div>", "html", "utf-8"))

        client = EmailClient(MagicMock())
        result = client._parse_email_data(msg.as_bytes())

        assert "First" in result["body"]
        assert "Second & third" in result["body"]

    def test_parse_email_data_with_attachments(self):
        """Test parsing email with attachments."""
        # This would require creating a multipart email with attachments
        # For simplicity, we'll mock the email parsing
        with patch("email.parser.BytesParser.parsebytes") as mock_parse:
            mock_email = MagicMock()
            mock_email.get.side_effect = lambda x, default=None: {
                "Subject": "Test Subject",
                "From": "sender@example.com",
                "Date": email.utils.formatdate(),
            }.get(x, default)
            mock_email.is_multipart.return_value = True

            # Mock parts
            text_part = MagicMock()
            text_part.get_content_type.return_value = "text/plain"
            text_part.get.return_value = ""  # Not an attachment
            text_part.get_payload.return_value = b"This is the email body"
            text_part.get_content_charset.return_value = "utf-8"

            attachment_part = MagicMock()
            attachment_part.get_content_type.return_value = "application/pdf"
            attachment_part.get.return_value = "attachment; filename=test.pdf"
            attachment_part.get_filename.return_value = "test.pdf"

            mock_email.walk.return_value = [text_part, attachment_part]
            mock_parse.return_value = mock_email

            client = EmailClient(MagicMock())
            result = client._parse_email_data(b"dummy email content")

            assert result["subject"] == "Test Subject"
            assert result["from"] == "sender@example.com"
            assert result["body"] == "This is the email body"
            assert isinstance(result["date"], datetime)
            assert result["attachments"] == ["test.pdf"]

    def test_build_search_criteria(self):
        """Test building search criteria for IMAP."""
        # Test with no criteria (should return ["ALL"])
        criteria = EmailClient._build_search_criteria()
        assert criteria == ["ALL"]

        # Test with before date
        before_date = datetime(2023, 1, 1, tzinfo=timezone.utc)
        criteria = EmailClient._build_search_criteria(before=before_date)
        assert criteria == ["BEFORE", "01-JAN-2023"]

        # Test with since date
        since_date = datetime(2023, 1, 1, tzinfo=timezone.utc)
        criteria = EmailClient._build_search_criteria(since=since_date)
        assert criteria == ["SINCE", "01-JAN-2023"]

        # Test with subject
        criteria = EmailClient._build_search_criteria(subject="Test")
        assert criteria == ["SUBJECT", "Test"]

        # Test with body
        criteria = EmailClient._build_search_criteria(body="Test")
        assert criteria == ["BODY", "Test"]

        # Test with text
        criteria = EmailClient._build_search_criteria(text="Test")
        assert criteria == ["TEXT", "Test"]

        # Test with from_address
        criteria = EmailClient._build_search_criteria(from_address="test@example.com")
        assert criteria == ["FROM", "test@example.com"]

        # Test with to_address
        criteria = EmailClient._build_search_criteria(to_address="test@example.com")
        assert criteria == ["TO", "test@example.com"]

        # Test with multiple criteria
        criteria = EmailClient._build_search_criteria(
            subject="Test", from_address="test@example.com", since=datetime(2023, 1, 1, tzinfo=timezone.utc)
        )
        assert criteria == ["SINCE", "01-JAN-2023", "SUBJECT", "Test", "FROM", "test@example.com"]

        # Test with seen=True (read emails)
        criteria = EmailClient._build_search_criteria(seen=True)
        assert criteria == ["SEEN"]

        # Test with seen=False (unread emails)
        criteria = EmailClient._build_search_criteria(seen=False)
        assert criteria == ["UNSEEN"]

        # Test with seen=None (all emails - no criteria added)
        criteria = EmailClient._build_search_criteria(seen=None)
        assert criteria == ["ALL"]

        # Test with flagged=True (starred emails)
        criteria = EmailClient._build_search_criteria(flagged=True)
        assert criteria == ["FLAGGED"]

        # Test with flagged=False (non-starred emails)
        criteria = EmailClient._build_search_criteria(flagged=False)
        assert criteria == ["UNFLAGGED"]

        # Test with answered=True (replied emails)
        criteria = EmailClient._build_search_criteria(answered=True)
        assert criteria == ["ANSWERED"]

        # Test with answered=False (not replied emails)
        criteria = EmailClient._build_search_criteria(answered=False)
        assert criteria == ["UNANSWERED"]

        # Test compound criteria: unread emails from a specific sender
        criteria = EmailClient._build_search_criteria(seen=False, from_address="sender@example.com")
        assert "UNSEEN" in criteria
        assert "FROM" in criteria
        assert "sender@example.com" in criteria

        # Test compound criteria: flagged and answered
        criteria = EmailClient._build_search_criteria(flagged=True, answered=True)
        assert "FLAGGED" in criteria
        assert "ANSWERED" in criteria

        # Test has_attachment=True (multipart/mixed heuristic)
        criteria = EmailClient._build_search_criteria(has_attachment=True)
        assert criteria == ["HEADER", "Content-Type", "multipart/mixed"]

        # Test has_attachment=False (negated heuristic)
        criteria = EmailClient._build_search_criteria(has_attachment=False)
        assert criteria == ["NOT", "HEADER", "Content-Type", "multipart/mixed"]

        # Test has_attachment=None (no criteria added)
        criteria = EmailClient._build_search_criteria(has_attachment=None)
        assert criteria == ["ALL"]

        # Test compound criteria: unread, flagged, from specific sender, with subject
        criteria = EmailClient._build_search_criteria(
            seen=False, flagged=True, from_address="test@example.com", subject="Important"
        )
        assert "UNSEEN" in criteria
        assert "FLAGGED" in criteria
        assert "FROM" in criteria
        assert "test@example.com" in criteria
        assert "SUBJECT" in criteria
        assert "Important" in criteria

    def test_build_search_criteria_multiword_subject(self):
        """Multi-word subjects must be quoted for IMAP."""
        criteria = EmailClient._build_search_criteria(subject="Meeting Notes")
        assert criteria == ["SUBJECT", '"Meeting Notes"']

    def test_build_search_criteria_multiword_from(self):
        """Multi-word from_address must be quoted for IMAP."""
        criteria = EmailClient._build_search_criteria(from_address="Alice Example")
        assert criteria == ["FROM", '"Alice Example"']

    def test_build_search_criteria_multiword_to(self):
        """Multi-word to_address must be quoted for IMAP."""
        criteria = EmailClient._build_search_criteria(to_address="Bob Smith")
        assert criteria == ["TO", '"Bob Smith"']

    def test_build_search_criteria_subject_with_embedded_quotes(self):
        """Embedded double quotes must be stripped (invalid in IMAP quoted strings)."""
        criteria = EmailClient._build_search_criteria(subject='He said "hello"')
        assert criteria == ["SUBJECT", '"He said hello"']

    @pytest.mark.asyncio
    async def test_get_emails_metadata(self, email_client):
        """Test getting emails metadata returns total and sorted, paginated results."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b"1 2 3"]))
        mock_imap.logout = AsyncMock()

        # Mock at the helper level - test behavior, not implementation
        mock_dates = {
            "1": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "2": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "3": datetime(2024, 1, 3, tzinfo=timezone.utc),
        }
        mock_metadata = {
            "1": {
                "email_id": "1",
                "subject": "Subject 1",
                "from": "a@test.com",
                "to": [],
                "date": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "attachments": [],
            },
            "2": {
                "email_id": "2",
                "subject": "Subject 2",
                "from": "b@test.com",
                "to": [],
                "date": datetime(2024, 1, 2, tzinfo=timezone.utc),
                "attachments": [],
            },
            "3": {
                "email_id": "3",
                "subject": "Subject 3",
                "from": "c@test.com",
                "to": [],
                "date": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "attachments": [],
            },
        }

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_dates", return_value=mock_dates) as mock_fetch_dates:
                with patch.object(
                    email_client, "_batch_fetch_headers", return_value=mock_metadata
                ) as mock_fetch_headers:
                    total, emails = await email_client.get_emails_metadata(page=1, page_size=10)

                    # Behavior: returns total count and emails sorted by date desc (newest first)
                    assert total == 3
                    assert len(emails) == 3
                    assert emails[0]["subject"] == "Subject 3"
                    assert emails[1]["subject"] == "Subject 2"
                    assert emails[2]["subject"] == "Subject 1"

                    mock_imap.login.assert_called_once()
                    mock_imap.logout.assert_called_once()

                    # Verify helpers called with correct arguments
                    mock_fetch_dates.assert_called_once_with(mock_imap, [b"1", b"2", b"3"])
                    # Headers fetched for page UIDs in sorted order (desc by date)
                    mock_fetch_headers.assert_called_once_with(mock_imap, ["3", "2", "1"])

    @pytest.mark.asyncio
    async def test_get_emails_metadata_encodes_unicode_mailbox(self, email_client):
        """Unicode mailbox names should be encoded before IMAP SELECT."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b""]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            total, emails = await email_client.get_emails_metadata(mailbox="Entwürfe")

        assert total == 0
        assert emails == []
        mock_imap.select.assert_called_once_with('"Entw&APw-rfe"')

    @pytest.mark.asyncio
    async def test_get_emails_metadata_search_omits_charset(self, email_client):
        """ASCII-only UID SEARCH must not send 'CHARSET utf-8'.

        aioimaplib defaults to charset='utf-8'; Microsoft Exchange rejects that
        with `NO [BADCHARSET (US-ASCII)]`, breaking all search/list operations.
        For ASCII-only criteria we must pass charset=None so no CHARSET token
        is sent.
        """
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b""]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            await email_client.get_emails_metadata(mailbox="INBOX")

        mock_imap.uid_search.assert_called_once()
        assert mock_imap.uid_search.call_args.kwargs.get("charset") is None

    @pytest.mark.asyncio
    async def test_get_emails_metadata_search_declares_utf8_for_non_ascii(self, email_client):
        """Non-ASCII (e.g. CJK) UID SEARCH must declare 'CHARSET utf-8'.

        RFC 3501 requires a charset declaration for non-ASCII search values;
        without it, servers such as Coremail interpret the raw UTF-8 bytes as
        US-ASCII and silently return zero matches. ASCII-only searches keep
        charset=None for Exchange compatibility (see the companion test above).
        """
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b""]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            await email_client.get_emails_metadata(mailbox="INBOX", subject="会议")

        mock_imap.uid_search.assert_called_once()
        assert mock_imap.uid_search.call_args.kwargs.get("charset") == "utf-8"
        assert "会议" in mock_imap.uid_search.call_args.args

    @pytest.mark.asyncio
    async def test_get_emails_metadata_search_ignores_non_ascii_generated_criteria(self, email_client):
        """Generated criteria must not trigger UTF-8 charset by themselves.

        Date strings are generated internally and may be locale-dependent. They
        should not cause charset='utf-8' for an otherwise ASCII-only user search,
        because that would reintroduce the Exchange BADCHARSET failure.
        """
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b""]))
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with patch.object(EmailClient, "_build_search_criteria", return_value=["SINCE", "01-MÄR-2026"]):
                await email_client.get_emails_metadata(mailbox="INBOX")

        mock_imap.uid_search.assert_called_once()
        assert mock_imap.uid_search.call_args.kwargs.get("charset") is None
        assert mock_imap.uid_search.call_args.args == ("SINCE", "01-MÄR-2026")

    @pytest.mark.asyncio
    async def test_get_emails_metadata_falls_back_to_uid_order_when_dates_missing(self, email_client):
        """Metadata listing should still return emails when INTERNALDATE parsing fails."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b"1 2 3"]))
        mock_imap.logout = AsyncMock()

        mock_metadata = {
            "1": {
                "email_id": "1",
                "subject": "Subject 1",
                "from": "a@test.com",
                "to": [],
                "date": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "attachments": [],
            },
            "2": {
                "email_id": "2",
                "subject": "Subject 2",
                "from": "b@test.com",
                "to": [],
                "date": datetime(2024, 1, 2, tzinfo=timezone.utc),
                "attachments": [],
            },
            "3": {
                "email_id": "3",
                "subject": "Subject 3",
                "from": "c@test.com",
                "to": [],
                "date": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "attachments": [],
            },
        }

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_dates", return_value={}) as mock_fetch_dates:
                with patch.object(
                    email_client, "_batch_fetch_headers", return_value=mock_metadata
                ) as mock_fetch_headers:
                    total, emails = await email_client.get_emails_metadata(page=1, page_size=2)

        assert total == 3
        assert [email["email_id"] for email in emails] == ["3", "2"]
        mock_fetch_dates.assert_called_once_with(mock_imap, [b"1", b"2", b"3"])
        mock_fetch_headers.assert_called_once_with(mock_imap, ["3", "2"])
        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_emails_metadata_raises_on_select_failure(self, email_client):
        """Test mailbox selection failures are surfaced before SEARCH."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("NO", [b"[NONEXISTENT] Unknown Mailbox: Archive"]))
        mock_imap.uid_search = AsyncMock()
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with pytest.raises(RuntimeError) as exc_info:
                await email_client.get_emails_metadata(mailbox="Archive")

        message = str(exc_info.value)
        assert "SELECT mailbox Archive failed" in message
        assert "NO" in message
        assert "[NONEXISTENT] Unknown Mailbox: Archive" in message
        mock_imap.uid_search.assert_not_called()
        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_emails_raises_on_select_failure(self, email_client):
        """Test delete stops when mailbox selection fails."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("NO", [b"[NONEXISTENT] Unknown Mailbox: Archive"]))
        mock_imap.uid = AsyncMock()
        mock_imap.expunge = AsyncMock()
        mock_imap.logout = AsyncMock()

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with pytest.raises(RuntimeError, match="SELECT mailbox Archive failed"):
                await email_client.delete_emails(["123"], mailbox="Archive")

        mock_imap.uid.assert_not_called()
        mock_imap.expunge.assert_not_called()
        mock_imap.logout.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_email(self, email_client):
        """Test sending email."""
        # Mock SMTP client
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
                cc=["cc@example.com"],
                bcc=["bcc@example.com"],
            )

            # Verify SMTP methods were called correctly
            mock_smtp.login.assert_called_once_with(
                email_client.email_server.user_name, email_client.email_server.password.get_secret_value()
            )
            mock_smtp.send_message.assert_called_once()

            # Check that the message was constructed correctly
            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            recipients = call_args[1]["recipients"]

            assert msg["Subject"] == "Test Subject"
            assert msg["From"] == email_client.sender
            assert msg["To"] == "recipient@example.com"
            assert msg["Cc"] == "cc@example.com"
            assert "Bcc" not in msg  # BCC should not be in headers

            # Check that all recipients are included in the SMTP call
            assert "recipient@example.com" in recipients
            assert "cc@example.com" in recipients
            assert "bcc@example.com" in recipients


class TestSendEmailMessageIdAndDate:
    def test_compose_message_encodes_only_non_ascii_sender_display_name(self, email_server):
        """Non-ASCII display names must not encode the addr-spec in the From header."""
        client = EmailClient(email_server, sender="刘滨瑞 <lbr1@mails.tsinghua.edu.cn>")

        msg = client.compose_message(
            recipients=["recipient@example.com"],
            subject="Test Subject",
            body="Test Body",
        )

        serialized = msg.as_string()
        from_header = next(line for line in serialized.splitlines() if line.startswith("From:"))
        assert from_header.startswith("From: =?utf-8?")
        assert "<lbr1@mails.tsinghua.edu.cn>" in from_header
        assert "@mails.tsinghua.edu.cn>" in msg["Message-Id"]

    @pytest.mark.asyncio
    async def test_send_email_sets_message_id_and_date(self, email_client):
        """Test that send_email sets Message-Id and Date headers."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            msg = await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test Subject",
                body="Test Body",
            )

            assert msg["Message-Id"] is not None
            assert "@example.com>" in msg["Message-Id"]
            assert msg["Date"] is not None

    @pytest.mark.asyncio
    async def test_send_email_message_id_uses_sender_domain(self, email_server):
        """Test that Message-Id domain is extracted from the sender address."""
        client = EmailClient(email_server, sender="user@getsequel.app")
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            msg = await client.send_email(
                recipients=["recipient@example.com"],
                subject="Test",
                body="Body",
            )

            assert "@getsequel.app>" in msg["Message-Id"]

    @pytest.mark.asyncio
    async def test_send_email_same_message_on_smtp_and_return(self, email_client):
        """Test that the same msg object (with Message-Id) is sent and returned."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            returned_msg = await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test",
                body="Body",
            )

            sent_msg = mock_smtp.send_message.call_args[0][0]
            assert sent_msg["Message-Id"] == returned_msg["Message-Id"]
            assert sent_msg["Date"] == returned_msg["Date"]


class TestParseEmailData:
    def test_parse_email_extracts_message_id(self, email_client):
        """Test that Message-ID header is extracted during parsing."""
        raw_email = b"""Message-ID: <test123@example.com>
From: sender@example.com
To: recipient@example.com
Subject: Test Subject
Date: Mon, 1 Jan 2024 12:00:00 +0000

Test body content
"""
        result = email_client._parse_email_data(raw_email, email_id="1")
        assert result["message_id"] == "<test123@example.com>"

    def test_parse_email_handles_missing_message_id(self, email_client):
        """Test graceful handling when Message-ID is missing."""
        raw_email = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Subject
Date: Mon, 1 Jan 2024 12:00:00 +0000

Test body content
"""
        result = email_client._parse_email_data(raw_email, email_id="1")
        assert result["message_id"] is None


class TestSendEmailReplyHeaders:
    @pytest.mark.asyncio
    async def test_send_email_sets_in_reply_to_header(self, email_client):
        """Test that In-Reply-To header is set when provided."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Re: Test",
                body="Reply body",
                in_reply_to="<original123@example.com>",
            )

            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert msg["In-Reply-To"] == "<original123@example.com>"

    @pytest.mark.asyncio
    async def test_send_email_sets_references_header(self, email_client):
        """Test that References header is set when provided."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Re: Test",
                body="Reply body",
                references="<first@example.com> <second@example.com>",
            )

            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert msg["References"] == "<first@example.com> <second@example.com>"

    @pytest.mark.asyncio
    async def test_send_email_without_reply_headers(self, email_client):
        """Test that send works without reply headers (backward compatibility)."""
        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp):
            await email_client.send_email(
                recipients=["recipient@example.com"],
                subject="Test",
                body="Body",
            )

            call_args = mock_smtp.send_message.call_args
            msg = call_args[0][0]
            assert "In-Reply-To" not in msg
            assert "References" not in msg


class TestSmtpSslContext:
    """Tests for SMTP SSL context creation."""

    def test_create_smtp_ssl_context_with_verification(self):
        """When verify_ssl=True, should return None (use default verification)."""
        result = _create_smtp_ssl_context(verify_ssl=True)
        assert result is None

    def test_create_smtp_ssl_context_without_verification(self):
        """When verify_ssl=False, should return permissive SSL context."""
        result = _create_smtp_ssl_context(verify_ssl=False)

        assert result is not None
        assert isinstance(result, ssl.SSLContext)
        assert result.check_hostname is False
        assert result.verify_mode == ssl.CERT_NONE

    def test_email_client_get_smtp_ssl_context_default(self):
        """EmailClient should use verify_ssl from EmailServer (default True)."""
        server = EmailServer(
            user_name="test",
            password="test",
            host="smtp.example.com",
            port=587,
        )
        client = EmailClient(server)

        # Default verify_ssl is True, so should return None
        assert client.smtp_verify_ssl is True
        assert client._get_smtp_ssl_context() is None

    def test_email_client_get_smtp_ssl_context_disabled(self):
        """EmailClient should return permissive context when verify_ssl=False."""
        server = EmailServer(
            user_name="test",
            password="test",
            host="smtp.example.com",
            port=587,
            verify_ssl=False,
        )
        client = EmailClient(server)

        assert client.smtp_verify_ssl is False
        ctx = client._get_smtp_ssl_context()
        assert ctx is not None
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    @pytest.mark.asyncio
    async def test_send_email_passes_tls_context(self):
        """send_email should pass tls_context to SMTP connection."""
        server = EmailServer(
            user_name="test",
            password="test",
            host="smtp.example.com",
            port=587,
            verify_ssl=False,
        )
        client = EmailClient(server, sender="test@example.com")

        mock_smtp = AsyncMock()
        mock_smtp.__aenter__.return_value = mock_smtp
        mock_smtp.__aexit__.return_value = None
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()

        with patch("aiosmtplib.SMTP", return_value=mock_smtp) as mock_smtp_class:
            await client.send_email(
                recipients=["recipient@example.com"],
                subject="Test",
                body="Body",
            )

            # Verify SMTP was called with tls_context
            call_kwargs = mock_smtp_class.call_args.kwargs
            assert "tls_context" in call_kwargs
            ctx = call_kwargs["tls_context"]
            assert ctx is not None
            assert ctx.check_hostname is False
            assert ctx.verify_mode == ssl.CERT_NONE


class TestParseHeaders:
    def test_parse_headers_extracts_metadata(self, email_client):
        """Test that _parse_headers correctly extracts email metadata."""
        raw_headers = b"""From: sender@example.com
To: recipient@example.com, other@example.com
Cc: cc@example.com
Subject: Test Subject
Date: Mon, 1 Jan 2024 12:00:00 +0000

"""
        result = email_client._parse_headers("123", raw_headers)

        assert result["email_id"] == "123"
        assert result["subject"] == "Test Subject"
        assert result["from"] == "sender@example.com"
        assert "recipient@example.com" in result["to"]
        assert "cc@example.com" in result["to"]
        assert result["attachments"] == []

    def test_parse_headers_handles_missing_fields(self, email_client):
        """Test that _parse_headers handles emails with missing headers."""
        raw_headers = b"""Subject: Minimal Email

"""
        result = email_client._parse_headers("456", raw_headers)

        assert result["email_id"] == "456"
        assert result["subject"] == "Minimal Email"
        assert result["from"] == ""
        assert result["to"] == []


class TestBatchFetchDates:
    @pytest.mark.asyncio
    async def test_batch_fetch_dates_parses_imap_response(self, email_client):
        """Test that _batch_fetch_dates correctly parses IMAP INTERNALDATE responses."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b'1 FETCH (UID 100 INTERNALDATE "01-Jan-2024 12:00:00 +0000")',
                    b'2 FETCH (UID 200 INTERNALDATE "02-Jan-2024 12:00:00 +0000")',
                    b"FETCH completed",
                ],
            )
        )

        result = await email_client._batch_fetch_dates(mock_imap, [b"100", b"200"])

        assert len(result) == 2
        assert "100" in result
        assert "200" in result
        assert result["100"].day == 1
        assert result["200"].day == 2

    @pytest.mark.asyncio
    async def test_batch_fetch_dates_empty_input(self, email_client):
        """Test that _batch_fetch_dates returns empty dict for empty input."""
        mock_imap = AsyncMock()
        result = await email_client._batch_fetch_dates(mock_imap, [])
        assert result == {}
        mock_imap.uid.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_fetch_dates_handles_fastmail_format(self, email_client):
        """Test that _batch_fetch_dates handles space-padded dates (Fastmail)."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b'1 FETCH (UID 100 INTERNALDATE " 1-Jan-2024 12:00:00 +0000")',
                    b"FETCH completed",
                ],
            )
        )

        result = await email_client._batch_fetch_dates(mock_imap, [b"100"])

        assert result["100"] == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_batch_fetch_dates_chunks_large_uid_lists(self, email_client):
        """Test that _batch_fetch_dates splits large UID lists into sequential chunks.

        Regression test for recursion overflow in aioimaplib when processing
        responses for thousands of UIDs in a single FETCH command.
        aioimaplib's _handle_responses uses recursion to parse response lines;
        with >1000 responses in a single buffer, this exceeds Python's default
        recursion limit and causes a RecursionError / infinite hang.

        See: https://github.com/ai-zerolab/mcp-email-server/pull/155
        """
        # Simulate a mailbox with 1500 UIDs — must result in multiple chunks
        num_uids = 1500
        uid_list = [str(i).encode() for i in range(1, num_uids + 1)]

        call_count = 0

        async def mock_uid_fetch(cmd, uid_csv, fields):
            nonlocal call_count
            call_count += 1
            uids = uid_csv.split(",")
            data = [
                f'{i} FETCH (UID {uid} INTERNALDATE "01-Jan-2024 12:00:00 +0000")'.encode()
                for i, uid in enumerate(uids, 1)
            ]
            data.append(b"FETCH completed")
            return (None, data)

        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(side_effect=mock_uid_fetch)

        result = await email_client._batch_fetch_dates(mock_imap, uid_list, chunk_size=500)

        # Must have chunked into 3 calls (500 + 500 + 500)
        assert call_count == 3, f"Expected 3 sequential chunks, got {call_count} calls"
        assert len(result) == num_uids

    @pytest.mark.asyncio
    async def test_batch_fetch_dates_sequential_not_parallel(self, email_client):
        """Test that chunks are fetched sequentially, not in parallel.

        IMAP is a sequential protocol — parallel FETCH commands on a single
        connection cause undefined behaviour. Verify that chunks execute serially.
        """
        execution_order = []
        chunk_counter = 0

        async def mock_uid_fetch(cmd, uid_csv, fields):
            nonlocal chunk_counter
            chunk_counter += 1
            chunk_id = chunk_counter
            execution_order.append(f"start-{chunk_id}")
            await asyncio.sleep(0.01)  # Simulate network latency
            execution_order.append(f"end-{chunk_id}")
            uids = uid_csv.split(",")
            data = [
                f'{i} FETCH (UID {uid} INTERNALDATE "01-Jan-2024 12:00:00 +0000")'.encode()
                for i, uid in enumerate(uids, 1)
            ]
            return (None, data)

        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(side_effect=mock_uid_fetch)

        uid_list = [str(i).encode() for i in range(1, 21)]
        await email_client._batch_fetch_dates(mock_imap, uid_list, chunk_size=10)

        # Sequential execution: start-1, end-1, start-2, end-2
        assert execution_order == ["start-1", "end-1", "start-2", "end-2"]


class TestBatchFetchHeaders:
    @pytest.mark.asyncio
    async def test_batch_fetch_headers_parses_imap_response(self, email_client):
        """Test that _batch_fetch_headers correctly parses IMAP header responses."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b"1 FETCH (UID 100 BODY[HEADER] {50}",
                    bytearray(b"From: a@test.com\r\nSubject: Test\r\n\r\n"),
                    b")",
                    b"FETCH completed",
                ],
            )
        )

        result = await email_client._batch_fetch_headers(mock_imap, ["100"])

        assert "100" in result
        assert result["100"]["subject"] == "Test"
        assert result["100"]["from"] == "a@test.com"

    @pytest.mark.asyncio
    async def test_batch_fetch_headers_empty_input(self, email_client):
        """Test that _batch_fetch_headers returns empty dict for empty input."""
        mock_imap = AsyncMock()
        result = await email_client._batch_fetch_headers(mock_imap, [])
        assert result == {}
        mock_imap.uid.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_fetch_headers_preserves_uid_mapping(self, email_client):
        """Test that _batch_fetch_headers returns dict keyed by UID."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b"1 FETCH (UID 100 BODY[HEADER] {50}",
                    bytearray(b"From: a@test.com\r\nSubject: First\r\n\r\n"),
                    b")",
                    b"2 FETCH (UID 200 BODY[HEADER] {50}",
                    bytearray(b"From: b@test.com\r\nSubject: Second\r\n\r\n"),
                    b")",
                    b"FETCH completed",
                ],
            )
        )

        result = await email_client._batch_fetch_headers(mock_imap, ["100", "200"])

        assert len(result) == 2
        assert result["100"]["subject"] == "First"
        assert result["200"]["subject"] == "Second"


class TestBatchFetchSenders:
    @pytest.mark.asyncio
    async def test_batch_fetch_senders(self, email_client):
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b"1 FETCH (UID 100 BODY[HEADER.FIELDS (FROM)] {25}",
                    bytearray(b"From: alice@example.com\r\n\r\n"),
                    b")",
                    b"2 FETCH (UID 101 BODY[HEADER.FIELDS (FROM)] {32}",
                    bytearray(b"From: Bob <bob@evil.com>\r\n\r\n"),
                    b")",
                ],
            )
        )
        result = await email_client._batch_fetch_senders(mock_imap, [b"100", b"101"])
        assert result == {"100": "alice@example.com", "101": "Bob <bob@evil.com>"}


class TestGetEmailsMetadataSenderFilter:
    @pytest.mark.asyncio
    async def test_get_emails_metadata_filters_blocked_senders_honest_total(self, email_client):
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b"1 2 3"]))
        mock_imap.logout = AsyncMock()

        mock_senders = {"1": "alice@example.com", "2": "bob@evil.com", "3": "alice@example.com"}
        mock_dates = {
            "1": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "3": datetime(2024, 1, 3, tzinfo=timezone.utc),
        }
        mock_metadata = {
            "3": {
                "email_id": "3",
                "subject": "S3",
                "from": "alice@example.com",
                "to": [],
                "date": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "attachments": [],
            },
            "1": {
                "email_id": "1",
                "subject": "S1",
                "from": "alice@example.com",
                "to": [],
                "date": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "attachments": [],
            },
        }

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_senders", return_value=mock_senders) as mock_fs:
                with patch.object(email_client, "_batch_fetch_dates", return_value=mock_dates) as mock_fd:
                    with patch.object(email_client, "_batch_fetch_headers", return_value=mock_metadata):
                        total, emails = await email_client.get_emails_metadata(
                            page=1, page_size=10, allowed_senders=["alice@example.com"]
                        )

        assert total == 2  # honest: allowed count, NOT the raw 3
        assert len(emails) == 2
        assert all(e["from"] == "alice@example.com" for e in emails)
        mock_fs.assert_called_once_with(mock_imap, [b"1", b"2", b"3"])  # senders fetched for ALL matches
        mock_fd.assert_called_once_with(mock_imap, [b"1", b"3"])  # dates only for allowed

    @pytest.mark.asyncio
    async def test_get_emails_metadata_full_page_despite_blocked(self, email_client):
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b"1 2 3 4"]))
        mock_imap.logout = AsyncMock()

        mock_senders = {
            "1": "alice@example.com",
            "2": "bob@evil.com",
            "3": "alice@example.com",
            "4": "alice@example.com",
        }
        mock_dates = {
            "1": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "3": datetime(2024, 1, 3, tzinfo=timezone.utc),
            "4": datetime(2024, 1, 4, tzinfo=timezone.utc),
        }
        mock_metadata = {
            "4": {
                "email_id": "4",
                "subject": "S4",
                "from": "alice@example.com",
                "to": [],
                "date": datetime(2024, 1, 4, tzinfo=timezone.utc),
                "attachments": [],
            },
            "3": {
                "email_id": "3",
                "subject": "S3",
                "from": "alice@example.com",
                "to": [],
                "date": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "attachments": [],
            },
        }

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_senders", return_value=mock_senders):
                with patch.object(email_client, "_batch_fetch_dates", return_value=mock_dates):
                    with patch.object(email_client, "_batch_fetch_headers", return_value=mock_metadata):
                        total, emails = await email_client.get_emails_metadata(
                            page=1, page_size=2, allowed_senders=["alice@example.com"]
                        )

        assert total == 3  # 3 allowed (4 matched, 1 blocked)
        assert len(emails) == 2  # full page, not short

    @pytest.mark.asyncio
    async def test_get_emails_metadata_no_allowlist_skips_sender_fetch(self, email_client):
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b"1 2 3"]))
        mock_imap.logout = AsyncMock()

        mock_dates = {
            "1": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "2": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "3": datetime(2024, 1, 3, tzinfo=timezone.utc),
        }
        mock_metadata = {
            "3": {
                "email_id": "3",
                "subject": "S3",
                "from": "c@test.com",
                "to": [],
                "date": datetime(2024, 1, 3, tzinfo=timezone.utc),
                "attachments": [],
            },
            "2": {
                "email_id": "2",
                "subject": "S2",
                "from": "b@test.com",
                "to": [],
                "date": datetime(2024, 1, 2, tzinfo=timezone.utc),
                "attachments": [],
            },
            "1": {
                "email_id": "1",
                "subject": "S1",
                "from": "a@test.com",
                "to": [],
                "date": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "attachments": [],
            },
        }

        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_senders") as mock_fs:
                with patch.object(email_client, "_batch_fetch_dates", return_value=mock_dates):
                    with patch.object(email_client, "_batch_fetch_headers", return_value=mock_metadata):
                        total, emails = await email_client.get_emails_metadata(page=1, page_size=10)

        assert total == 3
        assert len(emails) == 3
        mock_fs.assert_not_called()  # zero overhead when no allowlist


class TestSenderFilterCoverage:
    @pytest.mark.asyncio
    async def test_batch_fetch_senders_empty_returns_empty(self, email_client):
        mock_imap = AsyncMock()
        result = await email_client._batch_fetch_senders(mock_imap, [])
        assert result == {}
        mock_imap.uid.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_fetch_senders_skips_unparseable_header(self, email_client):
        """A header block that fails to parse is skipped, not added."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [b"1 FETCH (UID 100 BODY[HEADER.FIELDS (FROM)] {1}", bytearray(b"x"), b")"],
            )
        )
        with patch.object(email_client, "_parse_headers", return_value=None):
            result = await email_client._batch_fetch_senders(mock_imap, [b"100"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_batch_fetch_senders_proton_bridge_format(self, email_client):
        """Proton Bridge returns the UID in a trailing item after the header payload."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(
                None,
                [
                    b"1 FETCH (BODY[HEADER.FIELDS (FROM)] {23}",
                    bytearray(b"From: bob@example.com\r\n\r\n"),
                    b" UID 200)",
                ],
            )
        )
        result = await email_client._batch_fetch_senders(mock_imap, [b"200"])
        assert result == {"200": "bob@example.com"}

    @pytest.mark.asyncio
    async def test_get_emails_metadata_all_blocked_returns_empty(self, email_client):
        """When every match is filtered out, total is 0 and the date fetch is skipped."""
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", []))
        mock_imap.uid_search = AsyncMock(return_value=(None, [b"1 2"]))
        mock_imap.logout = AsyncMock()
        mock_senders = {"1": "bob@evil.com", "2": "carol@evil.com"}
        with patch.object(email_client, "imap_class", return_value=mock_imap):
            with patch.object(email_client, "_batch_fetch_senders", return_value=mock_senders):
                with patch.object(email_client, "_batch_fetch_dates") as mock_fd:
                    total, emails = await email_client.get_emails_metadata(
                        page=1, page_size=10, allowed_senders=["alice@example.com"]
                    )
        assert total == 0
        assert emails == []
        mock_fd.assert_not_called()  # early return before the date fetch

    @pytest.mark.asyncio
    async def test_batch_fetch_senders_non_bytearray_payload_skipped(self, email_client):
        """An item whose payload is not a bytearray matches neither branch and is skipped."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(None, [b"1 FETCH (UID 100 BODY[HEADER.FIELDS (FROM)] {0}", b"not-bytearray", b")"])
        )
        result = await email_client._batch_fetch_senders(mock_imap, [b"100"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_batch_fetch_senders_proton_missing_uid_skipped(self, email_client):
        """A trailing-payload item with no UID is skipped."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(None, [b"1 FETCH (BODY[HEADER.FIELDS (FROM)] {x}", bytearray(b"From: a@b.com\r\n\r\n"), b")"])
        )
        result = await email_client._batch_fetch_senders(mock_imap, [b"100"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_batch_fetch_senders_proton_unparseable_skipped(self, email_client):
        """A trailing-UID item whose header fails to parse is skipped."""
        mock_imap = AsyncMock()
        mock_imap.uid = AsyncMock(
            return_value=(None, [b"1 FETCH (BODY[HEADER.FIELDS (FROM)] {1}", bytearray(b"x"), b" UID 300)"])
        )
        with patch.object(email_client, "_parse_headers", return_value=None):
            result = await email_client._batch_fetch_senders(mock_imap, [b"300"])
        assert result == {}
