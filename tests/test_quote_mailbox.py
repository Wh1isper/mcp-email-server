"""Tests for IMAP mailbox encoding and quoting helpers."""

from mcp_email_server.emails.classic import _quote_mailbox, decode_mailbox_name, encode_mailbox_name


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


class TestModifiedUtf7MailboxCodec:
    """Tests for RFC 3501 Modified UTF-7 mailbox codec."""

    def test_decodes_exchange_german_folder_names(self):
        """Exchange localized folder names should decode to Unicode."""
        assert decode_mailbox_name("Entw&APw-rfe") == "Entwürfe"
        assert decode_mailbox_name("Gel&APY-schte Elemente") == "Gelöschte Elemente"

    def test_encodes_exchange_german_folder_names(self):
        """Unicode folder names should encode to IMAP wire names."""
        assert encode_mailbox_name("Entwürfe") == "Entw&APw-rfe"
        assert encode_mailbox_name("Gelöschte Elemente") == "Gel&APY-schte Elemente"

    def test_ampersand_round_trips(self):
        """Literal ampersands use the special &- sequence."""
        assert decode_mailbox_name("Sales &- Support") == "Sales & Support"
        assert encode_mailbox_name("Sales & Support") == "Sales &- Support"

    def test_invalid_modified_utf7_sequence_is_preserved(self):
        """Malformed encoded runs should remain visible to callers."""
        assert decode_mailbox_name("Broken&NotBase64-") == "Broken&NotBase64-"

    def test_quote_mailbox_encodes_unicode_folder_name(self):
        """Quoted mailbox arguments should use Modified UTF-7 on the wire."""
        assert _quote_mailbox("Entwürfe") == '"Entw&APw-rfe"'
        assert _quote_mailbox("Gelöschte Elemente") == '"Gel&APY-schte Elemente"'
