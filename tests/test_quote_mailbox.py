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

    def test_quotes_special_folders(self):
        """Test quoting of various folder names."""
        assert _quote_mailbox("Sent") == '"Sent"'
        assert _quote_mailbox("INBOX.Sent") == '"INBOX.Sent"'
        assert _quote_mailbox("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'

    def test_quotes_empty_string(self):
        """Test handling of empty string."""
        assert _quote_mailbox("") == '""'

    def test_escapes_quotes_in_mailbox_name(self):
        """Test that double-quote characters are escaped per RFC 3501."""
        assert _quote_mailbox('My"Folder') == r'"My\"Folder"'
        assert _quote_mailbox('Test"Quote"Name') == r'"Test\"Quote\"Name"'

    def test_escapes_backslashes_in_mailbox_name(self):
        """Test that backslash characters are escaped per RFC 3501."""
        assert _quote_mailbox("My\\Folder") == r'"My\\Folder"'
        assert _quote_mailbox("Path\\To\\Folder") == r'"Path\\To\\Folder"'

    def test_escapes_both_quotes_and_backslashes(self):
        """Test escaping of both quotes and backslashes together."""
        assert _quote_mailbox('My\\"Folder') == r'"My\\\"Folder"'

    def test_already_quoted_gets_escaped(self):
        """Test that already-quoted names are properly escaped and re-quoted."""
        # Per RFC 3501, we should always escape and quote
        # '"INBOX"' should become '"\\"INBOX\\""' (quotes escaped)
        assert _quote_mailbox('"INBOX"') == r'"\"INBOX\""'
