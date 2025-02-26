import abc
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_email_server.emails.models import EmailPageResponse


class EmailHandler(abc.ABC):
    @abc.abstractmethod
    async def get_emails(
        self,
        page: int = 1,
        page_size: int = 10,
        before: datetime | None = None,
        after: datetime | None = None,
        subject: str | None = None,
        body: str | None = None,
        text: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
    ) -> "EmailPageResponse":
        """
        Get emails
        """

    @abc.abstractmethod
    async def send_email(self, recipient: str, subject: str, body: str) -> None:
        """
        Send email
        """
