import asyncio
import email.utils
import mimetypes
import re
import ssl
import time
from datetime import datetime, timezone
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any

import aioimaplib
import aiosmtplib

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails import EmailHandler
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailBodyResponse,
    EmailContentBatchResponse,
    EmailMetadata,
    EmailMetadataPageResponse,
    MailboxInfo,
)
from mcp_email_server.log import logger

# Maximum body length before truncation (characters)
MAX_BODY_LENGTH = 20000


# RFC 3501 system flags (except \Recent which is read-only) + custom keyword atoms
_VALID_IMAP_FLAG = re.compile(r"^\\[A-Za-z]+$|^[A-Za-z][A-Za-z0-9_-]*$")


def _validate_flags(flags: list[str]) -> str:
    """Validate and format IMAP flags into a parenthesised string.

    Accepts system flags (e.g. ``\\Draft``, ``\\Seen``) and custom keyword
    atoms.  Raises ``ValueError`` on anything that could inject IMAP protocol
    characters.
    """
    for flag in flags:
        if not _VALID_IMAP_FLAG.match(flag):
            msg = f"Invalid IMAP flag: {flag!r}"
            raise ValueError(msg)
    return "(" + " ".join(flags) + ")"


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


def _imap_status(response: Any) -> str:
    """Return the normalized status from an aioimaplib response."""
    if hasattr(response, "result"):
        return str(response.result).upper()
    if isinstance(response, tuple) and response:
        return str(response[0]).upper()
    return str(response).upper()


def _raise_for_imap_error(response: Any, operation: str) -> None:
    """Raise when an IMAP command returns a non-OK status."""
    if _imap_status(response) != "OK":
        msg = f"{operation} failed: {response!r}"
        raise RuntimeError(msg)


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


async def _imap_login(
    imap: aioimaplib.IMAP4 | aioimaplib.IMAP4_SSL,
    user_name: str,
    password: str,
) -> None:
    """Authenticate to IMAP and fail loudly when the server rejects credentials.

    aioimaplib's ``login()`` returns a Response with a ``.result`` of "OK",
    "NO", or "BAD". A "NO" response (e.g. wrong credentials, account locked,
    or a transient rate-limit cool-down on servers like Proton Mail Bridge)
    does NOT raise — and an unchecked caller will happily proceed to issue
    SELECT/FETCH on a NONAUTH connection, producing the misleading error
    ``command SELECT illegal in state NONAUTH``. Worse, each tool call then
    opens a fresh TCP connection and re-attempts ``LOGIN``, which amplifies
    rate-limits on servers that count failed-login attempts and locks the
    account out for tens of minutes.

    Raise immediately on a non-OK result so callers (and end users) see the
    real error and back off, and so a one-off auth failure does not cascade
    into a multi-minute lock-out.
    """
    response = await imap.login(user_name, password)
    if response.result == "OK":
        return
    detail = " ".join(
        line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        for line in (response.lines or [])
    ).strip()
    raise ConnectionError(
        f"IMAP login failed for {user_name!r}: {response.result}" + (f" ({detail})" if detail else "")
    )


def _create_ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    """Create SSL context for SMTP/IMAP connections.

    Returns None for default verification, or permissive context
    for self-signed certificates when verify_ssl=False.
    """
    if verify_ssl:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Backwards-compatible alias
_create_smtp_ssl_context = _create_ssl_context


class EmailClient:
    def __init__(self, email_server: EmailServer, sender: str | None = None):
        self.email_server = email_server
        self.sender = sender or email_server.user_name

        self.imap_class = aioimaplib.IMAP4_SSL if self.email_server.use_ssl else aioimaplib.IMAP4

        self.smtp_use_tls = self.email_server.use_ssl
        self.smtp_start_tls = self.email_server.start_ssl
        self.smtp_verify_ssl = self.email_server.verify_ssl

    def _imap_connect(self) -> aioimaplib.IMAP4_SSL | aioimaplib.IMAP4:
        """Create a new IMAP connection with the configured SSL context."""
        if self.email_server.use_ssl:
            imap_ssl_context = _create_ssl_context(self.email_server.verify_ssl)
            return self.imap_class(self.email_server.host, self.email_server.port, ssl_context=imap_ssl_context)
        return self.imap_class(self.email_server.host, self.email_server.port)

    def _get_smtp_ssl_context(self) -> ssl.SSLContext | None:
        """Get SSL context for SMTP connections based on verify_ssl setting."""
        return _create_ssl_context(self.smtp_verify_ssl)

    @staticmethod
    def _parse_recipients(email_message) -> list[str]:
        """Extract recipient addresses from To and Cc headers."""
        recipients = []
        to_header = email_message.get("To", "")
        if to_header:
            recipients = [addr.strip() for addr in to_header.split(",")]
        cc_header = email_message.get("Cc", "")
        if cc_header:
            recipients.extend([addr.strip() for addr in cc_header.split(",")])
        return recipients

    @staticmethod
    def _parse_date(date_str: str) -> datetime:
        """Parse email date string to datetime, with fallback to current time."""
        try:
            date_tuple = email.utils.parsedate_tz(date_str)
            if date_tuple:
                return datetime.fromtimestamp(email.utils.mktime_tz(date_tuple), tz=timezone.utc)
            return datetime.now(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    @staticmethod
    def _is_attachment_part(part) -> bool:
        """Determine whether a MIME part should be treated as an attachment.

        A strict check on ``Content-Disposition: attachment`` misses a common case:
        many clients (notably Apple Mail on iOS/macOS) send images, PDFs and other
        files with ``Content-Disposition: inline`` (or no disposition header at all)
        but with a filename parameter on the part. Those parts are real, user-facing
        attachments — the user uploaded a file and expects it to show up — even
        though they're inlined into the body via Content-ID references.

        Treat a part as an attachment when:
          - the disposition explicitly says ``attachment``, OR
          - the part carries a filename (works for ``inline`` or no disposition).

        Multipart container parts and bodyless text parts have no filename and an
        empty disposition, so they are correctly excluded.
        """
        content_disposition = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in content_disposition:
            return True
        filename = part.get_filename()
        # Be defensive: only trust real string filenames. (Unconfigured MagicMock
        # instances in older tests return truthy MagicMock objects from
        # ``get_filename`` and would otherwise misclassify text parts.)
        return isinstance(filename, str) and bool(filename)

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

        # Extract recipients and parse date
        to_addresses = self._parse_recipients(email_message)
        date = self._parse_date(date_str)

        # Get body content
        body = ""
        html_body = ""  # Fallback if no text/plain
        attachments = []

        def _strip_html(html: str) -> str:
            """Simple HTML to text conversion."""
            import re

            # Remove script and style elements
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
            # Convert common block elements to newlines
            text = re.sub(r"<(br|p|div|tr|li)[^>]*/?>", "\n", text, flags=re.IGNORECASE)
            # Remove all remaining HTML tags
            text = re.sub(r"<[^>]+>", "", text)
            # Decode common HTML entities
            text = text.replace("&nbsp;", " ").replace("&amp;", "&")
            text = text.replace("&lt;", "<").replace("&gt;", ">")
            text = text.replace("&quot;", '"').replace("&#39;", "'")
            # Collapse multiple newlines and whitespace
            text = re.sub(r"\n\s*\n", "\n\n", text)
            text = re.sub(r" +", " ", text)
            return text.strip()

        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()

                # Handle attachments — including inline-disposition parts with a
                # filename (Apple Mail commonly sends photos this way).
                if self._is_attachment_part(part):
                    filename = part.get_filename()
                    if filename:
                        attachments.append(filename)
                # Handle text parts - prefer text/plain
                elif content_type == "text/plain":
                    body_part = part.get_payload(decode=True)
                    if body_part:
                        charset = part.get_content_charset("utf-8")
                        try:
                            body += body_part.decode(charset)
                        except UnicodeDecodeError:
                            body += body_part.decode("utf-8", errors="replace")
                # Collect HTML as fallback
                elif content_type == "text/html" and not body:
                    html_part = part.get_payload(decode=True)
                    if html_part:
                        charset = part.get_content_charset("utf-8")
                        try:
                            html_body += html_part.decode(charset)
                        except UnicodeDecodeError:
                            html_body += html_part.decode("utf-8", errors="replace")

            # Fall back to HTML if no plain text found
            if not body and html_body:
                body = _strip_html(html_body)
        else:
            # Handle single-part emails
            content_type = email_message.get_content_type()
            payload = email_message.get_payload(decode=True)
            if payload:
                charset = email_message.get_content_charset("utf-8")
                try:
                    text = payload.decode(charset)
                except UnicodeDecodeError:
                    text = payload.decode("utf-8", errors="replace")

                body = _strip_html(text) if content_type == "text/html" else text
        # TODO: Allow retrieving full email body
        if body and len(body) > MAX_BODY_LENGTH:
            body = body[:MAX_BODY_LENGTH] + "...[TRUNCATED]"
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
    def _sanitize_imap_value(value: str) -> str:
        """Sanitize a string value for IMAP search criteria.

        For multi-word values, strips embedded double quotes (invalid per RFC 3501
        Section 4.3) and wraps in double quotes. Single-word values pass through unchanged.
        """
        if " " not in value:
            return value
        sanitized = value.replace('"', "")
        return f'"{sanitized}"'

    @staticmethod
    def _build_search_criteria(
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        body: str | None = None,
        text: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
    ) -> list[str]:
        search_criteria = []
        if before:
            search_criteria.extend(["BEFORE", before.strftime("%d-%b-%Y").upper()])
        if since:
            search_criteria.extend(["SINCE", since.strftime("%d-%b-%Y").upper()])
        if subject:
            search_criteria.extend(["SUBJECT", EmailClient._sanitize_imap_value(subject)])
        if body:
            search_criteria.extend(["BODY", EmailClient._sanitize_imap_value(body)])
        if text:
            search_criteria.extend(["TEXT", EmailClient._sanitize_imap_value(text)])
        if from_address:
            search_criteria.extend(["FROM", EmailClient._sanitize_imap_value(from_address)])
        if to_address:
            search_criteria.extend(["TO", EmailClient._sanitize_imap_value(to_address)])

        # Flag-based criteria using mapping to reduce complexity
        flag_criteria = [
            (seen, {True: "SEEN", False: "UNSEEN"}),
            (flagged, {True: "FLAGGED", False: "UNFLAGGED"}),
            (answered, {True: "ANSWERED", False: "UNANSWERED"}),
        ]
        for flag_value, criteria_map in flag_criteria:
            if flag_value in criteria_map:
                search_criteria.append(criteria_map[flag_value])

        return search_criteria or ["ALL"]

    def _parse_headers(self, email_id: str, raw_headers: bytes) -> dict[str, Any] | None:
        """Parse raw email headers into metadata dictionary.

        Note: this parses only header data (BODY.PEEK[HEADER]) so it cannot
        populate the attachments list — that requires fetching BODYSTRUCTURE
        or the full body. The attachments list is intentionally returned
        empty here; ``_parse_email_data`` populates it from the full body.
        """
        try:
            parser = BytesParser(policy=default)
            email_message = parser.parsebytes(raw_headers)

            subject = email_message.get("Subject", "")
            sender = email_message.get("From", "")
            date_str = email_message.get("Date", "")
            # Expose Message-ID for reply threading and de-duplication on the client.
            message_id = email_message.get("Message-ID")

            to_addresses = self._parse_recipients(email_message)
            date = self._parse_date(date_str)

            return {
                "email_id": email_id,
                "message_id": message_id,
                "subject": subject,
                "from": sender,
                "to": to_addresses,
                "date": date,
                "attachments": [],
            }
        except Exception as e:
            logger.error(f"Error parsing email headers: {e!s}")
            return None

    async def _fetch_dates_chunk(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        chunk: list[bytes],
        chunk_num: int,
        total_chunks: int,
        timeout: float = 30.0,
    ) -> dict[str, datetime]:
        """Fetch INTERNALDATE for a single chunk of UIDs."""
        uid_list = ",".join(uid.decode() for uid in chunk)
        chunk_start = time.perf_counter()
        _, data = await asyncio.wait_for(
            imap.uid("fetch", uid_list, "(INTERNALDATE)"),
            timeout=timeout,
        )
        chunk_elapsed = time.perf_counter() - chunk_start

        chunk_dates: dict[str, datetime] = {}
        for item in data:
            if not isinstance(item, bytes) or b"INTERNALDATE" not in item:
                continue
            uid_match = re.search(rb"UID (\d+)", item)
            date_match = re.search(rb'INTERNALDATE "([^"]+)"', item)
            if uid_match and date_match:
                uid = uid_match.group(1).decode()
                date_str = date_match.group(1).decode().strip()
                chunk_dates[uid] = datetime.strptime(date_str, "%d-%b-%Y %H:%M:%S %z")

        if total_chunks > 1:
            logger.info(f"Fetched dates chunk {chunk_num}/{total_chunks}: {len(chunk)} UIDs in {chunk_elapsed:.2f}s")

        return chunk_dates

    async def _batch_fetch_dates(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_ids: list[bytes],
        chunk_size: int = 500,
    ) -> dict[str, datetime]:
        """Batch fetch INTERNALDATE for all UIDs in sequential chunks.

        Uses a conservative chunk_size (default 500) to avoid hitting
        Python's recursion limit in aioimaplib's recursive response parser
        (see: aioimaplib _handle_responses). IMAP connections are sequential
        by protocol, so chunks must be fetched serially — not in parallel.
        """
        if not email_ids:
            return {}

        # Split into chunks
        chunks = [email_ids[i : i + chunk_size] for i in range(0, len(email_ids), chunk_size)]
        total_chunks = len(chunks)

        # Fetch chunks sequentially (IMAP protocol is sequential on a single connection)
        uid_dates: dict[str, datetime] = {}
        for chunk_num, chunk in enumerate(chunks, 1):
            chunk_dates = await self._fetch_dates_chunk(imap, chunk, chunk_num, total_chunks)
            uid_dates.update(chunk_dates)

        return uid_dates

    async def _batch_fetch_headers(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_ids: list[bytes] | list[str],
    ) -> dict[str, dict[str, Any]]:
        """Batch fetch headers for a list of UIDs."""
        if not email_ids:
            return {}

        # Normalize to list of strings
        str_ids = [uid.decode() if isinstance(uid, bytes) else uid for uid in email_ids]
        uid_list = ",".join(str_ids)
        _, data = await imap.uid("fetch", uid_list, "BODY.PEEK[HEADER]")

        results: dict[str, dict[str, Any]] = {}
        for i, item in enumerate(data):
            if not isinstance(item, bytes) or b"BODY[HEADER]" not in item:
                continue
            # First try to find UID in the same line (standard format)
            uid_match = re.search(rb"UID (\d+)", item)
            if uid_match and i + 1 < len(data) and isinstance(data[i + 1], bytearray):
                uid = uid_match.group(1).decode()
                raw_headers = bytes(data[i + 1])
                metadata = self._parse_headers(uid, raw_headers)
                if metadata:
                    results[uid] = metadata
            # Proton Bridge format: UID comes AFTER header data in a separate item
            # Format: [i]=b'N FETCH (BODY[HEADER] {size}', [i+1]=bytearray(headers), [i+2]=b' UID xxx)'
            elif i + 2 < len(data) and isinstance(data[i + 1], bytearray):
                uid_after_match = re.search(rb"UID (\d+)", data[i + 2]) if isinstance(data[i + 2], bytes) else None
                if uid_after_match:
                    uid = uid_after_match.group(1).decode()
                    raw_headers = bytes(data[i + 1])
                    metadata = self._parse_headers(uid, raw_headers)
                    if metadata:
                        results[uid] = metadata

        return results

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
    ) -> tuple[int, list[dict[str, Any]]]:
        imap = self._imap_connect()
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select mailbox
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            search_criteria = self._build_search_criteria(
                before,
                since,
                subject,
                from_address=from_address,
                to_address=to_address,
                seen=seen,
                flagged=flagged,
                answered=answered,
            )
            logger.info(f"Get metadata: Search criteria: {search_criteria}")

            # Search for messages - use UID SEARCH for better compatibility
            _, messages = await imap.uid_search(*search_criteria)

            # Handle empty or None responses
            if not messages or not messages[0]:
                logger.warning("No messages returned from search")
                return 0, []

            email_ids = messages[0].split()
            logger.info(f"Found {len(email_ids)} email IDs")

            # Phase 1: Batch fetch INTERNALDATE for sorting (sequential chunks)
            fetch_dates_start = time.perf_counter()
            uid_dates = await self._batch_fetch_dates(imap, email_ids)
            fetch_dates_elapsed = time.perf_counter() - fetch_dates_start

            # Sort by INTERNALDATE
            sorted_uids = sorted(uid_dates.items(), key=lambda x: x[1], reverse=(order == "desc"))

            # Paginate
            start = (page - 1) * page_size
            page_uids = [uid for uid, _ in sorted_uids[start : start + page_size]]

            if not page_uids:
                logger.info(f"Phase 1 (dates): {len(uid_dates)} UIDs in {fetch_dates_elapsed:.2f}s, page {page} empty")
                return len(email_ids), []

            # Phase 2: Batch fetch headers for requested page only
            fetch_headers_start = time.perf_counter()
            metadata_by_uid = await self._batch_fetch_headers(imap, page_uids)
            fetch_headers_elapsed = time.perf_counter() - fetch_headers_start

            logger.info(
                f"Fetched page {page}: {fetch_dates_elapsed:.2f}s dates ({len(uid_dates)} UIDs), "
                f"{fetch_headers_elapsed:.2f}s headers ({len(page_uids)} UIDs)"
            )

            # Collect page results in sorted order
            page_emails = [metadata_by_uid[uid] for uid in page_uids if uid in metadata_by_uid]
            return len(email_ids), page_emails
        finally:
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
        """Try non-mutating fetch formats to get email data."""
        fetch_formats = ["BODY.PEEK[]", "(BODY.PEEK[])"]

        for fetch_format in fetch_formats:
            try:
                response = await imap.uid("fetch", email_id, fetch_format)
                _raise_for_imap_error(response, f"FETCH email {email_id} with {fetch_format}")
                _, data = response

                if data and len(data) > 0 and self._check_email_content(data):
                    return data

            except Exception as e:
                logger.debug(f"Fetch format {fetch_format} failed: {e}")

        return None

    async def get_email_body_by_id(
        self, email_id: str, mailbox: str = "INBOX", mark_as_read: bool = False
    ) -> dict[str, Any] | None:
        imap = self._imap_connect()
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select mailbox
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            # Fetch the specific email by UID without implicitly marking it as read
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
                email_data = self._parse_email_data(raw_email, email_id)
            except Exception as e:
                logger.error(f"Error parsing email: {e!s}")
                return None

            if mark_as_read:
                try:
                    store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Seen)")
                    _raise_for_imap_error(store_response, f"STORE \\Seen for email {email_id}")
                except Exception as e:
                    logger.warning(f"Failed to mark email {email_id} as read: {e}")

            return email_data

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
        imap = self._imap_connect()
        try:
            await imap._client_task
            await imap.wait_hello_from_server()

            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
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
                    # Match attachments listed by ``_parse_email_data`` — this includes
                    # inline-disposition parts with a filename (e.g. iOS Mail photos).
                    if not self._is_attachment_part(part):
                        continue
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

    def compose_message(
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
        include_bcc_header: bool = False,
    ) -> MIMEText | MIMEMultipart:
        """Compose an email message without sending it.

        Builds MIME structure, sets headers (Subject, From, To, Cc, Date,
        Message-Id, threading headers). Synchronous — no I/O.

        When ``include_bcc_header`` is True (used for local IMAP storage such
        as Drafts or Sent copies), the Bcc header is included so mail clients
        can display the BCC recipients.  When False (default, used for SMTP
        sending), the Bcc header is omitted — BCC recipients are delivered
        via the SMTP envelope only.
        """
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

        # Add BCC header when saving locally (drafts, sent copies)
        if bcc and include_bcc_header:
            msg["Bcc"] = ", ".join(bcc)

        # Set threading headers for replies
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references

        # Set Date and Message-Id headers
        msg["Date"] = email.utils.formatdate(localtime=True)
        sender_domain = self.sender.rsplit("@", 1)[-1].rstrip(">")
        msg["Message-Id"] = email.utils.make_msgid(domain=sender_domain)

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
    ) -> MIMEText | MIMEMultipart:
        msg = self.compose_message(recipients, subject, body, cc, bcc, html, attachments, in_reply_to, references)

        async with aiosmtplib.SMTP(
            hostname=self.email_server.host,
            port=self.email_server.port,
            start_tls=self.smtp_start_tls,
            use_tls=self.smtp_use_tls,
            tls_context=self._get_smtp_ssl_context(),
        ) as smtp:
            await smtp.login(self.email_server.user_name, self.email_server.password.get_secret_value())

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
        if incoming_server.use_ssl:
            imap_ssl_context = _create_ssl_context(incoming_server.verify_ssl)
            imap = aioimaplib.IMAP4_SSL(incoming_server.host, incoming_server.port, ssl_context=imap_ssl_context)
        else:
            imap = aioimaplib.IMAP4(incoming_server.host, incoming_server.port)

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
            await _imap_login(imap, incoming_server.user_name, incoming_server.password.get_secret_value())
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

        except ConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error saving to Sent folder: {e}")
            return False
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.debug(f"Error during logout: {e}")

    async def append_to_mailbox(
        self,
        msg: MIMEText | MIMEMultipart,
        incoming_server: EmailServer,
        mailbox: str,
        flags: str = r"(\Draft \Seen)",
    ) -> str | None:
        """Append a message to the specified IMAP folder.

        Unlike append_to_sent, this targets a single user-specified mailbox
        without folder discovery. Returns the IMAP UID of the appended message
        (if the server supports APPENDUID / RFC 4315), or ``"unknown"`` on
        success without UID, or ``None`` on failure.
        """
        if incoming_server.use_ssl:
            imap_ssl_context = _create_ssl_context(incoming_server.verify_ssl)
            imap = aioimaplib.IMAP4_SSL(incoming_server.host, incoming_server.port, ssl_context=imap_ssl_context)
        else:
            imap = aioimaplib.IMAP4(incoming_server.host, incoming_server.port)

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await _imap_login(imap, incoming_server.user_name, incoming_server.password.get_secret_value())
            await _send_imap_id(imap)

            result = await imap.select(_quote_mailbox(mailbox))
            status = result[0] if isinstance(result, tuple) else result
            if str(status).upper() != "OK":
                logger.warning(f"Mailbox '{mailbox}' not found or not selectable: {status}")
                return None

            msg_bytes = msg.as_bytes()
            append_result = await imap.append(
                msg_bytes,
                mailbox=_quote_mailbox(mailbox),
                flags=flags,
            )
            append_status = append_result[0] if isinstance(append_result, tuple) else append_result
            if str(append_status).upper() == "OK":
                # Try to extract UID from APPENDUID response (RFC 4315)
                uid = None
                if isinstance(append_result, tuple) and len(append_result) > 1:
                    for part in append_result[1]:
                        part_str = part.decode("utf-8") if isinstance(part, bytes) else str(part)
                        match = re.search(r"APPENDUID\s+\d+\s+(\d+)", part_str, re.IGNORECASE)
                        if match:
                            uid = match.group(1)
                            break
                logger.info(f"Saved email to '{mailbox}'" + (f" (UID {uid})" if uid else ""))
                return uid or "unknown"
            else:
                logger.warning(f"Failed to append to '{mailbox}': {append_status}")
                return None

        except ConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error saving to mailbox '{mailbox}': {e}")
            return None
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.debug(f"Error during logout: {e}")

    async def delete_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids)."""
        imap = self._imap_connect()
        deleted_ids = []
        failed_ids = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
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

    async def mark_emails_as_read(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Mark emails as read by setting the \\Seen flag. Returns (marked_ids, failed_ids)."""
        imap = self._imap_connect()
        marked_ids: list[str] = []
        failed_ids: list[str] = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            for email_id in email_ids:
                try:
                    store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Seen)")
                    _raise_for_imap_error(store_response, f"STORE \\Seen for email {email_id}")
                    marked_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to mark email {email_id} as read: {e}")
                    failed_ids.append(email_id)
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return marked_ids, failed_ids

    async def move_emails(
        self, email_ids: list[str], source_mailbox: str, destination_mailbox: str
    ) -> tuple[list[str], list[str]]:
        """Move emails to a different mailbox. Uses IMAP MOVE (RFC 6851) with COPY+DELETE fallback."""
        imap = self._imap_connect()
        moved_ids = []
        failed_ids = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(source_mailbox))
            _raise_for_imap_error(select_response, f"SELECT source mailbox {source_mailbox}")

            capabilities = {str(capability).upper() for capability in getattr(imap, "capabilities", ())}
            has_move = hasattr(imap, "move") and "MOVE" in capabilities

            for email_id in email_ids:
                try:
                    if has_move:
                        move_response = await imap.uid("move", email_id, _quote_mailbox(destination_mailbox))
                        _raise_for_imap_error(move_response, f"MOVE email {email_id}")
                    else:
                        copy_response = await imap.uid("copy", email_id, _quote_mailbox(destination_mailbox))
                        _raise_for_imap_error(copy_response, f"COPY email {email_id}")
                        store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Deleted)")
                        _raise_for_imap_error(store_response, f"STORE \\Deleted for email {email_id}")
                    moved_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to move email {email_id}: {e}")
                    failed_ids.append(email_id)

            if not has_move and moved_ids:
                try:
                    expunge_response = await imap.expunge()
                    _raise_for_imap_error(expunge_response, "EXPUNGE moved emails")
                except Exception as e:
                    logger.error(f"Failed to expunge moved emails: {e}")
                    failed_ids.extend(moved_ids)
                    moved_ids = []
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return moved_ids, failed_ids

    async def list_mailboxes(self, pattern: str = "*", reference: str = "") -> list[MailboxInfo]:
        """List available IMAP mailboxes with flags and delimiter."""
        imap = self._imap_connect()
        mailboxes = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)

            quoted_ref = _quote_mailbox(reference) if reference else '""'
            response = await imap.list(quoted_ref, pattern)
            _raise_for_imap_error(response, f"LIST mailboxes with pattern {pattern}")
            _, data = response

            for item in data:
                if item == b"":
                    continue
                item_str = item.decode("utf-8") if isinstance(item, bytes) else str(item)
                flags = []
                if "(" in item_str and ")" in item_str:
                    flags_str = item_str[item_str.index("(") + 1 : item_str.index(")")]
                    flags = [f.strip() for f in flags_str.split() if f.strip()]

                parts = item_str.split('"')
                if len(parts) >= 3:
                    delimiter = parts[1]
                    folder_name = parts[-2]
                    mailboxes.append(MailboxInfo(name=folder_name, delimiter=delimiter, flags=flags))
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return mailboxes


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
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
    ) -> EmailMetadataPageResponse:
        total, email_dicts = await self.incoming_client.get_emails_metadata(
            page,
            page_size,
            before,
            since,
            subject,
            from_address,
            to_address,
            order,
            mailbox,
            seen,
            flagged,
            answered,
        )
        emails = [EmailMetadata.from_email(d) for d in email_dicts]
        return EmailMetadataPageResponse(
            page=page,
            page_size=page_size,
            before=before,
            since=since,
            subject=subject,
            emails=emails,
            total=total,
        )

    async def get_emails_content(
        self, email_ids: list[str], mailbox: str = "INBOX", mark_as_read: bool = False
    ) -> EmailContentBatchResponse:
        """Batch retrieve email body content"""
        emails = []
        failed_ids = []

        for email_id in email_ids:
            try:
                email_data = await self.incoming_client.get_email_body_by_id(email_id, mailbox, mark_as_read)
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
            # Add BCC header to the saved copy so users can see who was BCC'd.
            # This MUST happen after smtp.send_message() — that ordering is
            # load-bearing for security (BCC must not appear in sent headers).
            if bcc and msg["Bcc"] is None:
                msg["Bcc"] = ", ".join(bcc)
            try:
                await self.outgoing_client.append_to_sent(
                    msg,
                    self.email_settings.incoming,
                    self.sent_folder_name,
                )
            except Exception as e:
                logger.error(f"Failed to save email to Sent folder: {e}", exc_info=True)

    async def save_to_mailbox(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        mailbox: str = "Drafts",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: bool = False,
        attachments: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        flags: list[str] | None = None,
    ) -> str:
        """Compose and save an email to the specified IMAP mailbox.

        BCC headers are preserved in the saved message so mail clients can
        display BCC recipients (unlike ``send_email``, where BCC is handled
        via the SMTP envelope only).

        Returns:
            A string in the format ``<message-id>|uid:<uid>``.

        Raises:
            ValueError: If any flag in *flags* is invalid per RFC 3501.
            RuntimeError: If the IMAP APPEND operation fails.
        """
        msg = self.outgoing_client.compose_message(
            recipients,
            subject,
            body,
            cc,
            bcc,
            html,
            attachments,
            in_reply_to,
            references,
            include_bcc_header=True,
        )

        flags_str = r"(\Draft \Seen)" if flags is None else _validate_flags(flags)

        uid = await self.outgoing_client.append_to_mailbox(msg, self.email_settings.incoming, mailbox, flags_str)

        if uid is None:
            raise RuntimeError(f"Failed to save email to mailbox '{mailbox}'")

        message_id = msg["Message-Id"] or "saved"
        return f"{message_id}|uid:{uid}"

    async def delete_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids)."""
        return await self.incoming_client.delete_emails(email_ids, mailbox)

    async def mark_emails_as_read(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Mark emails as read by their UIDs. Returns (marked_ids, failed_ids)."""
        return await self.incoming_client.mark_emails_as_read(email_ids, mailbox)

    async def move_emails(
        self, email_ids: list[str], source_mailbox: str, destination_mailbox: str
    ) -> tuple[list[str], list[str]]:
        """Move emails between mailboxes. Returns (moved_ids, failed_ids)."""
        return await self.incoming_client.move_emails(email_ids, source_mailbox, destination_mailbox)

    async def list_mailboxes(self, pattern: str = "*", reference: str = "") -> list[MailboxInfo]:
        """List available mailboxes with flags and delimiter."""
        return await self.incoming_client.list_mailboxes(pattern, reference)

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
