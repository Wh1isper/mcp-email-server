from datetime import datetime

from mcp_email_server.emails.models import EmailMetadata, EmailMetadataPageResponse


class TestEmailMetadata:
    def test_init(self):
        """Test initialization with valid data."""
        email_data = EmailMetadata(
            email_id="123",
            subject="Test Subject",
            sender="test@example.com",
            recipients=["recipient@example.com"],
            date=datetime.now(),
            attachments=["file1.txt", "file2.pdf"],
        )

        assert email_data.subject == "Test Subject"
        assert email_data.sender == "test@example.com"
        assert email_data.recipients == ["recipient@example.com"]
        assert isinstance(email_data.date, datetime)
        assert email_data.attachments == ["file1.txt", "file2.pdf"]

    def test_from_email(self):
        """Test from_email class method."""
        now = datetime.now()
        email_dict = {
            "email_id": "123",
            "subject": "Test Subject",
            "from": "test@example.com",
            "to": ["recipient@example.com"],
            "date": now,
            "attachments": ["file1.txt", "file2.pdf"],
        }

        email_data = EmailMetadata.from_email(email_dict)

        assert email_data.subject == "Test Subject"
        assert email_data.sender == "test@example.com"
        assert email_data.recipients == ["recipient@example.com"]
        assert email_data.date == now
        assert email_data.attachments == ["file1.txt", "file2.pdf"]


class TestEmailMetadataPageResponse:
    def test_init(self):
        """Test initialization with valid data."""
        now = datetime.now()
        email_data = EmailMetadata(
            email_id="123",
            subject="Test Subject",
            sender="test@example.com",
            recipients=["recipient@example.com"],
            date=now,
            attachments=[],
        )

        response = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=now,
            since=None,
            subject="Test",
            emails=[email_data],
            total=1,
        )

        assert response.page == 1
        assert response.page_size == 10
        assert response.before == now
        assert response.since is None
        assert response.subject == "Test"
        assert len(response.emails) == 1
        assert response.emails[0] == email_data
        assert response.total == 1

    def test_empty_emails(self):
        """Test with empty email list."""
        response = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[],
            total=0,
        )

        assert response.page == 1
        assert response.page_size == 10
        assert len(response.emails) == 0
        assert response.total == 0
