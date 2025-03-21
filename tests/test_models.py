from datetime import datetime

from mcp_email_server.emails.models import EmailData, EmailPageResponse


class TestEmailData:
    def test_init(self):
        """Test initialization with valid data."""
        email_data = EmailData(
            subject="Test Subject",
            sender="test@example.com",
            body="Test Body",
            date=datetime.now(),
            attachments=["file1.txt", "file2.pdf"],
        )

        assert email_data.subject == "Test Subject"
        assert email_data.sender == "test@example.com"
        assert email_data.body == "Test Body"
        assert isinstance(email_data.date, datetime)
        assert email_data.attachments == ["file1.txt", "file2.pdf"]

    def test_from_email(self):
        """Test from_email class method."""
        now = datetime.now()
        email_dict = {
            "subject": "Test Subject",
            "from": "test@example.com",
            "body": "Test Body",
            "date": now,
            "attachments": ["file1.txt", "file2.pdf"],
        }

        email_data = EmailData.from_email(email_dict)

        assert email_data.subject == "Test Subject"
        assert email_data.sender == "test@example.com"
        assert email_data.body == "Test Body"
        assert email_data.date == now
        assert email_data.attachments == ["file1.txt", "file2.pdf"]


class TestEmailPageResponse:
    def test_init(self):
        """Test initialization with valid data."""
        now = datetime.now()
        email_data = EmailData(
            subject="Test Subject",
            sender="test@example.com",
            body="Test Body",
            date=now,
            attachments=[],
        )

        response = EmailPageResponse(
            page=1,
            page_size=10,
            before=now,
            since=None,
            subject="Test",
            body=None,
            text=None,
            emails=[email_data],
            total=1,
        )

        assert response.page == 1
        assert response.page_size == 10
        assert response.before == now
        assert response.since is None
        assert response.subject == "Test"
        assert response.body is None
        assert response.text is None
        assert len(response.emails) == 1
        assert response.emails[0] == email_data
        assert response.total == 1

    def test_empty_emails(self):
        """Test with empty email list."""
        response = EmailPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            body=None,
            text=None,
            emails=[],
            total=0,
        )

        assert response.page == 1
        assert response.page_size == 10
        assert len(response.emails) == 0
        assert response.total == 0
