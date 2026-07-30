from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import TypeAdapter

from mcp_email_server.application.limits import (
    APPLICATION_LIMITS,
    validate_controlled_string,
    validate_imap_uid,
    validate_serialized_result,
)
from mcp_email_server.application.metadata import RuntimeMode
from mcp_email_server.application.mutations import (
    BatchMutationOutcome,
    MarkReadCommand,
)
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailContentBatchResponse,
    MailboxInfo,
)
from mcp_email_server.log import logger

MAX_CONTENT_EMAIL_IDS = APPLICATION_LIMITS.content_email_ids
ProviderResultT = TypeVar("ProviderResultT")


async def _bounded_provider_call(operation: Awaitable[ProviderResultT]) -> ProviderResultT:
    try:
        async with asyncio.timeout(APPLICATION_LIMITS.provider_timeout_seconds):
            return await operation
    except TimeoutError:
        raise ReadProviderError("provider request timed out") from None


class ReadProviderError(RuntimeError):
    """A read provider failed without exposing transport-controlled detail."""


@dataclass(frozen=True)
class LargeResultReference:
    output_file_path: str
    output_bytes: int
    output_sha256: str
    output_media_type: str = "application/json"
    output_lifetime: str = "until this server process exits"


@dataclass(frozen=True)
class AttachmentPayload:
    email_id: str
    attachment_name: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class ReadAccountSnapshot:
    account_name: str
    mode: RuntimeMode
    allowed_senders: tuple[str, ...]
    enable_attachment_download: bool


@dataclass(frozen=True)
class ListMailboxesQuery:
    account_name: str
    pattern: str = "*"
    reference: str = ""

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        validate_controlled_string(
            self.pattern,
            field_name="mailbox pattern",
            maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
        )
        validate_controlled_string(
            self.reference,
            field_name="mailbox reference",
            maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
            allow_empty=True,
        )


@dataclass(frozen=True)
class GetEmailContentQuery:
    account_name: str
    email_ids: tuple[str, ...]
    mailbox: str = "INBOX"
    mark_as_read: bool = False
    body_offset: int = 0
    max_body_length: int = 20_000

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        _validate_email_ids(self.email_ids)
        _validate_mailbox(self.mailbox)
        if self.body_offset < 0:
            raise ValueError("body_offset must not be negative")
        if not 1 <= self.max_body_length <= 100_000:
            raise ValueError("max_body_length must be between 1 and 100000")


@dataclass(frozen=True)
class DownloadAttachmentCommand:
    account_name: str
    email_id: str
    attachment_name: str
    save_path: str
    mailbox: str = "INBOX"

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        _validate_email_ids((self.email_id,))
        _validate_mailbox(self.mailbox)
        validate_controlled_string(
            self.attachment_name,
            field_name="attachment_name",
            maximum_bytes=APPLICATION_LIMITS.attachment_path_bytes,
        )
        validate_controlled_string(
            self.save_path,
            field_name="save_path",
            maximum_bytes=APPLICATION_LIMITS.attachment_path_bytes,
        )


class ReadAccountAuthority(Protocol):
    def resolve(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode | None = None,
    ) -> ReadAccountSnapshot: ...


class ReadProvider(Protocol):
    async def list_mailboxes(self, query: ListMailboxesQuery) -> list[MailboxInfo]: ...

    async def get_content(
        self,
        query: GetEmailContentQuery,
        account: ReadAccountSnapshot,
    ) -> EmailContentBatchResponse: ...

    async def fetch_attachment(
        self,
        command: DownloadAttachmentCommand,
        account: ReadAccountSnapshot,
    ) -> AttachmentPayload: ...


@dataclass(frozen=True)
class ReadProviderAccess:
    account: ReadAccountSnapshot
    provider: ReadProvider


class ReadProviderFactory(Protocol):
    def open(self, account_name: str, *, expected_mode: RuntimeMode) -> ReadProviderAccess: ...


class ArtifactWriter(Protocol):
    async def write(self, save_path: str, payload: AttachmentPayload) -> str: ...


class LargeResultWriter(Protocol):
    async def write(self, *, prefix: str, content: bytes) -> LargeResultReference: ...

    async def aclose(self) -> None: ...


class MarkReadExecutor(Protocol):
    async def execute(self, command: MarkReadCommand) -> BatchMutationOutcome: ...


def _validate_account_name(value: str) -> None:
    validate_controlled_string(
        value,
        field_name="account_name",
        maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
    )


def _validate_mailbox(value: str) -> None:
    validate_controlled_string(
        value,
        field_name="mailbox",
        maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
    )


def _validate_email_ids(values: tuple[str, ...]) -> None:
    if not values or len(values) > MAX_CONTENT_EMAIL_IDS:
        raise ValueError(f"email_ids must contain between 1 and {MAX_CONTENT_EMAIL_IDS} values")
    for value in values:
        validate_imap_uid(value, field_name="email_ids item")


_MAILBOX_LIST_ADAPTER = TypeAdapter(list[MailboxInfo])


def _validate_mailbox_result(mailboxes: list[MailboxInfo]) -> None:
    if len(mailboxes) > APPLICATION_LIMITS.mailboxes:
        raise ReadProviderError(f"limit_exceeded: mailbox count exceeds {APPLICATION_LIMITS.mailboxes}")
    total_bytes = sum(
        len(mailbox.name.encode("utf-8"))
        + len(mailbox.delimiter.encode("utf-8"))
        + sum(len(flag.encode("utf-8")) for flag in mailbox.flags)
        for mailbox in mailboxes
    )
    if total_bytes > APPLICATION_LIMITS.mailbox_result_bytes:
        raise ReadProviderError(
            f"limit_exceeded: mailbox result exceeds {APPLICATION_LIMITS.mailbox_result_bytes} bytes"
        )
    try:
        validate_serialized_result(_MAILBOX_LIST_ADAPTER.dump_json(mailboxes))
    except ValueError:
        raise ReadProviderError("limit_exceeded: serialized mailbox result is too large") from None


def _validate_content_result(response: EmailContentBatchResponse) -> bytes:
    if len(response.failed_ids) > APPLICATION_LIMITS.warning_items:
        raise ReadProviderError(f"limit_exceeded: failed ID count exceeds {APPLICATION_LIMITS.warning_items}")
    body_sizes = [len(email.body.encode("utf-8")) for email in response.emails]
    if any(size > APPLICATION_LIMITS.body_bytes for size in body_sizes):
        raise ReadProviderError(f"limit_exceeded: an email body exceeds {APPLICATION_LIMITS.body_bytes} bytes")
    if sum(body_sizes) > APPLICATION_LIMITS.aggregate_body_bytes:
        raise ReadProviderError(
            f"limit_exceeded: email bodies exceed {APPLICATION_LIMITS.aggregate_body_bytes} bytes in total"
        )
    thread_header_sizes = [
        len(value.encode("utf-8"))
        for email in response.emails
        for value in (email.in_reply_to, email.references)
        if value is not None
    ]
    if any(size > APPLICATION_LIMITS.header_bytes for size in thread_header_sizes):
        raise ReadProviderError(
            f"limit_exceeded: an email thread header exceeds {APPLICATION_LIMITS.header_bytes} bytes"
        )
    header_bytes = sum(
        len(email.email_id.encode("utf-8"))
        + len((email.message_id or "").encode("utf-8"))
        + len((email.in_reply_to or "").encode("utf-8"))
        + len((email.references or "").encode("utf-8"))
        + len(email.subject.encode("utf-8"))
        + len(email.sender.encode("utf-8"))
        + sum(len(value.encode("utf-8")) for value in email.recipients)
        + sum(len(value.encode("utf-8")) for value in email.attachments)
        for email in response.emails
    )
    if header_bytes > APPLICATION_LIMITS.aggregate_header_bytes:
        raise ReadProviderError(
            f"limit_exceeded: email headers exceed {APPLICATION_LIMITS.aggregate_header_bytes} bytes in total"
        )
    serialized = response.model_dump_json().encode("utf-8")
    if len(serialized) > APPLICATION_LIMITS.spill_file_bytes:
        raise ReadProviderError(
            f"limit_exceeded: content result exceeds {APPLICATION_LIMITS.spill_file_bytes} spill bytes"
        )
    return serialized


class MailboxDiscoveryService:
    def __init__(self, accounts: ReadAccountAuthority, providers: ReadProviderFactory) -> None:
        self._accounts = accounts
        self._providers = providers

    async def execute(self, query: ListMailboxesQuery) -> list[MailboxInfo]:
        query.validate()
        account = self._accounts.resolve(query.account_name)
        access = self._providers.open(account.account_name, expected_mode=account.mode)
        mailboxes = await _bounded_provider_call(access.provider.list_mailboxes(query))
        _validate_mailbox_result(mailboxes)
        return mailboxes


class EmailContentService:
    def __init__(
        self,
        accounts: ReadAccountAuthority,
        providers: ReadProviderFactory,
        mark_read: MarkReadExecutor,
        large_results: LargeResultWriter | None = None,
    ) -> None:
        self._accounts = accounts
        self._providers = providers
        self._mark_read = mark_read
        self._large_results = large_results

    async def _prepare_result(
        self,
        response: EmailContentBatchResponse,
        serialized: bytes,
    ) -> EmailContentBatchResponse:
        try:
            validate_serialized_result(serialized)
        except ValueError:
            pass
        else:
            return response
        if self._large_results is None:
            raise ReadProviderError("limit_exceeded: serialized content result is too large")
        reference = await self._large_results.write(prefix="email-content", content=serialized)
        preview = EmailContentBatchResponse(
            emails=[],
            requested_count=response.requested_count,
            retrieved_count=response.retrieved_count,
            failed_ids=response.failed_ids,
            content_omitted=True,
            output_file_path=reference.output_file_path,
            output_media_type=reference.output_media_type,
            output_bytes=reference.output_bytes,
            output_sha256=reference.output_sha256,
            output_lifetime=reference.output_lifetime,
        )
        try:
            validate_serialized_result(preview.model_dump_json(exclude_none=True).encode())
        except ValueError:
            raise ReadProviderError("limit_exceeded: spill reference is too large") from None
        return preview

    async def execute(self, query: GetEmailContentQuery) -> EmailContentBatchResponse:
        query.validate()
        account = self._accounts.resolve(query.account_name)
        access = self._providers.open(account.account_name, expected_mode=account.mode)
        response = await _bounded_provider_call(access.provider.get_content(query, access.account))
        serialized = _validate_content_result(response)
        prepared = await self._prepare_result(response, serialized)
        if not query.mark_as_read or not response.emails:
            return prepared
        mark_ids = tuple(dict.fromkeys(item.email_id for item in response.emails))
        try:
            for offset in range(0, len(mark_ids), APPLICATION_LIMITS.mutation_uids):
                outcome = await self._mark_read.execute(
                    MarkReadCommand(
                        account_name=query.account_name,
                        email_ids=mark_ids[offset : offset + APPLICATION_LIMITS.mutation_uids],
                        mailbox=query.mailbox,
                    )
                )
                failed = sum(item.status != "succeeded" for item in outcome.outcomes)
                if failed or outcome.reconciliation_needed:
                    logger.warning(
                        "Content retrieval mark-as-read was incomplete: "
                        f"failed_or_unknown={failed}, reconciliation_needed={outcome.reconciliation_needed}"
                    )
                if outcome.reconciliation_needed or any(item.status == "unknown" for item in outcome.outcomes):
                    break
        except Exception as exc:
            logger.warning(f"Content retrieval mark-as-read failed: {type(exc).__name__}")
        return prepared


class AttachmentDownloadService:
    def __init__(
        self,
        accounts: ReadAccountAuthority,
        providers: ReadProviderFactory,
        artifacts: ArtifactWriter,
    ) -> None:
        self._accounts = accounts
        self._providers = providers
        self._artifacts = artifacts

    async def execute(self, command: DownloadAttachmentCommand) -> AttachmentDownloadResponse:
        command.validate()
        account = self._accounts.resolve(command.account_name)
        if not account.enable_attachment_download:
            raise PermissionError(
                "Attachment download is disabled. Set 'enable_attachment_download=true' in settings to enable this feature."
            )
        access = self._providers.open(account.account_name, expected_mode=account.mode)
        if not access.account.enable_attachment_download:
            raise PermissionError(
                "Attachment download is disabled. Set 'enable_attachment_download=true' in settings to enable this feature."
            )
        payload = await _bounded_provider_call(access.provider.fetch_attachment(command, access.account))
        if len(payload.content) > APPLICATION_LIMITS.attachment_bytes:
            raise ValueError(f"attachment exceeds {APPLICATION_LIMITS.attachment_bytes} bytes")
        current = self._accounts.resolve(command.account_name, expected_mode=access.account.mode)
        if not current.enable_attachment_download:
            raise PermissionError(
                "Attachment download is disabled. Set 'enable_attachment_download=true' in settings to enable this feature."
            )
        saved_path = await self._artifacts.write(command.save_path, payload)
        response = AttachmentDownloadResponse(
            email_id=payload.email_id,
            attachment_name=payload.attachment_name,
            mime_type=payload.mime_type,
            size=len(payload.content),
            saved_path=saved_path,
        )
        try:
            validate_serialized_result(response.model_dump_json().encode("utf-8"))
        except ValueError:
            raise ReadProviderError("limit_exceeded: serialized attachment result is too large") from None
        return response


@dataclass(frozen=True)
class ReadServices:
    mailboxes: MailboxDiscoveryService
    content: EmailContentService
    attachments: AttachmentDownloadService

    @classmethod
    def compose(
        cls,
        accounts: ReadAccountAuthority,
        providers: ReadProviderFactory,
        mark_read: MarkReadExecutor,
        artifacts: ArtifactWriter,
        large_results: LargeResultWriter | None = None,
    ) -> ReadServices:
        return cls(
            mailboxes=MailboxDiscoveryService(accounts, providers),
            content=EmailContentService(accounts, providers, mark_read, large_results),
            attachments=AttachmentDownloadService(accounts, providers, artifacts),
        )
