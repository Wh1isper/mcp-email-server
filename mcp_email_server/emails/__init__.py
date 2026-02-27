import abc
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_email_server.emails.models import (
        AttachmentDownloadResponse,
        EmailContentBatchResponse,
        EmailMetadataPageResponse,
    )


class EmailHandler(abc.ABC):
    @abc.abstractmethod
    async def get_emails_metadata(
        self,
        page: int = 1,
        page_size: int = 10,
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        order: str = "desc",
        mailbox: str = "INBOX",
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
    ) -> "EmailMetadataPageResponse":
        """
        Get email metadata only (without body content) for better performance.

        Args:
            page: Page number (starting from 1).
            page_size: Number of emails per page.
            before: Filter emails before this datetime.
            since: Filter emails since this datetime.
            subject: Filter by subject (substring match).
            from_address: Filter by sender address.
            to_address: Filter by recipient address.
            order: Sort order ('asc' or 'desc').
            mailbox: Mailbox to search (default: 'INBOX').
            seen: Filter by read status (True=read, False=unread, None=all).
            flagged: Filter by flagged/starred status (True=flagged, False=unflagged, None=all).
            answered: Filter by replied status (True=replied, False=not replied, None=all).
        """

    @abc.abstractmethod
    async def get_emails_content(self, email_ids: list[str], mailbox: str = "INBOX") -> "EmailContentBatchResponse":
        """
        Get full content (including body) of multiple emails by their email IDs (IMAP UIDs)
        """

    @abc.abstractmethod
    async def send_email(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: bool = False,
        attachments: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> None:
        """
        Send email

        Args:
            recipients: List of recipient email addresses.
            subject: Email subject.
            body: Email body content.
            cc: List of CC email addresses.
            bcc: List of BCC email addresses.
            html: Whether to send as HTML (True) or plain text (False).
            attachments: List of file paths to attach.
            in_reply_to: Message-ID of the email being replied to (for threading).
            references: Space-separated Message-IDs for the thread chain.
        """

    @abc.abstractmethod
    async def list_mailboxes(self) -> list[dict]:
        """
        List all mailboxes (folders) in the email account.

        Returns:
            List of dictionaries with mailbox info (name, flags, delimiter).
        """

    @abc.abstractmethod
    async def search_emails(
        self,
        query: str,
        mailbox: str = "INBOX",
        search_in: str = "all",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """
        Search emails using server-side IMAP SEARCH.

        Args:
            query: Text to search for.
            mailbox: Mailbox to search in (default: "INBOX").
            search_in: Where to search - "all", "subject", "body", "from".
            page: Page number (starting from 1).
            page_size: Number of results per page.

        Returns:
            Dictionary with query, total, page, and emails list.
        """

    @abc.abstractmethod
    async def delete_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """
        Delete emails by their IDs. Returns (deleted_ids, failed_ids)
        """

    @abc.abstractmethod
    async def mark_emails_as_read(
        self, email_ids: list[str], mailbox: str = "INBOX", read: bool = True
    ) -> tuple[list[str], list[str]]:
        """
        Mark emails as read or unread. Returns (success_ids, failed_ids)

        Args:
            email_ids: List of email IDs to mark.
            mailbox: The mailbox containing the emails (default: "INBOX").
            read: True to mark as read, False to mark as unread.
        """

    @abc.abstractmethod
    async def move_emails(
        self, email_ids: list[str], destination_mailbox: str, source_mailbox: str = "INBOX"
    ) -> tuple[list[str], list[str]]:
        """
        Move emails to another mailbox. Returns (moved_ids, failed_ids)

        Args:
            email_ids: List of email IDs to move.
            destination_mailbox: Target mailbox name (e.g., "Archive", "Trash").
            source_mailbox: Source mailbox (default: "INBOX").
        """

    @abc.abstractmethod
    async def download_attachment(
        self,
        email_id: str,
        attachment_name: str,
        save_path: str,
        mailbox: str = "INBOX",
    ) -> "AttachmentDownloadResponse":
        """
        Download an email attachment and save it to the specified path.

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            save_path: The local path where the attachment will be saved.
            mailbox: The mailbox to search in (default: "INBOX").

        Returns:
            AttachmentDownloadResponse with download result information.
        """
