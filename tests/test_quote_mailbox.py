"""Tests for the _quote_mailbox helper function."""

from mcp_email_server.emails.classic import _quote_mailbox


class TestQuoteMailbox:
    """Tests for _quote_mailbox function."""

    def test_quotes_simple_mailbox_name(self):
        """Test that simple mailbox names are quoted."""
        assert _quote_mailbox("INBOX") == '"INBOX"'

    def test_quotes_mailbox_with_spaces(self):
        """Test that mailbox names with spaces are quoted."""
        assert _quote_mailbox("All Mail") == '"All Mail"'

    def test_does_not_double_quote(self):
        """Test that already-quoted names are not double-quoted."""
        assert _quote_mailbox('"INBOX"') == '"INBOX"'
        assert _quote_mailbox('"All Mail"') == '"All Mail"'

    def test_quotes_special_folders(self):
        """Test quoting of various folder names."""
        assert _quote_mailbox("Sent") == '"Sent"'
        assert _quote_mailbox("INBOX.Sent") == '"INBOX.Sent"'
        assert _quote_mailbox("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'

    def test_quotes_empty_string(self):
        """Test handling of empty string."""
        assert _quote_mailbox("") == '""'
