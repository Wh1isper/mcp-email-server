import email.utils
import mimetypes
import ssl
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.policy import default
from html import escape
from pathlib import Path
from typing import Any

import aioimaplib
import aiosmtplib
import justhtml

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails import EmailHandler
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailBodyResponse,
    EmailContentBatchResponse,
    EmailMetadata,
    EmailMetadataPageResponse,
    ForwardEmailResponse,
)
from mcp_email_server.log import logger


def _quote_mailbox(mailbox: str) -> str:
    """Quote mailbox name for IMAP compatibility.

    Some IMAP servers (notably Proton Mail Bridge) require mailbox names
    to be quoted. This is valid per RFC 3501 and works with all IMAP servers.

    Per RFC 3501 Section 9 (Formal Syntax), quoted strings must escape
    backslashes and double-quote characters with a preceding backslash.

    See: https://github.com/ai-zerolab/mcp-email-server/issues/87
    See: https://www.rfc-editor.org/rfc/rfc3501#section-9
    """
    # Per RFC 3501, literal double-quote characters in a quoted string must
    # be escaped with a backslash. Backslashes themselves must also be escaped.
    escaped = mailbox.replace("\\", "\\\\").replace('"', r"\"")
    return f'"{escaped}"'


async def _send_imap_id(imap: aioimaplib.IMAP4 | aioimaplib.IMAP4_SSL) -> None:
    """Send IMAP ID command with fallback for strict servers like 163.com.

    aioimaplib's id() method sends ID command with spaces between parentheses
    and content (e.g., 'ID ( "name" "value" )'), which some strict IMAP servers
    like 163.com reject with 'BAD Parse command error'.

    This function first tries the standard id() method, and if it fails,
    falls back to sending a raw command with correct format.

    See: https://github.com/ai-zerolab/mcp-email-server/issues/85
    """
    try:
        response = await imap.id(name="mcp-email-server", version="1.0.0")
        if response.result != "OK":
            # Fallback for strict servers (e.g., 163.com)
            # Send raw command with correct parenthesis format
            await imap.protocol.execute(
                aioimaplib.Command(
                    "ID",
                    imap.protocol.new_tag(),
                    '("name" "mcp-email-server" "version" "1.0.0")',
                )
            )
    except Exception as e:
        logger.warning(f"IMAP ID command failed: {e!s}")


def _create_smtp_ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    """Create SSL context for SMTP connections.

    Returns None for default verification, or permissive context
    for self-signed certificates when verify_ssl=False.
    """
    if verify_ssl:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class EmailClient:
    def __init__(self, email_server: EmailServer, sender: str | None = None):
        self.email_server = email_server
        self.sender = sender or email_server.user_name

        self.imap_class = aioimaplib.IMAP4_SSL if self.email_server.use_ssl else aioimaplib.IMAP4

        self.smtp_use_tls = self.email_server.use_ssl
        self.smtp_start_tls = self.email_server.start_ssl
        self.smtp_verify_ssl = self.email_server.verify_ssl

    def _get_smtp_ssl_context(self) -> ssl.SSLContext | None:
        """Get SSL context for SMTP connections based on verify_ssl setting."""
        return _create_smtp_ssl_context(self.smtp_verify_ssl)

    def _parse_email_data(self, raw_email: bytes, email_id: str | None = None) -> dict[str, Any]:  # noqa: C901
        """Parse raw email data into a structured dictionary."""
        parser = BytesParser(policy=default)
        email_message = parser.parsebytes(raw_email)

        # Extract email parts
        subject = email_message.get("Subject", "")
        sender = email_message.get("From", "")
        date_str = email_message.get("Date", "")

        # Extract Message-ID for reply threading
        message_id = email_message.get("Message-ID")

        # Extract recipients
        to_addresses = []
        to_header = email_message.get("To", "")
        if to_header:
            # Simple parsing - split by comma and strip whitespace
            to_addresses = [addr.strip() for addr in to_header.split(",")]

        # Also check CC recipients
        cc_header = email_message.get("Cc", "")
        if cc_header:
            to_addresses.extend([addr.strip() for addr in cc_header.split(",")])

        # Parse date
        try:
            date_tuple = email.utils.parsedate_tz(date_str)
            date = (
                datetime.fromtimestamp(email.utils.mktime_tz(date_tuple), tz=timezone.utc)
                if date_tuple
                else datetime.now(timezone.utc)
            )
        except Exception:
            date = datetime.now(timezone.utc)

        # Get body content
        body = ""
        attachments = []

        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                # Handle attachments
                if "attachment" in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        attachments.append(filename)
                # Handle text parts
                elif content_type == "text/plain":
                    body_part = part.get_payload(decode=True)
                    if body_part:
                        charset = part.get_content_charset("utf-8")
                        try:
                            body += body_part.decode(charset)
                        except UnicodeDecodeError:
                            body += body_part.decode("utf-8", errors="replace")
        else:
            # Handle plain text emails
            payload = email_message.get_payload(decode=True)
            if payload:
                charset = email_message.get_content_charset("utf-8")
                try:
                    body = payload.decode(charset)
                except UnicodeDecodeError:
                    body = payload.decode("utf-8", errors="replace")
        # TODO: Allow retrieving full email body
        if body and len(body) > 20000:
            body = body[:20000] + "...[TRUNCATED]"
        return {
            "email_id": email_id or "",
            "message_id": message_id,
            "subject": subject,
            "from": sender,
            "to": to_addresses,
            "body": body,
            "date": date,
            "attachments": attachments,
        }

    @staticmethod
    def _build_search_criteria(
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        body: str | None = None,
        text: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
    ):
        search_criteria = []
        if before:
            search_criteria.extend(["BEFORE", before.strftime("%d-%b-%Y").upper()])
        if since:
            search_criteria.extend(["SINCE", since.strftime("%d-%b-%Y").upper()])
        if subject:
            search_criteria.extend(["SUBJECT", subject])
        if body:
            search_criteria.extend(["BODY", body])
        if text:
            search_criteria.extend(["TEXT", text])
        if from_address:
            search_criteria.extend(["FROM", from_address])
        if to_address:
            search_criteria.extend(["TO", to_address])

        # If no specific criteria, search for ALL
        if not search_criteria:
            search_criteria = ["ALL"]

        return search_criteria

    async def get_email_count(
        self,
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        mailbox: str = "INBOX",
    ) -> int:
        imap = self.imap_class(self.email_server.host, self.email_server.port)
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select inbox
            await imap.login(self.email_server.user_name, self.email_server.password)
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))
            search_criteria = self._build_search_criteria(
                before, since, subject, from_address=from_address, to_address=to_address
            )
            logger.info(f"Count: Search criteria: {search_criteria}")
            # Search for messages and count them - use UID SEARCH for consistency
            _, messages = await imap.uid_search(*search_criteria)
            return len(messages[0].split())
        finally:
            # Ensure we logout properly
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    async def get_emails_metadata_stream(  # noqa: C901
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
    ) -> AsyncGenerator[dict[str, Any], None]:
        imap = self.imap_class(self.email_server.host, self.email_server.port)
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select inbox
            await imap.login(self.email_server.user_name, self.email_server.password)
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            search_criteria = self._build_search_criteria(
                before, since, subject, from_address=from_address, to_address=to_address
            )
            logger.info(f"Get metadata: Search criteria: {search_criteria}")

            # Search for messages - use UID SEARCH for better compatibility
            _, messages = await imap.uid_search(*search_criteria)

            # Handle empty or None responses
            if not messages or not messages[0]:
                logger.warning("No messages returned from search")
                email_ids = []
            else:
                email_ids = messages[0].split()
                logger.info(f"Found {len(email_ids)} email IDs")
            # Fetch metadata for all emails first, then sort by date
            # (UID order doesn't guarantee chronological order on all IMAP servers)
            all_metadata: list[dict[str, Any]] = []

            # Fetch each message's metadata only
            for _, email_id in enumerate(email_ids):
                try:
                    # Convert email_id from bytes to string
                    email_id_str = email_id.decode("utf-8")

                    # Fetch only headers to get metadata without body
                    _, data = await imap.uid("fetch", email_id_str, "BODY.PEEK[HEADER]")

                    if not data:
                        logger.error(f"Failed to fetch headers for UID {email_id_str}")
                        continue

                    # Find the email headers in the response
                    raw_headers = None
                    if len(data) > 1 and isinstance(data[1], bytearray):
                        raw_headers = bytes(data[1])
                    else:
                        # Search through all items for header content
                        for item in data:
                            if isinstance(item, bytes | bytearray) and len(item) > 10:
                                # Skip IMAP protocol responses
                                if isinstance(item, bytes) and b"FETCH" in item:
                                    continue
                                # This is likely the header content
                                raw_headers = bytes(item) if isinstance(item, bytearray) else item
                                break

                    if raw_headers:
                        try:
                            # Parse headers only
                            parser = BytesParser(policy=default)
                            email_message = parser.parsebytes(raw_headers)

                            # Extract metadata
                            subject = email_message.get("Subject", "")
                            sender = email_message.get("From", "")
                            date_str = email_message.get("Date", "")

                            # Extract recipients
                            to_addresses = []
                            to_header = email_message.get("To", "")
                            if to_header:
                                to_addresses = [addr.strip() for addr in to_header.split(",")]

                            cc_header = email_message.get("Cc", "")
                            if cc_header:
                                to_addresses.extend([addr.strip() for addr in cc_header.split(",")])

                            # Parse date
                            try:
                                date_tuple = email.utils.parsedate_tz(date_str)
                                date = (
                                    datetime.fromtimestamp(email.utils.mktime_tz(date_tuple), tz=timezone.utc)
                                    if date_tuple
                                    else datetime.now(timezone.utc)
                                )
                            except Exception:
                                date = datetime.now(timezone.utc)

                            # For metadata, we don't fetch attachments to save bandwidth
                            # We'll mark it as unknown for now
                            metadata = {
                                "email_id": email_id_str,
                                "subject": subject,
                                "from": sender,
                                "to": to_addresses,
                                "date": date,
                                "attachments": [],  # We don't fetch attachment info for metadata
                            }
                            all_metadata.append(metadata)
                        except Exception as e:
                            # Log error but continue with other emails
                            logger.error(f"Error parsing email metadata: {e!s}")
                    else:
                        logger.error(f"Could not find header data in response for email ID: {email_id_str}")
                except Exception as e:
                    logger.error(f"Error fetching email metadata {email_id}: {e!s}")

            # Sort by date (desc = newest first, asc = oldest first)
            all_metadata.sort(key=lambda x: x["date"], reverse=(order == "desc"))

            # Apply pagination after sorting
            start = (page - 1) * page_size
            end = start + page_size
            for metadata in all_metadata[start:end]:
                yield metadata
        finally:
            # Ensure we logout properly
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    def _check_email_content(self, data: list) -> bool:
        """Check if the fetched data contains actual email content."""
        for item in data:
            if isinstance(item, bytes) and b"FETCH (" in item and b"RFC822" not in item and b"BODY" not in item:
                # This is just metadata, not actual content
                continue
            elif isinstance(item, bytes | bytearray) and len(item) > 100:
                # This looks like email content
                return True
        return False

    def _extract_raw_email(self, data: list) -> bytes | None:
        """Extract raw email bytes from IMAP response data."""
        # The email content is typically at index 1 as a bytearray
        if len(data) > 1 and isinstance(data[1], bytearray):
            return bytes(data[1])

        # Search through all items for email content
        for item in data:
            if isinstance(item, bytes | bytearray) and len(item) > 100:
                # Skip IMAP protocol responses
                if isinstance(item, bytes) and b"FETCH" in item:
                    continue
                # This is likely the email content
                return bytes(item) if isinstance(item, bytearray) else item
        return None

    async def _fetch_email_with_formats(self, imap, email_id: str) -> list | None:
        """Try different fetch formats to get email data."""
        fetch_formats = ["RFC822", "BODY[]", "BODY.PEEK[]", "(BODY.PEEK[])"]

        for fetch_format in fetch_formats:
            try:
                _, data = await imap.uid("fetch", email_id, fetch_format)

                if data and len(data) > 0 and self._check_email_content(data):
                    return data

            except Exception as e:
                logger.debug(f"Fetch format {fetch_format} failed: {e}")

        return None

    async def get_email_body_by_id(self, email_id: str, mailbox: str = "INBOX") -> dict[str, Any] | None:
        imap = self.imap_class(self.email_server.host, self.email_server.port)
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select inbox
            await imap.login(self.email_server.user_name, self.email_server.password)
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            # Fetch the specific email by UID
            data = await self._fetch_email_with_formats(imap, email_id)
            if not data:
                logger.error(f"Failed to fetch UID {email_id} with any format")
                return None

            # Extract raw email data
            raw_email = self._extract_raw_email(data)
            if not raw_email:
                logger.error(f"Could not find email data in response for email ID: {email_id}")
                return None

            # Parse the email
            try:
                return self._parse_email_data(raw_email, email_id)
            except Exception as e:
                logger.error(f"Error parsing email: {e!s}")
                return None

        finally:
            # Ensure we logout properly
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    async def download_attachment(
        self,
        email_id: str,
        attachment_name: str,
        save_path: str,
        mailbox: str = "INBOX",
    ) -> dict[str, Any]:
        """Download a specific attachment from an email and save it to disk.

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            save_path: The local path where the attachment will be saved.
            mailbox: The mailbox to search in (default: "INBOX").

        Returns:
            A dictionary with download result information.
        """
        imap = self.imap_class(self.email_server.host, self.email_server.port)
        try:
            await imap._client_task
            await imap.wait_hello_from_server()

            await imap.login(self.email_server.user_name, self.email_server.password)
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            data = await self._fetch_email_with_formats(imap, email_id)
            if not data:
                msg = f"Failed to fetch email with UID {email_id}"
                logger.error(msg)
                raise ValueError(msg)

            raw_email = self._extract_raw_email(data)
            if not raw_email:
                msg = f"Could not find email data for email ID: {email_id}"
                logger.error(msg)
                raise ValueError(msg)

            parser = BytesParser(policy=default)
            email_message = parser.parsebytes(raw_email)

            # Find the attachment
            attachment_data = None
            mime_type = None

            if email_message.is_multipart():
                for part in email_message.walk():
                    content_disposition = str(part.get("Content-Disposition", ""))
                    if "attachment" in content_disposition:
                        filename = part.get_filename()
                        if filename == attachment_name:
                            attachment_data = part.get_payload(decode=True)
                            mime_type = part.get_content_type()
                            break

            if attachment_data is None:
                msg = f"Attachment '{attachment_name}' not found in email {email_id}"
                logger.error(msg)
                raise ValueError(msg)

            # Save to disk
            save_file = Path(save_path)
            save_file.parent.mkdir(parents=True, exist_ok=True)
            save_file.write_bytes(attachment_data)

            logger.info(f"Attachment '{attachment_name}' saved to {save_path}")

            return {
                "email_id": email_id,
                "attachment_name": attachment_name,
                "mime_type": mime_type or "application/octet-stream",
                "size": len(attachment_data),
                "saved_path": str(save_file.resolve()),
            }

        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    def _validate_attachment(self, file_path: str) -> Path:
        """Validate attachment file path."""
        path = Path(file_path)
        if not path.exists():
            msg = f"Attachment file not found: {file_path}"
            logger.error(msg)
            raise FileNotFoundError(msg)

        if not path.is_file():
            msg = f"Attachment path is not a file: {file_path}"
            logger.error(msg)
            raise ValueError(msg)

        return path

    def _create_attachment_part(self, path: Path) -> MIMEApplication:
        """Create MIME attachment part from file."""
        with open(path, "rb") as f:
            file_data = f.read()

        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None:
            mime_type = "application/octet-stream"

        attachment_part = MIMEApplication(file_data, _subtype=mime_type.split("/")[1])
        attachment_part.add_header(
            "Content-Disposition",
            "attachment",
            filename=path.name,
        )
        logger.info(f"Attached file: {path.name} ({mime_type})")
        return attachment_part

    def _create_message_with_attachments(self, body: str, html: bool, attachments: list[str]) -> MIMEMultipart:
        """Create multipart message with attachments."""
        msg = MIMEMultipart()
        content_type = "html" if html else "plain"
        text_part = MIMEText(body, content_type, "utf-8")
        msg.attach(text_part)

        for file_path in attachments:
            try:
                path = self._validate_attachment(file_path)
                attachment_part = self._create_attachment_part(path)
                msg.attach(attachment_part)
            except Exception as e:
                logger.error(f"Failed to attach file {file_path}: {e}")
                raise

        return msg

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
    ):
        # Create message with or without attachments
        if attachments:
            msg = self._create_message_with_attachments(body, html, attachments)
        else:
            content_type = "html" if html else "plain"
            msg = MIMEText(body, content_type, "utf-8")

        # Handle subject with special characters
        if any(ord(c) > 127 for c in subject):
            msg["Subject"] = Header(subject, "utf-8")
        else:
            msg["Subject"] = subject

        # Handle sender name with special characters
        if any(ord(c) > 127 for c in self.sender):
            msg["From"] = Header(self.sender, "utf-8")
        else:
            msg["From"] = self.sender

        msg["To"] = ", ".join(recipients)

        # Add CC header if provided (visible to recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)

        # Set threading headers for replies
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references

        # Note: BCC recipients are not added to headers (they remain hidden)
        # but will be included in the actual recipients for SMTP delivery

        async with aiosmtplib.SMTP(
            hostname=self.email_server.host,
            port=self.email_server.port,
            start_tls=self.smtp_start_tls,
            use_tls=self.smtp_use_tls,
            tls_context=self._get_smtp_ssl_context(),
        ) as smtp:
            await smtp.login(self.email_server.user_name, self.email_server.password)

            # Create a combined list of all recipients for delivery
            all_recipients = recipients.copy()
            if cc:
                all_recipients.extend(cc)
            if bcc:
                all_recipients.extend(bcc)

            await smtp.send_message(msg, recipients=all_recipients)

        # Return the message for potential saving to Sent folder
        return msg

    async def _find_sent_folder_by_flag(self, imap) -> str | None:
        """Find the Sent folder by searching for the \\Sent IMAP flag.

        Args:
            imap: Connected IMAP client

        Returns:
            The folder name with the \\Sent flag, or None if not found
        """
        try:
            # List all folders - aioimaplib requires reference_name and mailbox_pattern
            _, folders = await imap.list('""', "*")

            # Search for folder with \Sent flag
            for folder in folders:
                folder_str = folder.decode("utf-8") if isinstance(folder, bytes) else str(folder)
                # IMAP LIST response format: (flags) "delimiter" "name"
                # Example: (\Sent \HasNoChildren) "/" "Gesendete Objekte"
                if r"\Sent" in folder_str or "\\Sent" in folder_str:
                    # Extract folder name from the response
                    # Split by quotes and get the last quoted part
                    parts = folder_str.split('"')
                    if len(parts) >= 3:
                        folder_name = parts[-2]  # The folder name is the second-to-last quoted part
                        logger.info(f"Found Sent folder by \\Sent flag: '{folder_name}'")
                        return folder_name
        except Exception as e:
            logger.debug(f"Error finding Sent folder by flag: {e}")

        return None

    async def append_to_sent(
        self,
        msg: MIMEText | MIMEMultipart,
        incoming_server: EmailServer,
        sent_folder_name: str | None = None,
    ) -> bool:
        """Append a sent message to the IMAP Sent folder.

        Args:
            msg: The email message that was sent
            incoming_server: IMAP server configuration for accessing Sent folder
            sent_folder_name: Override folder name, or None for auto-detection

        Returns:
            True if successfully saved, False otherwise
        """
        imap_class = aioimaplib.IMAP4_SSL if incoming_server.use_ssl else aioimaplib.IMAP4
        imap = imap_class(incoming_server.host, incoming_server.port)

        # Common Sent folder names across different providers
        sent_folder_candidates = [
            sent_folder_name,  # User-specified override (if provided)
            "Sent",
            "INBOX.Sent",
            "Sent Items",
            "Sent Mail",
            "[Gmail]/Sent Mail",
            "INBOX/Sent",
        ]
        # Filter out None values
        sent_folder_candidates = [f for f in sent_folder_candidates if f]

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(incoming_server.user_name, incoming_server.password)
            await _send_imap_id(imap)

            # Try to find Sent folder by IMAP \Sent flag first
            flag_folder = await self._find_sent_folder_by_flag(imap)
            if flag_folder and flag_folder not in sent_folder_candidates:
                # Add it at the beginning (high priority)
                sent_folder_candidates.insert(0, flag_folder)

            # Try to find and use the Sent folder
            for folder in sent_folder_candidates:
                try:
                    logger.debug(f"Trying Sent folder: '{folder}'")
                    # Try to select the folder to verify it exists
                    result = await imap.select(_quote_mailbox(folder))
                    logger.debug(f"Select result for '{folder}': {result}")

                    # aioimaplib returns (status, data) where status is a string like 'OK' or 'NO'
                    status = result[0] if isinstance(result, tuple) else result
                    if str(status).upper() == "OK":
                        # Folder exists, append the message
                        msg_bytes = msg.as_bytes()
                        logger.debug(f"Appending message to '{folder}'")
                        # aioimaplib.append signature: (message_bytes, mailbox, flags, date)
                        append_result = await imap.append(
                            msg_bytes,
                            mailbox=_quote_mailbox(folder),
                            flags=r"(\Seen)",
                        )
                        logger.debug(f"Append result: {append_result}")
                        append_status = append_result[0] if isinstance(append_result, tuple) else append_result
                        if str(append_status).upper() == "OK":
                            logger.info(f"Saved sent email to '{folder}'")
                            return True
                        else:
                            logger.warning(f"Failed to append to '{folder}': {append_status}")
                    else:
                        logger.debug(f"Folder '{folder}' select returned: {status}")
                except Exception as e:
                    logger.debug(f"Folder '{folder}' not available: {e}")
                    continue

            logger.warning("Could not find a valid Sent folder to save the message")
            return False

        except Exception as e:
            logger.error(f"Error saving to Sent folder: {e}")
            return False
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.debug(f"Error during logout: {e}")

    async def delete_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids)."""
        imap = self.imap_class(self.email_server.host, self.email_server.port)
        deleted_ids = []
        failed_ids = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(self.email_server.user_name, self.email_server.password)
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            for email_id in email_ids:
                try:
                    await imap.uid("store", email_id, "+FLAGS", r"(\Deleted)")
                    deleted_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to delete email {email_id}: {e}")
                    failed_ids.append(email_id)

            await imap.expunge()
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return deleted_ids, failed_ids

    async def get_email_for_forward(self, email_id: str, mailbox: str = "INBOX") -> dict[str, Any] | None:
        """Fetch email content and MIME parts needed for forwarding.

        Args:
            email_id: The UID of the email to forward.
            mailbox: The mailbox containing the email.

        Returns:
            Dictionary with email metadata, body, and attachment MIME parts.
        """
        imap = self.imap_class(self.email_server.host, self.email_server.port)
        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(self.email_server.user_name, self.email_server.password)
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            data = await self._fetch_email_with_formats(imap, email_id)
            if not data:
                logger.error(f"Failed to fetch UID {email_id} for forwarding")
                return None

            raw_email = self._extract_raw_email(data)
            if not raw_email:
                logger.error(f"Could not find email data for forward, email ID: {email_id}")
                return None

            # Parse the email
            parser = BytesParser(policy=default)
            email_message = parser.parsebytes(raw_email)

            # Extract basic metadata
            subject = email_message.get("Subject", "")
            sender = email_message.get("From", "")
            date_str = email_message.get("Date", "")
            to_header = email_message.get("To", "")
            cc_header = email_message.get("Cc", "")

            # Extract body (plain text and HTML) and attachments
            body = ""
            html_body = ""
            attachment_parts: list[MIMEApplication] = []

            if email_message.is_multipart():
                for part in email_message.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition", ""))

                    if "attachment" in content_disposition:
                        # Extract attachment as MIME part
                        filename = part.get_filename()
                        if filename:
                            payload = part.get_payload(decode=True)
                            if payload:
                                mime_type = part.get_content_type()
                                subtype = mime_type.split("/")[1] if "/" in mime_type else "octet-stream"
                                attachment = MIMEApplication(payload, _subtype=subtype)
                                attachment.add_header(
                                    "Content-Disposition",
                                    "attachment",
                                    filename=filename,
                                )
                                attachment_parts.append(attachment)
                    elif content_type == "text/plain":
                        body_part = part.get_payload(decode=True)
                        if body_part:
                            charset = part.get_content_charset("utf-8")
                            try:
                                body += body_part.decode(charset)
                            except UnicodeDecodeError:
                                body += body_part.decode("utf-8", errors="replace")
                    elif content_type == "text/html":
                        html_part = part.get_payload(decode=True)
                        if html_part:
                            charset = part.get_content_charset("utf-8")
                            try:
                                html_body += html_part.decode(charset)
                            except UnicodeDecodeError:
                                html_body += html_part.decode("utf-8", errors="replace")
            else:
                payload = email_message.get_payload(decode=True)
                if payload:
                    charset = email_message.get_content_charset("utf-8")
                    content_type = email_message.get_content_type()
                    try:
                        decoded = payload.decode(charset)
                    except UnicodeDecodeError:
                        decoded = payload.decode("utf-8", errors="replace")

                    if content_type == "text/html":
                        html_body = decoded
                    else:
                        body = decoded

            # Use HTML only when there's no plain text alternative
            is_html = bool(html_body and not body)

            return {
                "email_id": email_id,
                "subject": subject,
                "from": sender,
                "to": to_header,
                "cc": cc_header,
                "date": date_str,
                "body": body,
                "html_body": html_body,
                "is_html": is_html,
                "attachment_parts": attachment_parts,
            }

        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")


class ClassicEmailHandler(EmailHandler):
    def __init__(self, email_settings: EmailSettings):
        self.email_settings = email_settings
        self.incoming_client = EmailClient(email_settings.incoming)
        self.outgoing_client = EmailClient(
            email_settings.outgoing,
            sender=f"{email_settings.full_name} <{email_settings.email_address}>",
        )
        self.save_to_sent = email_settings.save_to_sent
        self.sent_folder_name = email_settings.sent_folder_name

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
    ) -> EmailMetadataPageResponse:
        emails = []
        async for email_data in self.incoming_client.get_emails_metadata_stream(
            page, page_size, before, since, subject, from_address, to_address, order, mailbox
        ):
            emails.append(EmailMetadata.from_email(email_data))
        total = await self.incoming_client.get_email_count(
            before, since, subject, from_address=from_address, to_address=to_address, mailbox=mailbox
        )
        return EmailMetadataPageResponse(
            page=page,
            page_size=page_size,
            before=before,
            since=since,
            subject=subject,
            emails=emails,
            total=total,
        )

    async def get_emails_content(self, email_ids: list[str], mailbox: str = "INBOX") -> EmailContentBatchResponse:
        """Batch retrieve email body content"""
        emails = []
        failed_ids = []

        for email_id in email_ids:
            try:
                email_data = await self.incoming_client.get_email_body_by_id(email_id, mailbox)
                if email_data:
                    emails.append(
                        EmailBodyResponse(
                            email_id=email_data["email_id"],
                            message_id=email_data.get("message_id"),
                            subject=email_data["subject"],
                            sender=email_data["from"],
                            recipients=email_data["to"],
                            date=email_data["date"],
                            body=email_data["body"],
                            attachments=email_data["attachments"],
                        )
                    )
                else:
                    failed_ids.append(email_id)
            except Exception as e:
                logger.error(f"Failed to retrieve email {email_id}: {e}")
                failed_ids.append(email_id)

        return EmailContentBatchResponse(
            emails=emails,
            requested_count=len(email_ids),
            retrieved_count=len(emails),
            failed_ids=failed_ids,
        )

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
        msg = await self.outgoing_client.send_email(
            recipients, subject, body, cc, bcc, html, attachments, in_reply_to, references
        )

        # Save to Sent folder if enabled
        if self.save_to_sent and msg:
            try:
                await self.outgoing_client.append_to_sent(
                    msg,
                    self.email_settings.incoming,
                    self.sent_folder_name,
                )
            except Exception as e:
                logger.error(f"Failed to save email to Sent folder: {e}", exc_info=True)

    async def delete_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids)."""
        return await self.incoming_client.delete_emails(email_ids, mailbox)

    async def download_attachment(
        self,
        email_id: str,
        attachment_name: str,
        save_path: str,
        mailbox: str = "INBOX",
    ) -> AttachmentDownloadResponse:
        """Download an email attachment and save it to the specified path.

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            save_path: The local path where the attachment will be saved.
            mailbox: The mailbox to search in (default: "INBOX").

        Returns:
            AttachmentDownloadResponse with download result information.
        """
        result = await self.incoming_client.download_attachment(email_id, attachment_name, save_path, mailbox)
        return AttachmentDownloadResponse(
            email_id=result["email_id"],
            attachment_name=result["attachment_name"],
            mime_type=result["mime_type"],
            size=result["size"],
            saved_path=result["saved_path"],
        )

    async def forward_email(
        self,
        email_id: str,
        recipients: list[str],
        from_address: str | None = None,
        additional_message: str | None = None,
        include_attachments: bool = True,
        mailbox: str = "INBOX",
    ) -> ForwardEmailResponse:
        """Forward an email to new recipients.

        Args:
            email_id: The UID of the email to forward.
            recipients: List of recipient email addresses.
            from_address: Override sender address (uses account default if None).
            additional_message: Optional message to prepend to the forwarded email.
            include_attachments: Whether to include original attachments.
            mailbox: The mailbox containing the email.

        Returns:
            ForwardEmailResponse with forward result information.
        """
        # Determine sender address
        sender = from_address or f"{self.email_settings.full_name} <{self.email_settings.email_address}>"

        # Get the original email from incoming server (IMAP)
        original = await self.incoming_client.get_email_for_forward(email_id, mailbox)
        if not original:
            return ForwardEmailResponse(
                original_email_id=email_id,
                forwarded_to=recipients,
                from_address=sender,
                subject="",
                success=False,
                message=f"Could not retrieve email {email_id} for forwarding",
            )

        # Build the forward subject
        original_subject = original["subject"]
        if not original_subject.lower().startswith("fwd:"):
            forward_subject = f"Fwd: {original_subject}"
        else:
            forward_subject = original_subject

        # Determine if original is HTML
        is_html = original.get("is_html", False)
        html_body = original.get("html_body", "")
        plain_body = original.get("body", "")

        # For plain text forwarding, prefer converting HTML to clean text
        # This avoids issues where plain_body contains embedded HTML tags
        if not is_html and html_body:
            plain_body = justhtml.JustHTML(html_body).to_text()
        elif is_html and not plain_body and html_body:
            # HTML-only email, need plain text fallback
            plain_body = justhtml.JustHTML(html_body).to_text()

        # Build attachment file paths (we need to temporarily save them for send_email)
        # For simplicity, we'll send without file attachments and include the attachment parts directly
        attachment_parts = original.get("attachment_parts", []) if include_attachments else []

        try:
            if is_html and html_body:
                # Build HTML forward
                quoted_html = "<br><br>---------- Forwarded message ----------<br>"
                quoted_html += f"From: {escape(original['from'])}<br>"
                quoted_html += f"Date: {escape(original['date'])}<br>"
                quoted_html += f"Subject: {escape(original['subject'])}<br>"
                if original["to"]:
                    quoted_html += f"To: {escape(original['to'])}<br>"
                if original["cc"]:
                    quoted_html += f"Cc: {escape(original['cc'])}<br>"
                quoted_html += f"<br>{html_body}"

                if additional_message:
                    forward_html = f"<p>{escape(additional_message)}</p>{quoted_html}"
                else:
                    forward_html = quoted_html

                if attachment_parts:
                    msg = MIMEMultipart()
                    html_part = MIMEText(forward_html, "html", "utf-8")
                    msg.attach(html_part)
                    for attachment in attachment_parts:
                        msg.attach(attachment)
                else:
                    msg = MIMEText(forward_html, "html", "utf-8")
            else:
                # Build plain text forward
                quoted_body = "\n\n---------- Forwarded message ----------\n"
                quoted_body += f"From: {original['from']}\n"
                quoted_body += f"Date: {original['date']}\n"
                quoted_body += f"Subject: {original['subject']}\n"
                if original["to"]:
                    quoted_body += f"To: {original['to']}\n"
                if original["cc"]:
                    quoted_body += f"Cc: {original['cc']}\n"
                quoted_body += "\n"
                quoted_body += plain_body

                if additional_message:
                    forward_body = additional_message + quoted_body
                else:
                    forward_body = quoted_body

                if attachment_parts:
                    msg = MIMEMultipart()
                    text_part = MIMEText(forward_body, "plain", "utf-8")
                    msg.attach(text_part)
                    for attachment in attachment_parts:
                        msg.attach(attachment)
                else:
                    msg = MIMEText(forward_body, "plain", "utf-8")

            # Set headers
            if any(ord(c) > 127 for c in forward_subject):
                msg["Subject"] = Header(forward_subject, "utf-8")
            else:
                msg["Subject"] = forward_subject

            if any(ord(c) > 127 for c in sender):
                msg["From"] = Header(sender, "utf-8")
            else:
                msg["From"] = sender

            msg["To"] = ", ".join(recipients)
            msg["Date"] = email.utils.formatdate(localtime=True)

            # Send via SMTP using outgoing server
            async with aiosmtplib.SMTP(
                hostname=self.email_settings.outgoing.host,
                port=self.email_settings.outgoing.port,
                start_tls=self.email_settings.outgoing.start_ssl,
                use_tls=self.email_settings.outgoing.use_ssl,
            ) as smtp:
                await smtp.login(
                    self.email_settings.outgoing.user_name,
                    self.email_settings.outgoing.password,
                )
                await smtp.send_message(msg, recipients=recipients)

            # Save to Sent folder if enabled
            if self.save_to_sent:
                try:
                    await self.outgoing_client.append_to_sent(
                        msg,
                        self.email_settings.incoming,
                        self.sent_folder_name,
                    )
                except Exception as e:
                    logger.error(f"Failed to save forwarded email to Sent folder: {e}", exc_info=True)

            return ForwardEmailResponse(
                original_email_id=email_id,
                forwarded_to=recipients,
                from_address=sender,
                subject=forward_subject,
                success=True,
                message=f"Email forwarded successfully to {', '.join(recipients)}",
            )

        except Exception as e:
            logger.error(f"Failed to forward email {email_id}: {e}", exc_info=True)
            return ForwardEmailResponse(
                original_email_id=email_id,
                forwarded_to=recipients,
                from_address=sender,
                subject=forward_subject if "forward_subject" in dir() else "",
                success=False,
                message=f"Failed to forward email: {e!s}",
            )
