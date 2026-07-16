from collections.abc import Callable
from datetime import datetime
from email.utils import getaddresses
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import Icon, ToolAnnotations
from pydantic import Field

from mcp_email_server.config import (
    AccountAttributes,
    EmailSettings,
    ProviderSettings,
    clear_settings_cache,
    get_settings,
    normalize_address,
)
from mcp_email_server.emails.dispatcher import dispatch_handler
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailContentBatchResponse,
    EmailMetadataPageResponse,
    MailboxInfo,
)

AnyFunction = Callable[..., Any]
ToolVisibilityPredicate = Callable[[], bool]


def _has_send_capable_account() -> bool:
    settings = get_settings()
    return any(isinstance(account, EmailSettings) and account.can_send for account in settings.get_accounts())


def _has_allowed_recipients() -> bool:
    return bool(get_settings().allowed_recipients)


def _has_allowed_senders() -> bool:
    return bool(get_settings().allowed_senders)


def _enforce_recipient_allowlist(
    recipients: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
) -> None:
    """Raise ValueError if any To/CC/BCC address is not in a configured allowlist.

    No-op when the allowlist is empty (all recipients permitted).
    """
    allowed = get_settings().allowed_recipients
    if not allowed:
        return
    allowed_set = set(allowed)
    candidates = [*recipients, *(cc or []), *(bcc or [])]
    blocked = [addr for _, addr in getaddresses(candidates) if normalize_address(addr) not in allowed_set]
    if blocked:
        raise ValueError(f"Recipient(s) not in allowlist: {', '.join(blocked)}. Allowed: {', '.join(allowed)}")


class VisibilityAwareFastMCP(FastMCP):
    """FastMCP server with declarative tool visibility predicates."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tool_visibility: dict[str, ToolVisibilityPredicate] = {}

    def tool(
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
        *,
        visible_if: ToolVisibilityPredicate | None = None,
    ) -> Callable[[AnyFunction], AnyFunction]:
        decorator = super().tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )

        def wrapped(fn: AnyFunction) -> AnyFunction:
            registered = decorator(fn)
            if visible_if is not None:
                self._tool_visibility[name or fn.__name__] = visible_if
            return registered

        return wrapped

    async def list_tools(self):
        tools = await super().list_tools()
        return [tool for tool in tools if self._tool_visibility.get(tool.name, lambda: True)()]


mcp = VisibilityAwareFastMCP("email")


@mcp.resource("email://{account_name}")
async def get_account(account_name: str) -> EmailSettings | ProviderSettings | None:
    settings = get_settings()
    return settings.get_account(account_name, masked=True)


@mcp.tool(description="List all configured email accounts with masked credentials.")
async def list_available_accounts() -> list[AccountAttributes]:
    settings = get_settings()
    return [account.masked() for account in settings.get_accounts()]


@mcp.tool(description="Add a new email account configuration to the settings.")
async def add_email_account(email: EmailSettings) -> str:
    settings = get_settings()
    try:
        settings.add_email(email)
        settings.store()
    except Exception:
        # add_email() reassigns settings.emails, and pydantic's validate_assignment
        # applies the new value BEFORE running the check_unique_account_names
        # after-validator — so even a rejected duplicate-name add leaves the cached
        # instance mutated. store() can also mutate-then-fail (e.g. a locked
        # keychain in explicit keyring mode). Either way, discard the cache so later
        # tool calls don't see a phantom/corrupted account that isn't actually on disk.
        clear_settings_cache()
        raise
    return f"Successfully added email account '{email.account_name}'"


@mcp.tool(
    description="List email metadata (email_id, subject, sender, recipients, date) without body content. Returns email_id for use with get_emails_content."
)
async def list_emails_metadata(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    page: Annotated[
        int,
        Field(default=1, description="The page number to retrieve (starting from 1)."),
    ] = 1,
    page_size: Annotated[int, Field(default=10, description="The number of emails to retrieve per page.")] = 10,
    before: Annotated[
        datetime | None,
        Field(default=None, description="Retrieve emails before this datetime (UTC)."),
    ] = None,
    since: Annotated[
        datetime | None,
        Field(default=None, description="Retrieve emails since this datetime (UTC)."),
    ] = None,
    subject: Annotated[str | None, Field(default=None, description="Filter emails by subject.")] = None,
    from_address: Annotated[str | None, Field(default=None, description="Filter emails by sender address.")] = None,
    to_address: Annotated[
        str | None,
        Field(default=None, description="Filter emails by recipient address."),
    ] = None,
    order: Annotated[
        Literal["asc", "desc"],
        Field(default=None, description="Order emails by field. `asc` or `desc`."),
    ] = "desc",
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox to search.")] = "INBOX",
    seen: Annotated[
        bool | None,
        Field(default=None, description="Filter by read status: True=read, False=unread, None=all."),
    ] = None,
    flagged: Annotated[
        bool | None,
        Field(default=None, description="Filter by flagged/starred status: True=flagged, False=unflagged, None=all."),
    ] = None,
    answered: Annotated[
        bool | None,
        Field(default=None, description="Filter by replied status: True=replied, False=not replied, None=all."),
    ] = None,
    body: Annotated[
        str | None,
        Field(default=None, description="Search for text in the email body (IMAP BODY)."),
    ] = None,
    text: Annotated[
        str | None,
        Field(default=None, description="Search for text in the entire message — headers and body (IMAP TEXT)."),
    ] = None,
    has_attachment: Annotated[
        bool | None,
        Field(
            default=None,
            description="Filter by attachment presence: True=has attachment, False=none, None=all "
            "(multipart/mixed heuristic; may miss inline images or yield false positives).",
        ),
    ] = None,
) -> EmailMetadataPageResponse:
    handler = dispatch_handler(account_name)

    return await handler.get_emails_metadata(
        page=page,
        page_size=page_size,
        before=before,
        since=since,
        subject=subject,
        from_address=from_address,
        to_address=to_address,
        order=order,
        mailbox=mailbox,
        seen=seen,
        flagged=flagged,
        answered=answered,
        body=body,
        text=text,
        has_attachment=has_attachment,
    )


@mcp.tool(
    description="Get the full content (including body) of one or more emails by their email_id. Use list_emails_metadata first to get the email_id."
)
async def get_emails_content(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(
            description="List of email_id to retrieve (obtained from list_emails_metadata). Can be a single email_id or multiple email_ids."
        ),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox to retrieve emails from.")] = "INBOX",
    mark_as_read: Annotated[
        bool,
        Field(
            default=False,
            description="If True, mark each successfully retrieved email as read. If marking fails, a warning is logged and retrieval still succeeds.",
        ),
    ] = False,
    body_offset: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description="Character offset into each email body to start reading from. Use together "
            "with max_body_length to page through long emails: if a returned body ends with the "
            "'...[TRUNCATED]' marker, fetch the next chunk with body_offset += max_body_length.",
        ),
    ] = 0,
    max_body_length: Annotated[
        int,
        Field(
            default=20000,
            ge=1,
            le=100000,
            description="Maximum number of body characters to return, counted from body_offset. "
            "If the body extends past this window, the '...[TRUNCATED]' marker is appended after "
            "the requested body window.",
        ),
    ] = 20000,
) -> EmailContentBatchResponse:
    handler = dispatch_handler(account_name)
    return await handler.get_emails_content(email_ids, mailbox, mark_as_read, body_offset, max_body_length)


@mcp.tool(
    description=(
        "List the configured recipient allowlist — the addresses that send_email is permitted to "
        "send to and save_to_mailbox is permitted to address. Only available when an allowlist is configured."
    ),
    visible_if=_has_allowed_recipients,
)
async def list_allowed_recipients() -> list[str]:
    return get_settings().allowed_recipients


@mcp.tool(
    description=(
        "List the configured inbound sender allowlist — the address patterns whose mail the server "
        "will read or act on. When configured, only these senders' mail is visible to the read tools "
        "(list_emails_metadata, get_emails_content, download_attachment) and eligible for the mutation "
        "tools (delete_emails, mark_emails_as_read, move_emails, archive_emails). Only available when an "
        "allowlist is configured."
    ),
    visible_if=_has_allowed_senders,
)
async def list_allowed_senders() -> list[str]:
    return get_settings().allowed_senders


@mcp.tool(
    description="Send an email using the specified account. Supports replying to emails with proper threading when in_reply_to is provided.",
    visible_if=_has_send_capable_account,
)
async def send_email(
    account_name: Annotated[str, Field(description="The name of the email account to send from.")],
    recipients: Annotated[list[str], Field(description="A list of recipient email addresses.")],
    subject: Annotated[str, Field(description="The subject of the email.")],
    body: Annotated[str, Field(description="The body of the email.")],
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of CC email addresses."),
    ] = None,
    bcc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of BCC email addresses."),
    ] = None,
    html: Annotated[
        bool,
        Field(default=False, description="Whether to send the email as HTML (True) or plain text (False)."),
    ] = False,
    attachments: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="A list of absolute file paths to attach to the email. Supports common file types (documents, images, archives, etc.).",
        ),
    ] = None,
    in_reply_to: Annotated[
        str | None,
        Field(
            default=None,
            description="Message-ID of the email being replied to. Enables proper threading in email clients.",
        ),
    ] = None,
    references: Annotated[
        str | None,
        Field(
            default=None,
            description="Space-separated Message-IDs for the thread chain. Usually includes in_reply_to plus ancestors.",
        ),
    ] = None,
    reply_to: Annotated[
        str | None,
        Field(
            default=None,
            description="Email address to set as the Reply-To header. When set, email clients will reply to this address instead of the From address.",
        ),
    ] = None,
) -> str:
    _enforce_recipient_allowlist(recipients, cc, bcc)
    handler = dispatch_handler(account_name)
    await handler.send_email(
        recipients,
        subject,
        body,
        cc,
        bcc,
        html,
        attachments,
        in_reply_to,
        references,
        reply_to,
    )
    recipient_str = ", ".join(recipients)
    attachment_info = f" with {len(attachments)} attachment(s)" if attachments else ""
    return f"Email sent successfully to {recipient_str}{attachment_info}"


@mcp.tool(
    description="Compose an email and save it to an IMAP folder (e.g., Drafts). "
    "Same parameters as send_email, but saves instead of sending. "
    "Default folder is Drafts with \\Draft and \\Seen flags. "
    "Pure IMAP operation — works without SMTP configuration.",
)
async def save_to_mailbox(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    recipients: Annotated[list[str], Field(description="A list of recipient email addresses.")],
    subject: Annotated[str, Field(description="The subject of the email.")],
    body: Annotated[str, Field(description="The body of the email.")],
    mailbox: Annotated[
        str,
        Field(
            default="Drafts",
            description="The IMAP folder to save to (e.g., 'Drafts', 'INBOX.Drafts', 'Templates').",
        ),
    ] = "Drafts",
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of CC email addresses."),
    ] = None,
    bcc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of BCC email addresses."),
    ] = None,
    html: Annotated[
        bool,
        Field(default=False, description="Whether the email body is HTML (True) or plain text (False)."),
    ] = False,
    attachments: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="A list of absolute file paths to attach to the email.",
        ),
    ] = None,
    in_reply_to: Annotated[
        str | None,
        Field(
            default=None,
            description="Message-ID of the email being replied to. Enables proper threading in email clients.",
        ),
    ] = None,
    references: Annotated[
        str | None,
        Field(
            default=None,
            description="Space-separated Message-IDs for the thread chain.",
        ),
    ] = None,
    flags: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=r"IMAP flags to set on the message. Defaults to ['\\Draft', '\\Seen']. Common flags: '\\Draft', '\\Seen', '\\Flagged'.",
        ),
    ] = None,
) -> str:
    _enforce_recipient_allowlist(recipients, cc, bcc)
    handler = dispatch_handler(account_name)
    result = await handler.save_to_mailbox(
        recipients,
        subject,
        body,
        mailbox,
        cc,
        bcc,
        html,
        attachments,
        in_reply_to,
        references,
        flags,
    )
    # result format: "<message-id>|uid:<imap-uid>"
    parts = result.split("|uid:")
    message_id = parts[0]
    email_id = parts[1] if len(parts) > 1 else "unknown"
    return f"Email saved to '{mailbox}' successfully. Message-Id: {message_id}, email_id: {email_id}"


@mcp.tool(
    description="Delete one or more emails by their email_id. Use list_emails_metadata first to get the email_id."
)
async def delete_emails(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="List of email_id to delete (obtained from list_emails_metadata)."),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox to delete emails from.")] = "INBOX",
) -> str:
    handler = dispatch_handler(account_name)
    deleted_ids, failed_ids = await handler.delete_emails(email_ids, mailbox)

    result = f"Successfully deleted {len(deleted_ids)} email(s)"
    if failed_ids:
        result += f", failed to delete {len(failed_ids)} email(s): {', '.join(failed_ids)}"
    return result


@mcp.tool(
    description="Mark one or more emails as read by their email_id. Use list_emails_metadata first to get the email_id."
)
async def mark_emails_as_read(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="List of email_id to mark as read (obtained from list_emails_metadata)."),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox containing the emails.")] = "INBOX",
) -> str:
    handler = dispatch_handler(account_name)
    marked_ids, failed_ids = await handler.mark_emails_as_read(email_ids, mailbox)

    result = f"Successfully marked {len(marked_ids)} email(s) as read"
    if failed_ids:
        result += f", failed to mark {len(failed_ids)} email(s): {', '.join(failed_ids)}"
    return result


@mcp.tool(
    description="Move one or more emails between IMAP folders by their email_id. Use list_emails_metadata first to get the email_id and list_mailboxes to discover available folders."
)
async def move_emails(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="List of email_id to move (obtained from list_emails_metadata)."),
    ],
    destination_mailbox: Annotated[str, Field(description="The destination mailbox/folder to move emails to.")],
    source_mailbox: Annotated[
        str, Field(default="INBOX", description="The source mailbox containing the emails.")
    ] = "INBOX",
) -> str:
    handler = dispatch_handler(account_name)
    moved_ids, failed_ids = await handler.move_emails(email_ids, source_mailbox, destination_mailbox)

    result = f"Successfully moved {len(moved_ids)} email(s) to {destination_mailbox}"
    if failed_ids:
        result += f", failed to move {len(failed_ids)} email(s): {', '.join(failed_ids)}"
    return result


@mcp.tool(
    description="Archive one or more emails by moving them to the account's Archive folder, "
    "auto-detected via the RFC 6154 \\Archive flag (falling back to common names like Archive or "
    "[Gmail]/All Mail). Use list_emails_metadata first to get the email_id."
)
async def archive_emails(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="List of email_id to archive (obtained from list_emails_metadata)."),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="The source mailbox containing the emails.")] = "INBOX",
) -> str:
    handler = dispatch_handler(account_name)
    archived_ids, failed_ids, archive_folder = await handler.archive_emails(email_ids, mailbox)

    result = f"Successfully archived {len(archived_ids)} email(s) to {archive_folder}"
    if failed_ids:
        result += f", failed to archive {len(failed_ids)} email(s): {', '.join(failed_ids)}"
    return result


@mcp.tool(
    description="List available mailboxes/folders for an email account. Returns folder names, hierarchy delimiters, and flags. Useful for discovering folder names before moving emails."
)
async def list_mailboxes(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    pattern: Annotated[
        str,
        Field(default="*", description="IMAP LIST pattern. Use '*' for all folders, 'INBOX.*' for INBOX children."),
    ] = "*",
    reference: Annotated[
        str,
        Field(default="", description="IMAP LIST reference name (namespace prefix). Usually empty."),
    ] = "",
) -> list[MailboxInfo]:
    handler = dispatch_handler(account_name)
    return await handler.list_mailboxes(pattern, reference)


@mcp.tool(
    description="Download an email attachment and save it to the specified path. This feature must be explicitly enabled in settings (enable_attachment_download=true) due to security considerations.",
)
async def download_attachment(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_id: Annotated[
        str, Field(description="The email ID (obtained from list_emails_metadata or get_emails_content).")
    ],
    attachment_name: Annotated[
        str, Field(description="The name of the attachment to download (as shown in the attachments list).")
    ],
    save_path: Annotated[str, Field(description="The absolute path where the attachment should be saved.")],
    mailbox: Annotated[str, Field(description="The mailbox to search in (default: INBOX).")] = "INBOX",
) -> AttachmentDownloadResponse:
    settings = get_settings()
    if not settings.enable_attachment_download:
        msg = (
            "Attachment download is disabled. Set 'enable_attachment_download=true' in settings to enable this feature."
        )
        raise PermissionError(msg)

    handler = dispatch_handler(account_name)
    return await handler.download_attachment(email_id, attachment_name, save_path, mailbox)
