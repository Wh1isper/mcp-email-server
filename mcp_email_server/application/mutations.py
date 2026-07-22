from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from mcp_email_server.application.metadata import RuntimeMode

MutationStatus = Literal["succeeded", "failed", "unknown"]
SentCopyStatus = Literal["skipped", "succeeded", "failed", "unknown"]


@dataclass(frozen=True)
class ApplicationLimits:
    mutation_uids: int = 100
    account_name_bytes: int = 256
    recipients: int = 100
    address_bytes: int = 1_024
    mailbox_bytes: int = 1_024
    subject_bytes: int = 64 * 1_024
    body_bytes: int = 1 * 1_024 * 1_024
    header_bytes: int = 64 * 1_024
    attachments: int = 20
    attachment_path_bytes: int = 4_096
    flags: int = 100
    flag_bytes: int = 128
    attachment_bytes: int = 25 * 1_024 * 1_024
    total_attachment_bytes: int = 50 * 1_024 * 1_024
    maximum_imap_uid: int = 2**32 - 1


APPLICATION_LIMITS = ApplicationLimits()
_CANONICAL_UID = re.compile(r"[1-9][0-9]*\Z")


class MutationProviderError(RuntimeError):
    """A mutation provider failed before returning authoritative effect evidence."""


class MutationProjectionError(RuntimeError):
    """The rebuildable metadata projection could not be invalidated."""


class RecipientPolicyDeniedError(PermissionError):
    """Current account authority denies one or more message recipients."""


@dataclass(frozen=True)
class MutationAccountSnapshot:
    account_name: str
    mode: RuntimeMode
    allowed_senders: tuple[str, ...]
    allowed_recipients: tuple[str, ...]
    report_blocked_mutations: bool


@dataclass(frozen=True)
class TargetMutationOutcome:
    target: str
    status: MutationStatus
    detail: str | None = None


@dataclass(frozen=True)
class BatchMutationOutcome:
    outcomes: tuple[TargetMutationOutcome, ...]
    reconciliation_needed: bool = False

    def targets(self, status: MutationStatus) -> list[str]:
        return [outcome.target for outcome in self.outcomes if outcome.status == status]

    @property
    def effect_may_have_started(self) -> bool:
        return any(outcome.status in ("succeeded", "unknown") for outcome in self.outcomes)


@dataclass(frozen=True)
class AppendMutationOutcome:
    status: MutationStatus
    message_id: str
    uid: str | None = None
    mailbox: str | None = None
    detail: str | None = None
    reconciliation_needed: bool = False


@dataclass(frozen=True)
class DeliveryMutationOutcome:
    outcomes: tuple[TargetMutationOutcome, ...]
    sent_message: object | None

    @property
    def has_accepted_recipient(self) -> bool:
        return any(outcome.status == "succeeded" for outcome in self.outcomes)


@dataclass(frozen=True)
class SentCopyMutationOutcome:
    status: SentCopyStatus
    mailbox: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SendMutationOutcome:
    delivery: tuple[TargetMutationOutcome, ...]
    sent_copy: SentCopyMutationOutcome
    reconciliation_needed: bool = False

    def recipients(self, status: MutationStatus) -> list[str]:
        return [outcome.target for outcome in self.delivery if outcome.status == status]


@dataclass(frozen=True)
class MarkReadCommand:
    account_name: str
    email_ids: tuple[str, ...]
    mailbox: str = "INBOX"

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        _validate_email_ids(self.email_ids)
        validate_mailbox_name(self.mailbox)


@dataclass(frozen=True)
class DeleteCommand:
    account_name: str
    email_ids: tuple[str, ...]
    mailbox: str = "INBOX"

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        _validate_email_ids(self.email_ids)
        validate_mailbox_name(self.mailbox)


@dataclass(frozen=True)
class MoveCommand:
    account_name: str
    email_ids: tuple[str, ...]
    source_mailbox: str
    destination_mailbox: str

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        _validate_email_ids(self.email_ids)
        validate_mailbox_name(self.source_mailbox)
        validate_mailbox_name(self.destination_mailbox)
        same_reserved_inbox = (
            self.source_mailbox.casefold() == "inbox" and self.destination_mailbox.casefold() == "inbox"
        )
        if self.source_mailbox == self.destination_mailbox or same_reserved_inbox:
            raise ValueError("source_mailbox and destination_mailbox must differ")


@dataclass(frozen=True)
class ArchiveCommand:
    account_name: str
    email_ids: tuple[str, ...]
    source_mailbox: str = "INBOX"

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        _validate_email_ids(self.email_ids)
        validate_mailbox_name(self.source_mailbox)


@dataclass(frozen=True)
class ComposeCommand:
    account_name: str
    recipients: tuple[str, ...]
    subject: str
    body: str
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    html: bool = False
    attachments: tuple[str, ...] = ()
    in_reply_to: str | None = None
    references: str | None = None

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        _validate_recipients((*self.recipients, *self.cc, *self.bcc))
        _validate_content(self.subject, self.body, self.attachments)
        _validate_optional_header("in_reply_to", self.in_reply_to)
        _validate_optional_header("references", self.references)


@dataclass(frozen=True)
class SaveToMailboxCommand(ComposeCommand):
    mailbox: str = "Drafts"
    flags: tuple[str, ...] | None = None

    def validate(self) -> None:
        super().validate()
        validate_mailbox_name(self.mailbox)
        if self.flags is not None:
            if any(not isinstance(flag, str) for flag in self.flags):
                raise ValueError("flags must contain strings")
            if len(self.flags) > APPLICATION_LIMITS.flags:
                raise ValueError(f"flags must contain at most {APPLICATION_LIMITS.flags} values")
            if any(len(flag.encode("utf-8")) > APPLICATION_LIMITS.flag_bytes for flag in self.flags):
                raise ValueError(f"a flag exceeds {APPLICATION_LIMITS.flag_bytes} bytes")


@dataclass(frozen=True)
class SendCommand(ComposeCommand):
    reply_to: str | None = None

    def validate(self) -> None:
        super().validate()
        _validate_optional_header("reply_to", self.reply_to)


class MutationAccountAuthority(Protocol):
    def resolve(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode | None = None,
    ) -> MutationAccountSnapshot: ...


class MutationProvider(Protocol):
    async def mark_read(
        self,
        command: MarkReadCommand,
        account: MutationAccountSnapshot,
    ) -> BatchMutationOutcome: ...

    async def save_to_mailbox(
        self,
        command: SaveToMailboxCommand,
        account: MutationAccountSnapshot,
    ) -> AppendMutationOutcome: ...

    async def delete(
        self,
        command: DeleteCommand,
        account: MutationAccountSnapshot,
    ) -> BatchMutationOutcome: ...

    async def move(
        self,
        command: MoveCommand,
        account: MutationAccountSnapshot,
    ) -> BatchMutationOutcome: ...

    async def find_archive_mailbox(self, source_mailbox: str) -> str: ...

    async def send(
        self,
        command: SendCommand,
        account: MutationAccountSnapshot,
    ) -> DeliveryMutationOutcome: ...

    async def save_sent_copy(
        self,
        sent_message: object,
        bcc: tuple[str, ...],
    ) -> SentCopyMutationOutcome: ...


@dataclass(frozen=True)
class MutationProviderAccess:
    account: MutationAccountSnapshot
    provider: MutationProvider


class MutationProviderFactory(Protocol):
    def open(self, account_name: str, *, expected_mode: RuntimeMode) -> MutationProviderAccess: ...


class MutationProjection(Protocol):
    async def invalidate(self, mailboxes: tuple[str, ...]) -> None: ...


class MutationProjectionFactory(Protocol):
    async def open(self, account: MutationAccountSnapshot) -> MutationProjection: ...


def _validate_account_name(account_name: str) -> None:
    if not isinstance(account_name, str):
        raise ValueError("account_name must be a string")  # noqa: TRY004 - stable validation contract
    if not account_name.strip():
        raise ValueError("account_name must not be empty")
    if _contains_control_character(account_name):
        raise ValueError("account_name must not contain control characters")
    if len(account_name.encode("utf-8")) > APPLICATION_LIMITS.account_name_bytes:
        raise ValueError(f"account_name exceeds {APPLICATION_LIMITS.account_name_bytes} bytes")


def validate_mailbox_name(mailbox: str) -> None:
    if not isinstance(mailbox, str):
        raise ValueError("mailbox must be a string")  # noqa: TRY004 - stable validation contract
    if not mailbox.strip():
        raise ValueError("mailbox must not be empty")
    if _contains_control_character(mailbox):
        raise ValueError("mailbox must not contain control characters")
    if len(mailbox.encode("utf-8")) > APPLICATION_LIMITS.mailbox_bytes:
        raise ValueError(f"mailbox exceeds {APPLICATION_LIMITS.mailbox_bytes} bytes")


def _validate_email_ids(email_ids: tuple[str, ...]) -> None:
    if not email_ids:
        raise ValueError("email_ids must not be empty")
    if len(email_ids) > APPLICATION_LIMITS.mutation_uids:
        raise ValueError(f"email_ids must contain at most {APPLICATION_LIMITS.mutation_uids} values")
    if any(not isinstance(email_id, str) for email_id in email_ids):
        raise ValueError("email_ids must contain strings")
    if len(set(email_ids)) != len(email_ids):
        raise ValueError("email_ids must not contain duplicates")
    for email_id in email_ids:
        if _CANONICAL_UID.fullmatch(email_id) is None:
            raise ValueError("email_ids must contain canonical positive decimal IMAP UIDs")
        if int(email_id) > APPLICATION_LIMITS.maximum_imap_uid:
            raise ValueError("email_ids contain an out-of-range IMAP UID")


def _validate_recipients(recipients: tuple[str, ...]) -> None:
    from email.utils import getaddresses

    if not recipients:
        raise ValueError("at least one recipient is required")
    if len(recipients) > APPLICATION_LIMITS.recipients:
        raise ValueError(f"recipient batch must contain at most {APPLICATION_LIMITS.recipients} values")
    if any(not isinstance(recipient, str) for recipient in recipients):
        raise ValueError("recipient values must be strings")
    if any(not recipient.strip() for recipient in recipients):
        raise ValueError("recipient values must not be empty")
    if any(_contains_control_character(recipient) for recipient in recipients):
        raise ValueError("recipient values must not contain control characters")
    if any(len(recipient.encode("utf-8")) > APPLICATION_LIMITS.address_bytes for recipient in recipients):
        raise ValueError(f"a recipient value exceeds {APPLICATION_LIMITS.address_bytes} bytes")
    if any(len([address for _, address in getaddresses([recipient]) if address]) != 1 for recipient in recipients):
        raise ValueError("each recipient value must contain exactly one email address")


def _validate_content(subject: str, body: str, attachments: tuple[str, ...]) -> None:
    if not isinstance(subject, str) or not isinstance(body, str):
        raise ValueError("subject and body must be strings")  # noqa: TRY004 - stable validation contract
    if _contains_control_character(subject):
        raise ValueError("subject must not contain control characters")
    if len(subject.encode("utf-8")) > APPLICATION_LIMITS.subject_bytes:
        raise ValueError(f"subject exceeds {APPLICATION_LIMITS.subject_bytes} bytes")
    if len(body.encode("utf-8")) > APPLICATION_LIMITS.body_bytes:
        raise ValueError(f"body exceeds {APPLICATION_LIMITS.body_bytes} bytes")
    _validate_attachments(attachments)


def _validate_attachments(attachments: tuple[str, ...]) -> None:
    if len(attachments) > APPLICATION_LIMITS.attachments:
        raise ValueError(f"attachments must contain at most {APPLICATION_LIMITS.attachments} paths")
    if any(not isinstance(raw_path, str) for raw_path in attachments):
        raise ValueError("attachment paths must be strings")
    total_size = 0
    for raw_path in attachments:
        if len(raw_path.encode("utf-8")) > APPLICATION_LIMITS.attachment_path_bytes:
            raise ValueError(f"an attachment path exceeds {APPLICATION_LIMITS.attachment_path_bytes} bytes")
        path = Path(raw_path)
        try:
            metadata = path.stat()
        except OSError:
            # Preserve the classic composer as the owner of path-specific errors.
            continue
        if not path.is_file():
            continue
        size = metadata.st_size
        if size > APPLICATION_LIMITS.attachment_bytes:
            raise ValueError(f"an attachment exceeds {APPLICATION_LIMITS.attachment_bytes} bytes")
        total_size += size
        if total_size > APPLICATION_LIMITS.total_attachment_bytes:
            raise ValueError(f"attachments exceed {APPLICATION_LIMITS.total_attachment_bytes} bytes in total")


def _validate_optional_header(name: str, value: str | None) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if value is not None and _contains_control_character(value):
        raise ValueError(f"{name} must not contain control characters")
    if value is not None and len(value.encode("utf-8")) > APPLICATION_LIMITS.header_bytes:
        raise ValueError(f"{name} exceeds {APPLICATION_LIMITS.header_bytes} bytes")


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _recipient_policy_allows(recipient: str, allowed: tuple[str, ...]) -> bool:
    # Re-parse the validated single-address value so direct application callers
    # receive the same exact allowlist decision as the MCP compatibility gate.
    from email.utils import getaddresses

    from mcp_email_server.config import normalize_address

    if not allowed:
        return True
    addresses = [normalize_address(address) for _, address in getaddresses([recipient]) if address]
    allowed_set = set(allowed)
    return bool(addresses) and all(address in allowed_set for address in addresses)


def _validate_recipient_policy(command: ComposeCommand, account: MutationAccountSnapshot) -> None:
    blocked = [
        recipient
        for recipient in (*command.recipients, *command.cc, *command.bcc)
        if not _recipient_policy_allows(recipient, account.allowed_recipients)
    ]
    if blocked:
        raise RecipientPolicyDeniedError("recipient policy denied one or more addresses")


class _MutationWorkflow:
    def __init__(
        self,
        accounts: MutationAccountAuthority,
        providers: MutationProviderFactory,
        projections: MutationProjectionFactory,
    ) -> None:
        self._accounts = accounts
        self._providers = providers
        self._projections = projections

    def _resolve(self, account_name: str) -> MutationAccountSnapshot:
        return self._accounts.resolve(account_name)

    def _open(self, account: MutationAccountSnapshot) -> MutationProviderAccess:
        return self._providers.open(account.account_name, expected_mode=account.mode)

    async def _invalidate(
        self,
        account: MutationAccountSnapshot,
        mailboxes: tuple[str, ...],
    ) -> bool:
        try:
            projection = await self._projections.open(account)
            await projection.invalidate(tuple(dict.fromkeys(mailboxes)))
        except (asyncio.CancelledError, Exception):
            # Projection state is rebuildable and must never overwrite known or
            # ambiguous provider evidence with a misleading mutation failure.
            return False
        return True


class MarkReadService(_MutationWorkflow):
    async def execute(self, command: MarkReadCommand) -> BatchMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        access = self._open(account)
        outcome = await access.provider.mark_read(command, access.account)
        if not outcome.effect_may_have_started:
            return outcome
        invalidated = await self._invalidate(access.account, (command.mailbox,))
        return BatchMutationOutcome(outcome.outcomes, reconciliation_needed=not invalidated)


class SaveToMailboxService(_MutationWorkflow):
    async def execute(self, command: SaveToMailboxCommand) -> AppendMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        _validate_recipient_policy(command, account)
        access = self._open(account)
        _validate_recipient_policy(command, access.account)
        outcome = await access.provider.save_to_mailbox(command, access.account)
        if outcome.status not in ("succeeded", "unknown"):
            return outcome
        invalidated = await self._invalidate(access.account, (command.mailbox,))
        return AppendMutationOutcome(
            status=outcome.status,
            message_id=outcome.message_id,
            uid=outcome.uid,
            mailbox=outcome.mailbox,
            detail=outcome.detail,
            reconciliation_needed=not invalidated,
        )


class DeleteService(_MutationWorkflow):
    async def execute(self, command: DeleteCommand) -> BatchMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        access = self._open(account)
        outcome = await access.provider.delete(command, access.account)
        if not outcome.effect_may_have_started:
            return outcome
        invalidated = await self._invalidate(access.account, (command.mailbox,))
        return BatchMutationOutcome(outcome.outcomes, reconciliation_needed=not invalidated)


class MoveService(_MutationWorkflow):
    async def execute(self, command: MoveCommand) -> BatchMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        access = self._open(account)
        outcome = await access.provider.move(command, access.account)
        if not outcome.effect_may_have_started:
            return outcome
        invalidated = await self._invalidate(access.account, (command.source_mailbox, command.destination_mailbox))
        return BatchMutationOutcome(outcome.outcomes, reconciliation_needed=not invalidated)


@dataclass(frozen=True)
class ArchiveMutationOutcome:
    batch: BatchMutationOutcome
    archive_mailbox: str


class ArchiveService(_MutationWorkflow):
    async def execute(self, command: ArchiveCommand) -> ArchiveMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        discovery = self._open(account)
        archive_mailbox = await discovery.provider.find_archive_mailbox(command.source_mailbox)
        move = MoveCommand(
            account_name=command.account_name,
            email_ids=command.email_ids,
            source_mailbox=command.source_mailbox,
            destination_mailbox=archive_mailbox,
        )
        move.validate()
        # Re-resolve selected-mode authority immediately before the move effect.
        access = self._providers.open(command.account_name, expected_mode=account.mode)
        outcome = await access.provider.move(move, access.account)
        if outcome.effect_may_have_started:
            invalidated = await self._invalidate(access.account, (command.source_mailbox, archive_mailbox))
            outcome = BatchMutationOutcome(outcome.outcomes, reconciliation_needed=not invalidated)
        return ArchiveMutationOutcome(outcome, archive_mailbox)


class SendService(_MutationWorkflow):
    async def execute(self, command: SendCommand) -> SendMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        _validate_recipient_policy(command, account)
        access = self._open(account)
        _validate_recipient_policy(command, access.account)
        delivery = await access.provider.send(command, access.account)
        if not delivery.has_accepted_recipient or delivery.sent_message is None:
            return SendMutationOutcome(delivery.outcomes, SentCopyMutationOutcome("skipped"))

        # Saving the copy is a separate provider effect and therefore gets a fresh
        # lifecycle/credential resolution. SMTP delivery is never rewritten.
        try:
            sent_access = self._providers.open(command.account_name, expected_mode=account.mode)
        except Exception:
            # SMTP delivery is already authoritative. Lifecycle or credential
            # failure before opening the independent copy cannot erase it.
            return SendMutationOutcome(
                delivery.outcomes,
                SentCopyMutationOutcome("failed", detail="sent-copy-unavailable"),
            )
        try:
            sent_copy = await sent_access.provider.save_sent_copy(delivery.sent_message, command.bcc)
        except (MutationProviderError, asyncio.CancelledError):
            # Typed APPEND-boundary cancellation is returned by the provider;
            # escaped setup/cancellation happened before APPEND started.
            return SendMutationOutcome(
                delivery.outcomes,
                SentCopyMutationOutcome("failed", detail="sent-copy-unavailable"),
            )
        except Exception:
            # Treat an untyped provider escape conservatively rather than claim
            # that an APPEND definitely did not happen.
            return SendMutationOutcome(
                delivery.outcomes,
                SentCopyMutationOutcome("unknown", detail="sent-copy"),
            )
        reconciliation_needed = False
        if sent_copy.status in ("succeeded", "unknown") and sent_copy.mailbox is not None:
            invalidated = await self._invalidate(sent_access.account, (sent_copy.mailbox,))
            reconciliation_needed = not invalidated
        return SendMutationOutcome(delivery.outcomes, sent_copy, reconciliation_needed)


@dataclass(frozen=True)
class MutationServices:
    mark_read: MarkReadService
    save_to_mailbox: SaveToMailboxService
    delete: DeleteService
    move: MoveService
    archive: ArchiveService
    send: SendService

    @classmethod
    def compose(
        cls,
        accounts: MutationAccountAuthority,
        providers: MutationProviderFactory,
        projections: MutationProjectionFactory,
    ) -> MutationServices:
        arguments = (accounts, providers, projections)
        return cls(
            mark_read=MarkReadService(*arguments),
            save_to_mailbox=SaveToMailboxService(*arguments),
            delete=DeleteService(*arguments),
            move=MoveService(*arguments),
            archive=ArchiveService(*arguments),
            send=SendService(*arguments),
        )
