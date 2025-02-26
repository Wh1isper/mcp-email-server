from datetime import datetime
from typing import Any

from pydantic import BaseModel


class EmailData(BaseModel):
    subject: str
    sender: str
    body: str
    date: datetime
    attachments: list[str]

    @classmethod
    def from_email(cls, email: dict[str, Any]):
        return cls(
            subject=email["subject"],
            sender=email["from"],
            body=email["body"],
            date=email["date"],
            attachments=email["attachments"],
        )


class EmailPageResponse(BaseModel):
    page: int
    page_size: int
    before: datetime | None
    since: datetime | None
    subject: str | None
    body: str | None
    text: str | None
    emails: list[EmailData]
    total: int
