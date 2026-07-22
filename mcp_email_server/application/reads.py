from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mcp_email_server.application.metadata import RuntimeMode
from mcp_email_server.application.mutations import (
    APPLICATION_LIMITS,
    BatchMutationOutcome,
    MarkReadCommand,
)
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailContentBatchResponse,
    MailboxInfo,
)
from mcp_email_server.log import logger

MAX_CONTENT_EMAIL_IDS = 500


class ReadProviderError(RuntimeError):
    """A read provider failed without exposing transport-controlled detail."""


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
        if not self.pattern or len(self.pattern.encode()) > APPLICATION_LIMITS.mailbox_bytes:
            raise ValueError("mailbox pattern must be non-empty and within the size limit")
        if len(self.reference.encode()) > APPLICATION_LIMITS.mailbox_bytes:
            raise ValueError("mailbox reference exceeds the size limit")


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
        if not self.attachment_name or len(self.attachment_name.encode()) > APPLICATION_LIMITS.attachment_path_bytes:
            raise ValueError("attachment_name must be non-empty and within the size limit")
        if not self.save_path or len(self.save_path.encode()) > APPLICATION_LIMITS.attachment_path_bytes:
            raise ValueError("save_path must be non-empty and within the size limit")


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


class MarkReadExecutor(Protocol):
    async def execute(self, command: MarkReadCommand) -> BatchMutationOutcome: ...


def _validate_account_name(value: str) -> None:
    if not value or len(value.encode()) > APPLICATION_LIMITS.account_name_bytes:
        raise ValueError("account_name must be non-empty and within the size limit")


def _validate_mailbox(value: str) -> None:
    if not value or len(value.encode()) > APPLICATION_LIMITS.mailbox_bytes:
        raise ValueError("mailbox must be non-empty and within the size limit")


def _validate_email_ids(values: tuple[str, ...]) -> None:
    if not values or len(values) > MAX_CONTENT_EMAIL_IDS:
        raise ValueError(f"email_ids must contain between 1 and {MAX_CONTENT_EMAIL_IDS} values")
    for value in values:
        if not value.isdigit() or value.startswith("0") or int(value) > APPLICATION_LIMITS.maximum_imap_uid:
            raise ValueError("email_ids must contain canonical positive decimal IMAP UIDs")


class MailboxDiscoveryService:
    def __init__(self, accounts: ReadAccountAuthority, providers: ReadProviderFactory) -> None:
        self._accounts = accounts
        self._providers = providers

    async def execute(self, query: ListMailboxesQuery) -> list[MailboxInfo]:
        query.validate()
        account = self._accounts.resolve(query.account_name)
        access = self._providers.open(account.account_name, expected_mode=account.mode)
        return await access.provider.list_mailboxes(query)


class EmailContentService:
    def __init__(
        self,
        accounts: ReadAccountAuthority,
        providers: ReadProviderFactory,
        mark_read: MarkReadExecutor,
    ) -> None:
        self._accounts = accounts
        self._providers = providers
        self._mark_read = mark_read

    async def execute(self, query: GetEmailContentQuery) -> EmailContentBatchResponse:
        query.validate()
        account = self._accounts.resolve(query.account_name)
        access = self._providers.open(account.account_name, expected_mode=account.mode)
        response = await access.provider.get_content(query, access.account)
        if not query.mark_as_read or not response.emails:
            return response
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
        return response


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
        payload = await access.provider.fetch_attachment(command, access.account)
        if len(payload.content) > APPLICATION_LIMITS.attachment_bytes:
            raise ValueError(f"attachment exceeds {APPLICATION_LIMITS.attachment_bytes} bytes")
        current = self._accounts.resolve(command.account_name, expected_mode=access.account.mode)
        if not current.enable_attachment_download:
            raise PermissionError(
                "Attachment download is disabled. Set 'enable_attachment_download=true' in settings to enable this feature."
            )
        saved_path = await self._artifacts.write(command.save_path, payload)
        return AttachmentDownloadResponse(
            email_id=payload.email_id,
            attachment_name=payload.attachment_name,
            mime_type=payload.mime_type,
            size=len(payload.content),
            saved_path=saved_path,
        )


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
    ) -> ReadServices:
        return cls(
            mailboxes=MailboxDiscoveryService(accounts, providers),
            content=EmailContentService(accounts, providers, mark_read),
            attachments=AttachmentDownloadService(accounts, providers, artifacts),
        )
