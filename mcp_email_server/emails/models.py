from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from mcp_email_server.application.limits import APPLICATION_LIMITS

EmailTag = Annotated[str, Field(max_length=APPLICATION_LIMITS.flag_bytes)]


class EmailMetadata(BaseModel):
    """Email metadata"""

    email_id: str
    message_id: str | None = None  # RFC 5322 Message-ID header for reply threading
    subject: str
    sender: str
    recipients: list[str]  # Recipient list
    date: datetime
    attachments: list[str]
    provider_keywords: list[EmailTag] = Field(default_factory=list, max_length=APPLICATION_LIMITS.flags)
    semantic_tags: list[EmailTag] = Field(default_factory=list, max_length=APPLICATION_LIMITS.flags)

    @classmethod
    def from_email(cls, email: dict[str, Any]):
        return cls(
            email_id=email["email_id"],
            message_id=email.get("message_id"),
            subject=email["subject"],
            sender=email["from"],
            recipients=email.get("to", []),
            date=email["date"],
            attachments=email["attachments"],
            provider_keywords=[
                flag for flag in email.get("_flags", []) if isinstance(flag, str) and not flag.startswith("\\")
            ],
        )


class EmailMetadataPageResponse(BaseModel):
    """Paged email metadata response"""

    page: int
    page_size: int
    before: datetime | None
    since: datetime | None
    subject: str | None
    emails: list[EmailMetadata]
    total: int
    warnings: list[Literal["projection_write_failed"]] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )


class EmailBodyResponse(EmailMetadata):
    """Single email body response with body content and reply-thread headers."""

    in_reply_to: str | None = None
    references: str | None = None
    body: str


class EmailContentBatchResponse(BaseModel):
    """Batch content, optionally handed off as a private local JSON artifact."""

    emails: list[EmailBodyResponse]
    requested_count: int
    retrieved_count: int
    failed_ids: list[str]
    content_omitted: bool = False
    output_file_path: str | None = None
    output_media_type: str | None = None
    output_bytes: int | None = None
    output_sha256: str | None = None
    output_lifetime: str | None = None


class MailboxInfo(BaseModel):
    """IMAP mailbox/folder information"""

    name: str
    delimiter: str
    flags: list[str]


class AttachmentDownloadResponse(BaseModel):
    """Attachment download response"""

    email_id: str
    attachment_name: str
    mime_type: str
    size: int
    saved_path: str
