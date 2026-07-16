import asyncio
import base64
import binascii
import email.utils
import inspect
import mimetypes
import re
import ssl
import time
import unicodedata
from datetime import UTC, datetime
from email.header import Header
from email.message import Message
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any, cast

import aioimaplib
import aiosmtplib
from bs4 import BeautifulSoup

from mcp_email_server.config import EmailServer, EmailSettings, get_settings, sender_allowed
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

# Common Archive folder names, used as a fallback when no RFC 6154 \Archive flag is found.
_ARCHIVE_FOLDER_CANDIDATES = ("Archive", "Archives", "[Gmail]/All Mail")


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


def encode_mailbox_name(mailbox: str) -> str:
    """Encode an IMAP mailbox name using RFC 3501 Modified UTF-7."""
    result: list[str] = []
    buffer: list[str] = []

    def flush_buffer() -> None:
        if not buffer:
            return
        text = "".join(buffer)
        encoded = base64.b64encode(text.encode("utf-16-be")).decode("ascii")
        result.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
        buffer.clear()

    for char in mailbox:
        codepoint = ord(char)
        if char == "&":
            flush_buffer()
            result.append("&-")
        elif 0x20 <= codepoint <= 0x7E:
            flush_buffer()
            result.append(char)
        else:
            buffer.append(char)

    flush_buffer()
    return "".join(result)


def decode_mailbox_name(mailbox: str) -> str:
    """Decode an IMAP mailbox name from RFC 3501 Modified UTF-7."""
    result: list[str] = []
    index = 0

    while index < len(mailbox):
        char = mailbox[index]
        if char != "&":
            result.append(char)
            index += 1
            continue

        end = mailbox.find("-", index + 1)
        if end == -1:
            result.append(mailbox[index:])
            break
        if end == index + 1:
            result.append("&")
            index = end + 1
            continue

        encoded = mailbox[index + 1 : end].replace(",", "/")
        padding = "=" * (-len(encoded) % 4)
        try:
            decoded = base64.b64decode(encoded + padding, validate=True).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError):
            result.append(mailbox[index : end + 1])
        else:
            result.append(decoded)
        index = end + 1

    return "".join(result)


def _skip_imap_whitespace(value: str, start: int) -> int:
    """Return the next non-whitespace index in an IMAP response line."""
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    return index


def _read_quoted_imap_token(value: str, start: int) -> tuple[str, int]:
    """Read a quoted IMAP token."""
    index = start + 1
    token: list[str] = []
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            token.append(value[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(token), index + 1
        token.append(char)
        index += 1
    return "".join(token), index


def _read_parenthesized_imap_token(value: str, start: int) -> tuple[str, int]:
    """Read a parenthesized IMAP token."""
    depth = 1
    index = start + 1
    token: list[str] = []
    while index < len(value):
        char = value[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return "".join(token), index + 1
        token.append(char)
        index += 1
    return "".join(token), index


def _read_atom_imap_token(value: str, start: int) -> tuple[str, int]:
    """Read an atom IMAP token."""
    index = start
    while index < len(value) and not value[index].isspace():
        index += 1
    return value[start:index], index


def _read_imap_list_token(value: str, start: int) -> tuple[str | None, int]:
    """Read one token from an IMAP LIST response line."""
    index = _skip_imap_whitespace(value, start)
    if index >= len(value):
        return None, index
    if value[index] == '"':
        return _read_quoted_imap_token(value, index)
    if value[index] == "(":
        return _read_parenthesized_imap_token(value, index)
    return _read_atom_imap_token(value, index)


def _parse_list_response(item: bytes | str) -> MailboxInfo | None:
    """Parse one IMAP LIST response into a MailboxInfo object."""
    item_str = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
    item_str = item_str.strip()
    if not item_str:
        return None

    flags: list[str] = []
    position = 0
    if item_str.startswith("("):
        flags_token, position = _read_imap_list_token(item_str, position)
        flags = [flag.strip() for flag in (flags_token or "").split() if flag.strip()]

    delimiter_token, position = _read_imap_list_token(item_str, position)
    mailbox_token, _position = _read_imap_list_token(item_str, position)
    if delimiter_token is None or mailbox_token is None:
        return None

    delimiter = "" if delimiter_token.upper() == "NIL" else delimiter_token
    return MailboxInfo(name=decode_mailbox_name(mailbox_token), delimiter=delimiter, flags=flags)


def _quote_mailbox(mailbox: str) -> str:
    """Quote mailbox name for IMAP compatibility.

    Some IMAP servers (notably Proton Mail Bridge) require mailbox names
    to be quoted. This is valid per RFC 3501 and works with all IMAP servers.

    Per RFC 3501 Section 9 (Formal Syntax), quoted strings must escape
    backslashes and double-quote characters with a preceding backslash.
    Mailbox names with non-ASCII characters are encoded using Modified UTF-7
    as required by RFC 3501 Section 5.1.3.

    See: https://github.com/wh1isper/mcp-email-server/issues/87
    See: https://github.com/wh1isper/mcp-email-server/issues/172
    See: https://www.rfc-editor.org/rfc/rfc3501#section-9
    """
    encoded = encode_mailbox_name(mailbox)
    # Per RFC 3501, literal double-quote characters in a quoted string must
    # be escaped with a backslash. Backslashes themselves must also be escaped.
    escaped = encoded.replace("\\", "\\\\").replace('"', r"\"")
    return f'"{escaped}"'


def _uid_sort_key(uid: bytes | str) -> int:
    """Return a numeric sort key for IMAP UIDs."""
    value = uid.decode() if isinstance(uid, bytes) else uid
    return int(value)


def _imap_status(response: Any) -> str:
    """Return the normalized status from an aioimaplib response."""
    if hasattr(response, "result"):
        return str(response.result).upper()
    if isinstance(response, tuple) and response:
        return str(response[0]).upper()
    return str(response).upper()


def _format_imap_response_detail(response: Any) -> str:
    """Return a compact, readable IMAP response detail string."""
    status = _imap_status(response)
    lines = getattr(response, "lines", None)
    if lines is None and isinstance(response, tuple) and len(response) > 1:
        lines = response[1]

    detail_parts = []
    for line in lines or []:
        if isinstance(line, bytes):
            detail_parts.append(line.decode("utf-8", errors="replace"))
        else:
            detail_parts.append(str(line))

    detail = " ".join(part for part in detail_parts if part).strip()
    return f"{status} {detail}".strip()


def _raise_for_imap_error(response: Any, operation: str) -> None:
    """Raise when an IMAP command returns a non-OK status."""
    if _imap_status(response) != "OK":
        detail = _format_imap_response_detail(response)
        msg = f"{operation} failed" + (f": {detail}" if detail else "")
        raise RuntimeError(msg)


def _raise_for_imap_command_failure(response: Any, operation: str) -> None:
    """Raise only when an IMAP command reports an explicit failure status."""
    if _imap_status(response) in {"NO", "BAD"}:
        detail = _format_imap_response_detail(response)
        msg = f"{operation} failed" + (f": {detail}" if detail else "")
        raise RuntimeError(msg)


def _decoded_payload(message: Message) -> bytes | None:
    """Return a decoded MIME payload when the email API produced bytes."""
    payload = message.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else None


def _html_to_text(html: str) -> str:
    """Convert an HTML email body to readable plain text."""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()

    for link in soup.find_all("a"):
        href = str(link.get("href") or "").strip()
        normalized_href_scheme = re.sub(r"[\x00-\x20]+", "", href).lower()
        if not href or href.startswith("#") or normalized_href_scheme.startswith(("mailto:", "javascript:")):
            continue

        link_text = link.get_text(" ", strip=True)
        replacement = href if not link_text or link_text == href else f"{link_text} ({href})"
        link.replace_with(replacement)

    soup.smooth()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


async def _send_imap_id(imap: aioimaplib.IMAP4 | aioimaplib.IMAP4_SSL) -> None:
    """Send IMAP ID command with fallback for strict servers like 163.com.

    aioimaplib's id() method sends ID command with spaces between parentheses
    and content (e.g., 'ID ( "name" "value" )'), which some strict IMAP servers
    like 163.com reject with 'BAD Parse command error'.

    This function first tries the standard id() method, and if it fails,
    falls back to sending a raw command with correct format.

    See: https://github.com/wh1isper/mcp-email-server/issues/85
    """
    try:
        response = await imap.id(name="mcp-email-server", version="1.0.0")
        if response.result != "OK":
            # Fallback for strict servers (e.g., 163.com)
            # Send raw command with correct parenthesis format
            protocol = imap.protocol
            if protocol is None:
                logger.warning("IMAP ID command failed: IMAP protocol is not connected")
                return
            new_tag = protocol.new_tag()
            # aioimaplib 2.x returns str; retain compatibility with releases or
            # test doubles that expose an awaitable tag generator.
            if inspect.isawaitable(new_tag):
                new_tag = await new_tag
            await protocol.execute(
                aioimaplib.Command(
                    "ID",
                    new_tag,
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


def _create_starttls_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    """Create a concrete SSL context for asyncio STARTTLS upgrades."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _imap_capabilities(imap: aioimaplib.IMAP4) -> set[str]:
    """Return normalized capabilities from an aioimaplib protocol."""
    return {
        capability.decode("utf-8", errors="replace").upper()
        if isinstance(capability, bytes)
        else str(capability).upper()
        for capability in getattr(imap.protocol, "capabilities", ())
    }


async def _imap_starttls(imap: aioimaplib.IMAP4, ssl_context: ssl.SSLContext, host: str) -> None:
    """Upgrade an IMAP connection to TLS via STARTTLS."""
    protocol = imap.protocol
    if protocol is None:
        raise ConnectionError("IMAP protocol is not connected")

    capabilities = _imap_capabilities(imap)
    if "STARTTLS" not in capabilities:
        await protocol.capability()
        capabilities = _imap_capabilities(imap)
    if "STARTTLS" not in capabilities:
        raise OSError("IMAP server does not advertise STARTTLS capability")

    loop = asyncio.get_running_loop()
    response = await protocol.execute(aioimaplib.Command("STARTTLS", protocol.new_tag(), loop=loop))
    status = _imap_status(response)
    if status != "OK":
        raise OSError(f"STARTTLS command failed: {status}")

    transport = protocol.transport
    if transport is None:
        raise ConnectionError("IMAP transport is not connected")
    # aioimaplib declares BaseTransport but immediately calls write() on it;
    # an active asyncio IMAP connection necessarily has a writable transport.
    write_transport = cast(asyncio.WriteTransport, transport)
    tls_transport = await loop.start_tls(
        write_transport,
        protocol,
        ssl_context,
        server_hostname=host,
    )
    if tls_transport is None:
        raise ConnectionError("IMAP STARTTLS did not return a transport")
    protocol.transport = tls_transport
    await protocol.capability()


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
            if imap_ssl_context is not None:
                return self.imap_class(
                    self.email_server.host,
                    self.email_server.port,
                    ssl_context=imap_ssl_context,
                )
        return self.imap_class(self.email_server.host, self.email_server.port)

    async def _connect_imap(self) -> aioimaplib.IMAP4_SSL | aioimaplib.IMAP4:
        """Create, greet, and optionally STARTTLS-upgrade an IMAP connection."""
        imap = self._imap_connect()
        return await self._prepare_imap_connection(imap, self.email_server)

    @staticmethod
    async def _prepare_imap_connection(
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        server: EmailServer,
    ) -> aioimaplib.IMAP4_SSL | aioimaplib.IMAP4:
        """Wait for greeting and optionally STARTTLS-upgrade an IMAP connection."""
        await imap._client_task
        await imap.wait_hello_from_server()

        if server.start_ssl:
            ssl_context = _create_starttls_ssl_context(server.verify_ssl)
            await _imap_starttls(imap, ssl_context, server.host)

        return imap

    @staticmethod
    async def _connect_imap_server(server: EmailServer) -> aioimaplib.IMAP4_SSL | aioimaplib.IMAP4:
        """Create, greet, and optionally STARTTLS-upgrade an IMAP connection."""
        if server.use_ssl:
            imap_ssl_context = _create_ssl_context(server.verify_ssl)
            if imap_ssl_context is None:
                imap = aioimaplib.IMAP4_SSL(server.host, server.port)
            else:
                imap = aioimaplib.IMAP4_SSL(server.host, server.port, ssl_context=imap_ssl_context)
        else:
            imap = aioimaplib.IMAP4(server.host, server.port)

        return await EmailClient._prepare_imap_connection(imap, server)

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
                return datetime.fromtimestamp(email.utils.mktime_tz(date_tuple), tz=UTC)
            return datetime.now(UTC)
        except Exception:
            return datetime.now(UTC)

    @staticmethod
    def _normalize_attachment_name(name: str) -> str:
        """Normalize attachment filenames for robust MIME round-trip matching."""
        return unicodedata.normalize("NFC", name)

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

    def _parse_email_data(  # noqa: C901
        self,
        raw_email: bytes,
        email_id: str | None = None,
        body_offset: int = 0,
        max_body_length: int = MAX_BODY_LENGTH,
    ) -> dict[str, Any]:
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
                    body_part = _decoded_payload(part)
                    if body_part:
                        charset = part.get_content_charset("utf-8")
                        try:
                            body += body_part.decode(charset)
                        except UnicodeDecodeError:
                            body += body_part.decode("utf-8", errors="replace")
                # Collect HTML as fallback
                elif content_type == "text/html" and not body:
                    html_part = _decoded_payload(part)
                    if html_part:
                        charset = part.get_content_charset("utf-8")
                        try:
                            html_body += html_part.decode(charset)
                        except UnicodeDecodeError:
                            html_body += html_part.decode("utf-8", errors="replace")

            # Fall back to HTML if no plain text found
            if not body and html_body:
                body = _html_to_text(html_body)
        else:
            # Handle single-part emails
            content_type = email_message.get_content_type()
            payload = _decoded_payload(email_message)
            if payload:
                charset = email_message.get_content_charset("utf-8")
                try:
                    text = payload.decode(charset)
                except UnicodeDecodeError:
                    text = payload.decode("utf-8", errors="replace")

                body = _html_to_text(text) if content_type == "text/html" else text
        if body_offset < 0:
            raise ValueError("body_offset must be >= 0")
        if max_body_length < 1:
            raise ValueError("max_body_length must be >= 1")

        # Return at most ``max_body_length`` characters starting at ``body_offset``. When more of
        # the body remains past the window, append the ``...[TRUNCATED]`` marker so callers can page
        # through a long email by re-requesting with ``body_offset += max_body_length``.
        if body:
            window = body[body_offset : body_offset + max_body_length]
            if body_offset + max_body_length < len(body):
                window += "...[TRUNCATED]"
            body = window
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
        has_attachment: bool | None = None,
    ) -> list[str]:
        search_criteria = []
        if before:
            search_criteria.extend(["BEFORE", before.strftime("%d-%b-%Y").upper()])
        if since:
            search_criteria.extend(["SINCE", since.strftime("%d-%b-%Y").upper()])

        # Substring-match fields (IMAP keyword, value)
        text_criteria = [
            ("SUBJECT", subject),
            ("BODY", body),
            ("TEXT", text),
            ("FROM", from_address),
            ("TO", to_address),
        ]
        for keyword, value in text_criteria:
            if value:
                search_criteria.extend([keyword, EmailClient._sanitize_imap_value(value)])

        # Attachment heuristic: most attachments are carried in multipart/mixed.
        # May miss some types (e.g. inline images) or yield false positives.
        if has_attachment is True:
            search_criteria.extend(["HEADER", "Content-Type", "multipart/mixed"])
        elif has_attachment is False:
            search_criteria.extend(["NOT", "HEADER", "Content-Type", "multipart/mixed"])

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

    async def _batch_fetch_senders(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_ids: list[bytes] | list[str],
        chunk_size: int = 500,
    ) -> dict[str, str]:
        """Batch fetch the From header for all UIDs (chunked, sequential), for allowlist filtering.

        Returns {uid: raw From header}. Fetches only HEADER.FIELDS (FROM) to stay light and reuses
        _parse_headers (which tolerates a From-only header block).
        """
        if not email_ids:
            return {}

        chunks = [email_ids[i : i + chunk_size] for i in range(0, len(email_ids), chunk_size)]
        senders: dict[str, str] = {}
        for chunk in chunks:
            str_ids = [uid.decode() if isinstance(uid, bytes) else uid for uid in chunk]
            uid_list = ",".join(str_ids)
            fetch_response = await imap.uid("fetch", uid_list, "BODY.PEEK[HEADER.FIELDS (FROM)]")
            _raise_for_imap_command_failure(fetch_response, f"FETCH From headers for UIDs {uid_list}")
            _, data = fetch_response
            for i, item in enumerate(data):
                if not isinstance(item, bytes) or b"BODY[HEADER" not in item:
                    continue
                uid_match = re.search(rb"UID (\d+)", item)
                if uid_match and i + 1 < len(data) and isinstance(data[i + 1], bytearray):
                    meta = self._parse_headers(uid_match.group(1).decode(), bytes(data[i + 1]))
                    if meta:
                        senders[meta["email_id"]] = meta["from"]
                elif i + 2 < len(data) and isinstance(data[i + 1], bytearray):
                    uid_after = re.search(rb"UID (\d+)", data[i + 2]) if isinstance(data[i + 2], bytes) else None
                    if uid_after:
                        meta = self._parse_headers(uid_after.group(1).decode(), bytes(data[i + 1]))
                        if meta:
                            senders[meta["email_id"]] = meta["from"]
        return senders

    async def _enforce_sender_allowlist(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_id: str,
        allowed_senders: list[str] | None,
    ) -> None:
        """Raise ValueError (identical to not-found) when sender is not on the allowlist.

        No-op when ``allowed_senders`` is empty or None (backwards-compatible).
        """
        if allowed_senders:
            uid_senders = await self._batch_fetch_senders(imap, [email_id])
            if not sender_allowed(uid_senders.get(email_id, ""), allowed_senders):
                msg = f"Failed to fetch email with UID {email_id}"
                logger.error(msg)
                raise ValueError(msg)

    async def _blocked_uids(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_ids: list[str],
        allowed_senders: list[str] | None,
    ) -> set[str]:
        """UIDs whose From is not on the allowlist. Empty set when no allowlist (no IMAP work)."""
        if not allowed_senders:
            return set()
        uid_senders = await self._batch_fetch_senders(imap, email_ids)
        return {uid for uid in email_ids if not sender_allowed(uid_senders.get(uid, ""), allowed_senders)}

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
        body: str | None = None,
        text: str | None = None,
        has_attachment: bool | None = None,
        allowed_senders: list[str] | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        imap = await self._connect_imap()
        try:
            # Login and select mailbox
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            search_criteria = self._build_search_criteria(
                before,
                since,
                subject,
                body=body,
                text=text,
                from_address=from_address,
                to_address=to_address,
                seen=seen,
                flagged=flagged,
                answered=answered,
                has_attachment=has_attachment,
            )
            logger.info(f"Get metadata: Search criteria: {search_criteria}")

            # Search for messages - use UID SEARCH for better compatibility.
            # charset handling: aioimaplib defaults to "CHARSET utf-8", which
            # Microsoft Exchange rejects with `NO [BADCHARSET (US-ASCII)] The
            # specified charset is not supported.`, breaking all search/list
            # operations — so ASCII-only user searches omit the CHARSET token.
            # However, RFC 3501 requires a charset declaration for non-ASCII
            # search values: without it, servers such as Coremail interpret the
            # raw UTF-8 bytes as US-ASCII and silently return zero matches. Base
            # the decision only on user-supplied text fields, not on generated
            # criteria such as locale-dependent date strings.
            search_text_values = (subject, body, text, from_address, to_address)
            charset = "utf-8" if any(value and not value.isascii() for value in search_text_values) else None
            _, messages = await imap.uid_search(*search_criteria, charset=charset)

            # Handle empty or None responses
            if not messages or not messages[0]:
                logger.warning("No messages returned from search")
                return 0, []

            email_ids = messages[0].split()
            logger.info(f"Found {len(email_ids)} email IDs")

            # Sender allowlist: filter candidates BEFORE sorting/pagination so total + pages stay honest.
            if allowed_senders:
                uid_senders = await self._batch_fetch_senders(imap, email_ids)
                email_ids = [
                    uid for uid in email_ids if sender_allowed(uid_senders.get(uid.decode(), ""), allowed_senders)
                ]
                logger.info(f"Sender allowlist active: {len(email_ids)} of {len(uid_senders)} match")
                if not email_ids:
                    return 0, []

            # Phase 1: Batch fetch INTERNALDATE for sorting (sequential chunks)
            fetch_dates_start = time.perf_counter()
            uid_dates = await self._batch_fetch_dates(imap, email_ids)
            fetch_dates_elapsed = time.perf_counter() - fetch_dates_start

            missing_date_count = len(email_ids) - len(uid_dates)
            if missing_date_count:
                logger.warning(
                    f"Missing INTERNALDATE for {missing_date_count}/{len(email_ids)} searched UIDs; "
                    "falling back to UID order for those messages"
                )

            # Keep UID SEARCH results as the source of truth. Use INTERNALDATE where
            # available, and fall back to UID ordering for provider-specific
            # INTERNALDATE response formats that cannot be parsed.
            if order == "desc":
                sorted_uids = sorted(
                    (uid.decode() for uid in email_ids),
                    key=lambda uid: (
                        uid_dates.get(uid) is not None,
                        uid_dates.get(uid) or datetime.min.replace(tzinfo=UTC),
                        _uid_sort_key(uid),
                    ),
                    reverse=True,
                )
            else:
                sorted_uids = sorted(
                    (uid.decode() for uid in email_ids),
                    key=lambda uid: (
                        uid_dates.get(uid) is None,
                        uid_dates.get(uid) or datetime.max.replace(tzinfo=UTC),
                        _uid_sort_key(uid),
                    ),
                )

            # Paginate
            start = (page - 1) * page_size
            page_uids = sorted_uids[start : start + page_size]

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
        self,
        email_id: str,
        mailbox: str = "INBOX",
        mark_as_read: bool = False,
        allowed_senders: list[str] | None = None,
        body_offset: int = 0,
        max_body_length: int = MAX_BODY_LENGTH,
    ) -> dict[str, Any] | None:
        imap = await self._connect_imap()
        try:
            # Login and select mailbox
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            # Sender allowlist: check the From header BEFORE reading the body, so a blocked
            # message is never fetched/parsed, never marked read, and is indistinguishable from
            # a missing/inaccessible one (caller sees None either way).
            if allowed_senders:
                uid_senders = await self._batch_fetch_senders(imap, [email_id])
                if not sender_allowed(uid_senders.get(email_id, ""), allowed_senders):
                    return None

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
                email_data = self._parse_email_data(
                    raw_email, email_id, body_offset=body_offset, max_body_length=max_body_length
                )
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
        allowed_senders: list[str] | None = None,
    ) -> dict[str, Any]:
        """Download a specific attachment from an email and save it to disk.

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            save_path: The local path where the attachment will be saved.
            mailbox: The mailbox to search in (default: "INBOX").
            allowed_senders: Optional sender allowlist; when set, a non-allowed sender's
                message is treated as not found and its body is never fetched.

        Returns:
            A dictionary with download result information.
        """
        imap = await self._connect_imap()
        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            # Read-path allowlist: check the From header before fetching the body, so a
            # blocked sender's message is never read. Blocked fails identically to a missing
            # UID (same ValueError below), so it does not reveal whether the message exists.
            await self._enforce_sender_allowlist(imap, email_id, allowed_senders)

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
            attachment_data: bytes | None = None
            mime_type = None
            normalized_attachment_name = self._normalize_attachment_name(attachment_name)

            if email_message.is_multipart():
                for part in email_message.walk():
                    # Match attachments listed by ``_parse_email_data`` — this includes
                    # inline-disposition parts with a filename (e.g. iOS Mail photos).
                    if not self._is_attachment_part(part):
                        continue
                    filename = part.get_filename()
                    if not isinstance(filename, str):
                        continue
                    if self._normalize_attachment_name(filename) == normalized_attachment_name:
                        attachment_data = _decoded_payload(part)
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
        reply_to: str | None = None,
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
            msg["Subject"] = str(Header(subject, "utf-8"))
        else:
            msg["Subject"] = subject

        sender_name, sender_address = email.utils.parseaddr(self.sender)

        # Encode only the display name. RFC 2047 encoded-words must not cover the
        # addr-spec, otherwise strict clients may show the raw encoded blob.
        if sender_address:
            msg["From"] = email.utils.formataddr((str(Header(sender_name, "utf-8")), sender_address))
        elif any(ord(c) > 127 for c in self.sender):
            msg["From"] = str(Header(self.sender, "utf-8"))
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
        if reply_to:
            msg["Reply-To"] = reply_to

        # Set Date and Message-Id headers
        msg["Date"] = email.utils.formatdate(localtime=True)
        sender_for_domain = sender_address or self.sender
        sender_domain = sender_for_domain.rsplit("@", 1)[-1].rstrip(">")
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
        reply_to: str | None = None,
    ) -> MIMEText | MIMEMultipart:
        msg = self.compose_message(
            recipients, subject, body, cc, bcc, html, attachments, in_reply_to, references, False, reply_to
        )

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
                mailbox = _parse_list_response(folder)
                if mailbox and r"\Sent" in mailbox.flags:
                    logger.info(f"Found Sent folder by \\Sent flag: '{mailbox.name}'")
                    return mailbox.name
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
        imap = await self._connect_imap_server(incoming_server)

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
        imap = await self._connect_imap_server(incoming_server)

        try:
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

    async def delete_emails(
        self,
        email_ids: list[str],
        mailbox: str = "INBOX",
        allowed_senders: list[str] | None = None,
        report_blocked_mutations: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids).

        A blocked sender's UID is never flagged \\Deleted: by default it is reported as a
        no-op success (indistinguishable from a nonexistent UID); when
        report_blocked_mutations is True it is reported in failed_ids instead.
        """
        imap = await self._connect_imap()
        deleted_ids = []
        failed_ids = []

        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            blocked = await self._blocked_uids(imap, email_ids, allowed_senders)
            deleted_any = False
            for email_id in email_ids:
                if email_id in blocked:
                    (failed_ids if report_blocked_mutations else deleted_ids).append(email_id)
                    continue
                try:
                    store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Deleted)")
                    _raise_for_imap_error(store_response, f"STORE \\Deleted for email {email_id}")
                    deleted_ids.append(email_id)
                    deleted_any = True
                except Exception as e:
                    logger.error(f"Failed to delete email {email_id}: {e}")
                    failed_ids.append(email_id)

            if deleted_any:
                try:
                    expunge_response = await imap.expunge()
                    _raise_for_imap_error(expunge_response, "EXPUNGE deleted emails")
                except Exception as e:
                    logger.error(f"Failed to expunge deleted emails: {e}")
                    failed_ids.extend(deleted_ids)
                    deleted_ids = []
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return deleted_ids, failed_ids

    async def mark_emails_as_read(
        self,
        email_ids: list[str],
        mailbox: str = "INBOX",
        allowed_senders: list[str] | None = None,
        report_blocked_mutations: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Mark emails as read by setting the \\Seen flag. Returns (marked_ids, failed_ids).

        A blocked sender's UID is never flagged \\Seen: reported as a no-op success by
        default, or in failed_ids when report_blocked_mutations is True.
        """
        imap = await self._connect_imap()
        marked_ids: list[str] = []
        failed_ids: list[str] = []

        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            blocked = await self._blocked_uids(imap, email_ids, allowed_senders)
            for email_id in email_ids:
                if email_id in blocked:
                    (failed_ids if report_blocked_mutations else marked_ids).append(email_id)
                    continue
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
        self,
        email_ids: list[str],
        source_mailbox: str,
        destination_mailbox: str,
        allowed_senders: list[str] | None = None,
        report_blocked_mutations: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Move emails to a different mailbox. Uses IMAP MOVE (RFC 6851) with COPY+DELETE fallback.

        A blocked sender's UID is never copied/moved: reported as a no-op success by default,
        or in failed_ids when report_blocked_mutations is True.
        """
        imap = await self._connect_imap()
        moved_ids = []
        failed_ids = []

        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(source_mailbox))
            _raise_for_imap_error(select_response, f"SELECT source mailbox {source_mailbox}")

            capabilities = {str(capability).upper() for capability in getattr(imap, "capabilities", ())}
            has_move = hasattr(imap, "move") and "MOVE" in capabilities

            blocked = await self._blocked_uids(imap, email_ids, allowed_senders)
            copied: list[str] = []  # allowed UIDs that actually completed COPY+STORE on the fallback path
            for email_id in email_ids:
                if email_id in blocked:
                    (failed_ids if report_blocked_mutations else moved_ids).append(email_id)
                    continue
                try:
                    if has_move:
                        move_response = await imap.uid("move", email_id, _quote_mailbox(destination_mailbox))
                        _raise_for_imap_error(move_response, f"MOVE email {email_id}")
                    else:
                        copy_response = await imap.uid("copy", email_id, _quote_mailbox(destination_mailbox))
                        _raise_for_imap_error(copy_response, f"COPY email {email_id}")
                        store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Deleted)")
                        _raise_for_imap_error(store_response, f"STORE \\Deleted for email {email_id}")
                        copied.append(email_id)
                    moved_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to move email {email_id}: {e}")
                    failed_ids.append(email_id)

            if copied:  # only expunge when real COPY+STORE happened — never for blocked/no-op UIDs
                try:
                    expunge_response = await imap.expunge()
                    _raise_for_imap_error(expunge_response, "EXPUNGE moved emails")
                except Exception as e:
                    logger.error(f"Failed to expunge moved emails: {e}")
                    failed_ids.extend(copied)
                    moved_ids = [uid for uid in moved_ids if uid not in set(copied)]
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return moved_ids, failed_ids

    async def list_mailboxes(self, pattern: str = "*", reference: str = "") -> list[MailboxInfo]:
        """List available IMAP mailboxes with flags and delimiter."""
        imap = await self._connect_imap()
        mailboxes = []

        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)

            quoted_ref = _quote_mailbox(reference) if reference else '""'
            quoted_pattern = _quote_mailbox(pattern)
            # aioimaplib annotates mailbox_pattern as re.Pattern, but its wire
            # implementation and public API expect a preformatted IMAP string.
            response = await imap.list(quoted_ref, quoted_pattern)  # pyright: ignore[reportArgumentType]
            _raise_for_imap_error(response, f"LIST mailboxes with pattern {pattern}")
            _, data = response

            for item in data:
                mailbox = _parse_list_response(item)
                if mailbox:
                    mailboxes.append(mailbox)
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return mailboxes


class ClassicEmailHandler(EmailHandler):
    def __init__(self, email_settings: EmailSettings):
        self.email_settings = email_settings
        sender = f"{email_settings.full_name} <{email_settings.email_address}>"
        self.incoming_client = EmailClient(email_settings.incoming, sender=sender)
        self.outgoing_client = EmailClient(email_settings.outgoing, sender=sender) if email_settings.outgoing else None
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
        body: str | None = None,
        text: str | None = None,
        has_attachment: bool | None = None,
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
            body,
            text,
            has_attachment,
            allowed_senders=get_settings().allowed_senders,
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
        self,
        email_ids: list[str],
        mailbox: str = "INBOX",
        mark_as_read: bool = False,
        body_offset: int = 0,
        max_body_length: int = MAX_BODY_LENGTH,
    ) -> EmailContentBatchResponse:
        """Batch retrieve email body content, honoring the sender allowlist.

        The allowlist is enforced in the read path: get_email_body_by_id checks the From header
        before fetching the body, so a blocked message is never read or marked and returns None —
        indistinguishable from a missing/inaccessible one (both land in failed_ids).
        """
        allowed_senders = get_settings().allowed_senders
        emails = []
        failed_ids = []

        for email_id in email_ids:
            try:
                email_data = await self.incoming_client.get_email_body_by_id(
                    email_id,
                    mailbox,
                    mark_as_read,
                    allowed_senders=allowed_senders,
                    body_offset=body_offset,
                    max_body_length=max_body_length,
                )
                if not email_data:
                    failed_ids.append(email_id)
                    continue
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
        reply_to: str | None = None,
    ) -> None:
        if self.outgoing_client is None:
            raise RuntimeError(f"SMTP is not configured for account '{self.email_settings.account_name}'")

        msg = await self.outgoing_client.send_email(
            recipients, subject, body, cc, bcc, html, attachments, in_reply_to, references, reply_to
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
        msg = self.incoming_client.compose_message(
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

        uid = await self.incoming_client.append_to_mailbox(msg, self.email_settings.incoming, mailbox, flags_str)

        if uid is None:
            raise RuntimeError(f"Failed to save email to mailbox '{mailbox}'")

        message_id = msg["Message-Id"] or "saved"
        return f"{message_id}|uid:{uid}"

    async def delete_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids)."""
        settings = get_settings()
        return await self.incoming_client.delete_emails(
            email_ids,
            mailbox,
            allowed_senders=settings.allowed_senders,
            report_blocked_mutations=settings.report_blocked_mutations,
        )

    async def mark_emails_as_read(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Mark emails as read by their UIDs. Returns (marked_ids, failed_ids)."""
        settings = get_settings()
        return await self.incoming_client.mark_emails_as_read(
            email_ids,
            mailbox,
            allowed_senders=settings.allowed_senders,
            report_blocked_mutations=settings.report_blocked_mutations,
        )

    async def move_emails(
        self, email_ids: list[str], source_mailbox: str, destination_mailbox: str
    ) -> tuple[list[str], list[str]]:
        """Move emails between mailboxes. Returns (moved_ids, failed_ids)."""
        settings = get_settings()
        return await self.incoming_client.move_emails(
            email_ids,
            source_mailbox,
            destination_mailbox,
            allowed_senders=settings.allowed_senders,
            report_blocked_mutations=settings.report_blocked_mutations,
        )

    async def _find_archive_folder(self) -> str | None:
        """Locate the Archive folder via the RFC 6154 ``\\Archive`` flag, then common names."""
        mailboxes = await self.incoming_client.list_mailboxes()
        for mailbox_info in mailboxes:
            if any(flag.lstrip("\\").lower() == "archive" for flag in mailbox_info.flags):
                return mailbox_info.name

        names_by_lowercase = {mailbox_info.name.lower(): mailbox_info.name for mailbox_info in mailboxes}
        for candidate in _ARCHIVE_FOLDER_CANDIDATES:
            archive_folder = names_by_lowercase.get(candidate.lower())
            if archive_folder is not None:
                return archive_folder
        return None

    async def archive_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str], str]:
        """Move emails to the auto-detected Archive folder. Returns (moved_ids, failed_ids, archive_folder)."""
        archive_folder = await self._find_archive_folder()
        if archive_folder is None:
            raise ValueError(
                "No Archive folder found (looked for the RFC 6154 \\Archive flag and common names: "
                f"{', '.join(_ARCHIVE_FOLDER_CANDIDATES)}). Use move_emails with an explicit folder instead."
            )
        settings = get_settings()
        moved_ids, failed_ids = await self.incoming_client.move_emails(
            email_ids,
            mailbox,
            archive_folder,
            allowed_senders=settings.allowed_senders,
            report_blocked_mutations=settings.report_blocked_mutations,
        )
        return moved_ids, failed_ids, archive_folder

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
        allowed_senders = get_settings().allowed_senders
        result = await self.incoming_client.download_attachment(
            email_id, attachment_name, save_path, mailbox, allowed_senders=allowed_senders
        )
        return AttachmentDownloadResponse(
            email_id=result["email_id"],
            attachment_name=result["attachment_name"],
            mime_type=result["mime_type"],
            size=result["size"],
            saved_path=result["saved_path"],
        )
