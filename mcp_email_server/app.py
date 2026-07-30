from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.metadata import version
from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_email_server.application.accounts import AvailableAccount, EffectiveConfiguration
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.metadata import ListEmailMetadataQuery
from mcp_email_server.application.mutations import (
    MUTABLE_EMAIL_FLAGS,
    AppendMutationOutcome,
    ArchiveCommand,
    ArchiveMutationOutcome,
    BatchMutationOutcome,
    DeleteCommand,
    FlagOperation,
    MarkReadCommand,
    MoveCommand,
    MutableEmailFlag,
    RecipientPolicyDeniedError,
    SaveToMailboxCommand,
    SendCommand,
    SendMutationOutcome,
    SetEmailFlagsCommand,
    TargetMutationOutcome,
)
from mcp_email_server.application.reads import (
    DownloadAttachmentCommand,
    GetEmailContentQuery,
    ListMailboxesQuery,
)
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailContentBatchResponse,
    EmailMetadataPageResponse,
    MailboxInfo,
)
from mcp_email_server.runtime import close_application_runtime, get_application_runtime

MAX_IMAP_UID_CHARACTERS = len(str(APPLICATION_LIMITS.maximum_imap_uid))
UidInput = Annotated[str, Field(max_length=MAX_IMAP_UID_CHARACTERS, pattern=r"^[1-9][0-9]*$")]
AddressInput = Annotated[str, Field(max_length=APPLICATION_LIMITS.address_bytes)]
AttachmentPathInput = Annotated[str, Field(max_length=APPLICATION_LIMITS.attachment_path_bytes)]
FlagInput = Annotated[str, Field(max_length=APPLICATION_LIMITS.flag_bytes)]
AccountDiscoveryResult = Annotated[
    list[AvailableAccount],
    Field(max_length=APPLICATION_LIMITS.configured_accounts),
]
PolicyDiscoveryResult = Annotated[
    list[str],
    Field(max_length=APPLICATION_LIMITS.policy_entries),
]


def list_effective_accounts() -> list[AvailableAccount]:
    return get_application_runtime().accounts.discover()


async def list_email_metadata(query: ListEmailMetadataQuery) -> EmailMetadataPageResponse:
    return await get_application_runtime().metadata.execute(query)


async def send_email_command(command: SendCommand) -> SendMutationOutcome:
    return await get_application_runtime().mutations.send.execute(command)


async def save_to_mailbox_command(command: SaveToMailboxCommand) -> AppendMutationOutcome:
    return await get_application_runtime().mutations.save_to_mailbox.execute(command)


async def delete_emails_command(command: DeleteCommand) -> BatchMutationOutcome:
    return await get_application_runtime().mutations.delete.execute(command)


async def set_email_flags_command(command: SetEmailFlagsCommand) -> BatchMutationOutcome:
    return await get_application_runtime().mutations.set_flags.execute(command)


async def mark_read_command(command: MarkReadCommand) -> BatchMutationOutcome:
    return await get_application_runtime().mutations.mark_read.execute(command)


async def move_emails_command(command: MoveCommand) -> BatchMutationOutcome:
    return await get_application_runtime().mutations.move.execute(command)


async def archive_emails_command(command: ArchiveCommand) -> ArchiveMutationOutcome:
    return await get_application_runtime().mutations.archive.execute(command)


async def get_email_content_query(query: GetEmailContentQuery) -> EmailContentBatchResponse:
    return await get_application_runtime().reads.content.execute(query)


async def list_mailboxes_query(query: ListMailboxesQuery) -> list[MailboxInfo]:
    return await get_application_runtime().reads.mailboxes.execute(query)


async def download_attachment_command(command: DownloadAttachmentCommand) -> AttachmentDownloadResponse:
    return await get_application_runtime().reads.attachments.execute(command)


def effective_configuration() -> EffectiveConfiguration:
    return get_application_runtime().configuration.execute()


def _ordered_target_sections(
    outcomes: tuple[TargetMutationOutcome, ...],
    *,
    include_unknown_detail: bool,
) -> list[str]:
    """Format contiguous statuses without reordering input-aligned outcomes."""
    sections: list[str] = []
    current_status: str | None = None
    current_targets: list[str] = []
    for item in outcomes:
        if item.status != current_status and current_targets:
            sections.append(f"{current_status}: {', '.join(current_targets)}")
            current_targets = []
        current_status = item.status
        target = item.target
        if include_unknown_detail and item.status == "unknown" and item.detail is not None:
            target = f"{target} ({item.detail})"
        current_targets.append(target)
    if current_targets:
        sections.append(f"{current_status}: {', '.join(current_targets)}")
    return sections


def _tagged_batch_result(outcome: BatchMutationOutcome) -> str:
    sections = _ordered_target_sections(outcome.outcomes, include_unknown_detail=True)
    if outcome.reconciliation_needed:
        sections.append("warning: reconciliation needed")
    return "; ".join(sections)


def _tagged_send_result(outcome: SendMutationOutcome) -> str:
    sections = _ordered_target_sections(outcome.delivery, include_unknown_detail=False)
    sent_copy = outcome.sent_copy.status
    if outcome.sent_copy.mailbox:
        sent_copy = f"{sent_copy} ({outcome.sent_copy.mailbox})"
    sections.append(f"sent-copy: {sent_copy}")
    if outcome.reconciliation_needed:
        sections.append("warning: reconciliation needed")
    return "; ".join(sections)


@asynccontextmanager
async def _application_lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
    try:
        yield {}
    finally:
        with anyio.CancelScope(shield=True):
            await close_application_runtime()


mcp = FastMCP("email", lifespan=_application_lifespan)
# FastMCP 1.x does not expose its low-level server version in the constructor.
mcp._mcp_server.version = version("mcp-email-server")  # pyright: ignore[reportPrivateUsage]

_READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_READ_ONLY_REMOTE = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_NONDESTRUCTIVE_REMOTE_MUTATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_IDEMPOTENT_REMOTE_MUTATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_DESTRUCTIVE_REMOTE_MUTATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
_FILESYSTEM_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


@mcp.resource(
    "email://{account_name}",
    description="Return stable non-secret discovery capabilities for one configured account.",
)
async def get_account(account_name: str) -> AvailableAccount | None:
    return get_application_runtime().accounts.discover_one(account_name)


@mcp.tool(
    description=(
        "List configured accounts as stable non-secret capability records. Use only accounts with "
        "can_receive=true for mail reads and can_send=true for send_email. If the result is empty, ask the "
        "user to run `mcp-email-server ui` or the user-operated CLI; never ask for credentials in chat."
    ),
    annotations=_READ_ONLY_LOCAL,
)
async def list_available_accounts() -> AccountDiscoveryResult:
    return list_effective_accounts()


@mcp.tool(
    description="List email metadata (email_id, subject, sender, recipients, date) without body content. Returns email_id for use with get_emails_content.",
    annotations=_READ_ONLY_REMOTE,
)
async def list_emails_metadata(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    page: Annotated[
        int,
        Field(default=1, ge=1, description="The page number to retrieve (starting from 1)."),
    ] = 1,
    page_size: Annotated[
        int,
        Field(default=10, ge=1, le=100, description="The number of emails to retrieve per page."),
    ] = 10,
    before: Annotated[
        datetime | None,
        Field(default=None, description="Retrieve emails before this datetime (UTC)."),
    ] = None,
    since: Annotated[
        datetime | None,
        Field(default=None, description="Retrieve emails since this datetime (UTC)."),
    ] = None,
    subject: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.query_bytes,
            description="Filter emails by subject.",
        ),
    ] = None,
    from_address: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.address_bytes,
            description="Filter emails by sender address.",
        ),
    ] = None,
    to_address: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.address_bytes,
            description="Filter emails by recipient address.",
        ),
    ] = None,
    order: Annotated[
        Literal["asc", "desc"],
        Field(default=None, description="Sort matching emails by date: oldest first (`asc`) or newest first (`desc`)."),
    ] = "desc",
    mailbox: Annotated[
        str,
        Field(
            default="INBOX",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The mailbox to search.",
        ),
    ] = "INBOX",
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
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.query_bytes,
            description="Search for text in the email body (IMAP BODY).",
        ),
    ] = None,
    text: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.query_bytes,
            description="Search for text in the entire message — headers and body (IMAP TEXT).",
        ),
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
    return await list_email_metadata(
        ListEmailMetadataQuery(
            account_name=account_name,
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
    )


@mcp.tool(
    description=(
        "Get the full content (including body and reply-thread headers) of one or more emails by their email_id. "
        "Use list_emails_metadata first. This tool is non-read-only because mark_as_read=true changes remote flags."
    ),
    annotations=_IDEMPOTENT_REMOTE_MUTATION,
)
async def get_emails_content(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    email_ids: Annotated[
        list[UidInput],
        Field(
            min_length=1,
            max_length=APPLICATION_LIMITS.content_email_ids,
            description="One or more email_id values to retrieve, supplied as an array (obtained from list_emails_metadata).",
        ),
    ],
    mailbox: Annotated[
        str,
        Field(
            default="INBOX",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The mailbox to retrieve emails from.",
        ),
    ] = "INBOX",
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
    return await get_email_content_query(
        GetEmailContentQuery(
            account_name=account_name,
            email_ids=tuple(email_ids),
            mailbox=mailbox,
            mark_as_read=mark_as_read,
            body_offset=body_offset,
            max_body_length=max_body_length,
        )
    )


@mcp.tool(
    description=(
        "List the configured recipient allowlist — the addresses that send_email is permitted to "
        "send to and save_to_mailbox is permitted to address. Returns an empty list when unrestricted."
    ),
    annotations=_READ_ONLY_LOCAL,
)
async def list_allowed_recipients() -> PolicyDiscoveryResult:
    return list(effective_configuration().allowed_recipients)


@mcp.tool(
    description=(
        "List the configured inbound sender allowlist — the address patterns whose mail the server "
        "will read or act on. When configured, only these senders' mail is visible to the read tools "
        "(list_emails_metadata, get_emails_content, download_attachment) and eligible for the mutation "
        "tools (delete_emails, set_email_flags, mark_emails_as_read, move_emails, archive_emails). Returns an "
        "empty list "
        "when unrestricted."
    ),
    annotations=_READ_ONLY_LOCAL,
)
async def list_allowed_senders() -> PolicyDiscoveryResult:
    return list(effective_configuration().allowed_senders)


@mcp.tool(
    description=(
        "Send one email using the specified account. Supports reply threading. Partial or ambiguous SMTP "
        "delivery reports per-recipient succeeded/failed/unknown status and reports the independent Sent-copy "
        "outcome separately; ambiguous effects are not retried automatically."
    ),
    annotations=_NONDESTRUCTIVE_REMOTE_MUTATION,
)
async def send_email(
    account_name: Annotated[
        str,
        Field(
            max_length=APPLICATION_LIMITS.account_name_bytes,
            description="The name of the email account to send from.",
        ),
    ],
    recipients: Annotated[
        list[AddressInput],
        Field(
            min_length=1, max_length=APPLICATION_LIMITS.recipients, description="A list of recipient email addresses."
        ),
    ],
    subject: Annotated[
        str,
        Field(max_length=APPLICATION_LIMITS.subject_bytes, description="The subject of the email."),
    ],
    body: Annotated[
        str,
        Field(max_length=APPLICATION_LIMITS.body_bytes, description="The body of the email."),
    ],
    cc: Annotated[
        list[AddressInput] | None,
        Field(default=None, max_length=APPLICATION_LIMITS.recipients, description="A list of CC email addresses."),
    ] = None,
    bcc: Annotated[
        list[AddressInput] | None,
        Field(default=None, max_length=APPLICATION_LIMITS.recipients, description="A list of BCC email addresses."),
    ] = None,
    html: Annotated[
        bool,
        Field(default=False, description="Whether to send the email as HTML (True) or plain text (False)."),
    ] = False,
    attachments: Annotated[
        list[AttachmentPathInput] | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.attachments,
            description="A list of file paths to attach. Relative paths are resolved against the server process working directory; absolute paths are recommended.",
        ),
    ] = None,
    in_reply_to: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.header_bytes,
            description="Message-ID of the email being replied to. Enables proper threading in email clients.",
        ),
    ] = None,
    references: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.header_bytes,
            description="Space-separated Message-IDs for the thread chain. Usually includes in_reply_to plus ancestors.",
        ),
    ] = None,
    reply_to: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.header_bytes,
            description="Email address to set as the Reply-To header. When set, email clients will reply to this address instead of the From address.",
        ),
    ] = None,
) -> str:
    try:
        outcome = await send_email_command(
            SendCommand(
                account_name=account_name,
                recipients=tuple(recipients),
                subject=subject,
                body=body,
                cc=tuple(cc or ()),
                bcc=tuple(bcc or ()),
                html=html,
                attachments=tuple(attachments or ()),
                in_reply_to=in_reply_to,
                references=references,
                reply_to=reply_to,
            )
        )
    except RecipientPolicyDeniedError as exc:
        raise ValueError("Recipient(s) not in allowlist") from exc
    if (
        all(item.status == "succeeded" for item in outcome.delivery)
        and outcome.sent_copy.status in ("succeeded", "skipped")
        and not outcome.reconciliation_needed
    ):
        recipient_str = ", ".join(recipients)
        attachment_info = f" with {len(attachments)} attachment(s)" if attachments else ""
        return f"Email sent successfully to {recipient_str}{attachment_info}"
    return f"Email delivery [{_tagged_send_result(outcome)}]"


@mcp.tool(
    description="Compose an email and save it to an IMAP folder (e.g., Drafts). "
    "Shares recipient, body, attachment, and threading parameters with send_email; "
    "adds mailbox and flags, and does not support reply_to. "
    "Default folder is Drafts with \\Draft and \\Seen flags. "
    "Pure IMAP operation — works without SMTP configuration. An ambiguous APPEND is reported as unknown "
    "and is not retried automatically.",
    annotations=_NONDESTRUCTIVE_REMOTE_MUTATION,
)
async def save_to_mailbox(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    recipients: Annotated[
        list[AddressInput],
        Field(
            min_length=1, max_length=APPLICATION_LIMITS.recipients, description="A list of recipient email addresses."
        ),
    ],
    subject: Annotated[
        str,
        Field(max_length=APPLICATION_LIMITS.subject_bytes, description="The subject of the email."),
    ],
    body: Annotated[
        str,
        Field(max_length=APPLICATION_LIMITS.body_bytes, description="The body of the email."),
    ],
    mailbox: Annotated[
        str,
        Field(
            default="Drafts",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The IMAP folder to save to (e.g., 'Drafts', 'INBOX.Drafts', 'Templates').",
        ),
    ] = "Drafts",
    cc: Annotated[
        list[AddressInput] | None,
        Field(default=None, max_length=APPLICATION_LIMITS.recipients, description="A list of CC email addresses."),
    ] = None,
    bcc: Annotated[
        list[AddressInput] | None,
        Field(default=None, max_length=APPLICATION_LIMITS.recipients, description="A list of BCC email addresses."),
    ] = None,
    html: Annotated[
        bool,
        Field(default=False, description="Whether the email body is HTML (True) or plain text (False)."),
    ] = False,
    attachments: Annotated[
        list[AttachmentPathInput] | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.attachments,
            description="A list of file paths to attach. Relative paths are resolved against the server process working directory; absolute paths are recommended.",
        ),
    ] = None,
    in_reply_to: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.header_bytes,
            description="Message-ID of the email being replied to. Enables proper threading in email clients.",
        ),
    ] = None,
    references: Annotated[
        str | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.header_bytes,
            description="Space-separated Message-IDs for the thread chain.",
        ),
    ] = None,
    flags: Annotated[
        list[FlagInput] | None,
        Field(
            default=None,
            max_length=APPLICATION_LIMITS.flags,
            description=r"IMAP flags to set on the message. Defaults to ['\Draft', '\Seen']. Common flags: '\Draft', '\Seen', '\Flagged'.",
        ),
    ] = None,
) -> str:
    try:
        outcome = await save_to_mailbox_command(
            SaveToMailboxCommand(
                account_name=account_name,
                recipients=tuple(recipients),
                subject=subject,
                body=body,
                mailbox=mailbox,
                cc=tuple(cc or ()),
                bcc=tuple(bcc or ()),
                html=html,
                attachments=tuple(attachments or ()),
                in_reply_to=in_reply_to,
                references=references,
                flags=tuple(flags) if flags is not None else None,
            )
        )
    except RecipientPolicyDeniedError as exc:
        raise ValueError("Recipient(s) not in allowlist") from exc
    if outcome.status == "succeeded" and not outcome.reconciliation_needed:
        email_id = outcome.uid or "unknown"
        return f"Email saved to '{mailbox}' successfully. Message-Id: {outcome.message_id}, email_id: {email_id}"
    detail = f" ({outcome.detail})" if outcome.status == "unknown" and outcome.detail else ""
    warning = "; warning: reconciliation needed" if outcome.reconciliation_needed else ""
    return f"Email save [{outcome.status}{detail}: {mailbox}; Message-Id: {outcome.message_id}{warning}]"


@mcp.tool(
    description=(
        "Delete one or more emails by email_id using target-scoped UID EXPUNGE. Use list_emails_metadata first. "
        "Partial or ambiguous effects report per-ID succeeded/failed/unknown status and are not retried automatically."
    ),
    annotations=_DESTRUCTIVE_REMOTE_MUTATION,
)
async def delete_emails(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    email_ids: Annotated[
        list[UidInput],
        Field(
            min_length=1,
            max_length=APPLICATION_LIMITS.mutation_uids,
            description="List of email_id to delete (obtained from list_emails_metadata).",
        ),
    ],
    mailbox: Annotated[
        str,
        Field(
            default="INBOX",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The mailbox to delete emails from.",
        ),
    ] = "INBOX",
) -> str:
    outcome = await delete_emails_command(DeleteCommand(account_name, tuple(email_ids), mailbox))
    succeeded = outcome.targets("succeeded")
    if len(succeeded) == len(email_ids) and not outcome.reconciliation_needed:
        return f"Successfully deleted {len(succeeded)} email(s)"
    return f"Delete result [{_tagged_batch_result(outcome)}]"


@mcp.tool(
    description=(
        "Add or remove approved IMAP flags on one or more emails by email_id. Supported flags are \\Seen, "
        "\\Flagged, \\Answered, and \\Draft; \\Deleted and provider-specific keywords are not supported. "
        "Use list_emails_metadata first. Partial or ambiguous effects report per-ID succeeded/failed/unknown "
        "status and are not retried automatically."
    ),
    annotations=_IDEMPOTENT_REMOTE_MUTATION,
)
async def set_email_flags(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    email_ids: Annotated[
        list[UidInput],
        Field(
            min_length=1,
            max_length=APPLICATION_LIMITS.mutation_uids,
            description="List of email_id values whose flags should be changed.",
        ),
    ],
    operation: Annotated[
        FlagOperation,
        Field(description="Whether to add or remove every supplied flag."),
    ],
    flags: Annotated[
        list[MutableEmailFlag],
        Field(
            min_length=1,
            max_length=len(MUTABLE_EMAIL_FLAGS),
            description="Unique approved flags to add or remove: \\Seen, \\Flagged, \\Answered, or \\Draft.",
        ),
    ],
    mailbox: Annotated[
        str,
        Field(
            default="INBOX",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The mailbox containing the emails.",
        ),
    ] = "INBOX",
) -> str:
    outcome = await set_email_flags_command(
        SetEmailFlagsCommand(account_name, tuple(email_ids), operation, tuple(flags), mailbox)
    )
    succeeded = outcome.targets("succeeded")
    if len(succeeded) == len(email_ids) and not outcome.reconciliation_needed:
        verb, preposition = ("added", "to") if operation == "add" else ("removed", "from")
        return f"Successfully {verb} {', '.join(flags)} {preposition} {len(succeeded)} email(s)"
    return f"Set-flags result [{_tagged_batch_result(outcome)}]"


@mcp.tool(
    description=(
        "Mark one or more emails as read by email_id. This is the common-workflow equivalent of adding \\Seen "
        "with set_email_flags. Use list_emails_metadata first. Partial or ambiguous effects report per-ID "
        "succeeded/failed/unknown status and are not retried automatically."
    ),
    annotations=_IDEMPOTENT_REMOTE_MUTATION,
)
async def mark_emails_as_read(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    email_ids: Annotated[
        list[UidInput],
        Field(
            min_length=1,
            max_length=APPLICATION_LIMITS.mutation_uids,
            description="List of email_id to mark as read (obtained from list_emails_metadata).",
        ),
    ],
    mailbox: Annotated[
        str,
        Field(
            default="INBOX",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The mailbox containing the emails.",
        ),
    ] = "INBOX",
) -> str:
    outcome = await mark_read_command(MarkReadCommand(account_name, tuple(email_ids), mailbox))
    succeeded = outcome.targets("succeeded")
    if len(succeeded) == len(email_ids) and not outcome.reconciliation_needed:
        return f"Successfully marked {len(succeeded)} email(s) as read"
    return f"Mark-read result [{_tagged_batch_result(outcome)}]"


@mcp.tool(
    description=(
        "Move one or more emails between IMAP folders by email_id. Use list_emails_metadata and list_mailboxes "
        "first. Partial or ambiguous effects report per-ID succeeded/failed/unknown status and are not retried."
    ),
    annotations=_DESTRUCTIVE_REMOTE_MUTATION,
)
async def move_emails(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    email_ids: Annotated[
        list[UidInput],
        Field(
            min_length=1,
            max_length=APPLICATION_LIMITS.mutation_uids,
            description="List of email_id to move (obtained from list_emails_metadata).",
        ),
    ],
    destination_mailbox: Annotated[
        str,
        Field(
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The destination mailbox/folder to move emails to.",
        ),
    ],
    source_mailbox: Annotated[
        str,
        Field(
            default="INBOX",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The source mailbox containing the emails.",
        ),
    ] = "INBOX",
) -> str:
    outcome = await move_emails_command(
        MoveCommand(account_name, tuple(email_ids), source_mailbox, destination_mailbox)
    )
    succeeded = outcome.targets("succeeded")
    if len(succeeded) == len(email_ids) and not outcome.reconciliation_needed:
        return f"Successfully moved {len(succeeded)} email(s) to {destination_mailbox}"
    return f"Move result [{_tagged_batch_result(outcome)}]"


@mcp.tool(
    description="Archive one or more emails by moving them to the account's Archive folder, "
    "auto-detected via the RFC 6154 \\Archive flag (falling back to common names like Archive or "
    "[Gmail]/All Mail). Use list_emails_metadata first. Partial or ambiguous effects report per-ID "
    "succeeded/failed/unknown status and are not retried automatically.",
    annotations=_DESTRUCTIVE_REMOTE_MUTATION,
)
async def archive_emails(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    email_ids: Annotated[
        list[UidInput],
        Field(
            min_length=1,
            max_length=APPLICATION_LIMITS.mutation_uids,
            description="List of email_id to archive (obtained from list_emails_metadata).",
        ),
    ],
    mailbox: Annotated[
        str,
        Field(
            default="INBOX",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The source mailbox containing the emails.",
        ),
    ] = "INBOX",
) -> str:
    outcome = await archive_emails_command(ArchiveCommand(account_name, tuple(email_ids), mailbox))
    succeeded = outcome.batch.targets("succeeded")
    if len(succeeded) == len(email_ids) and not outcome.batch.reconciliation_needed:
        return f"Successfully archived {len(succeeded)} email(s) to {outcome.archive_mailbox}"
    return f"Archive result [{_tagged_batch_result(outcome.batch)}; mailbox: {outcome.archive_mailbox}]"


@mcp.tool(
    description="List available mailboxes/folders for an email account. Returns folder names, hierarchy delimiters, and flags. Useful for discovering folder names before moving emails.",
    annotations=_READ_ONLY_REMOTE,
)
async def list_mailboxes(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    pattern: Annotated[
        str,
        Field(
            default="*",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="IMAP LIST pattern. Use '*' for all folders, 'INBOX.*' for INBOX children.",
        ),
    ] = "*",
    reference: Annotated[
        str,
        Field(
            default="",
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="IMAP LIST reference name (namespace prefix). Usually empty.",
        ),
    ] = "",
) -> list[MailboxInfo]:
    return await list_mailboxes_query(
        ListMailboxesQuery(account_name=account_name, pattern=pattern, reference=reference)
    )


@mcp.tool(
    description="Download an email attachment and save it to the specified path. This feature must be explicitly enabled in settings (enable_attachment_download=true) due to security considerations.",
    annotations=_FILESYSTEM_WRITE,
)
async def download_attachment(
    account_name: Annotated[
        str, Field(max_length=APPLICATION_LIMITS.account_name_bytes, description="The name of the email account.")
    ],
    email_id: Annotated[
        str,
        Field(
            max_length=MAX_IMAP_UID_CHARACTERS,
            description="The email ID (obtained from list_emails_metadata or get_emails_content).",
        ),
    ],
    attachment_name: Annotated[
        str,
        Field(
            max_length=APPLICATION_LIMITS.attachment_path_bytes,
            description="The name of the attachment to download (as shown in the attachments list).",
        ),
    ],
    save_path: Annotated[
        str,
        Field(
            max_length=APPLICATION_LIMITS.attachment_path_bytes,
            description="The destination path. Relative paths are resolved against the server process working directory; absolute paths are recommended.",
        ),
    ],
    mailbox: Annotated[
        str,
        Field(
            max_length=APPLICATION_LIMITS.mailbox_bytes,
            description="The mailbox to search in (default: INBOX).",
        ),
    ] = "INBOX",
) -> AttachmentDownloadResponse:
    return await download_attachment_command(
        DownloadAttachmentCommand(
            account_name=account_name,
            email_id=email_id,
            attachment_name=attachment_name,
            save_path=save_path,
            mailbox=mailbox,
        )
    )
