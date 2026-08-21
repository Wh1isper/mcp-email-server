from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from mcp_email_server.application.limits import (
    APPLICATION_LIMITS,
    validate_controlled_string,
    validate_imap_uid,
    validate_optional_controlled_string,
    validate_serialized_result,
)
from mcp_email_server.application.metadata import RuntimeMode

MutationStatus = Literal["succeeded", "failed", "unknown"]
MutationProviderPurpose = Literal["incoming", "outgoing", "sent-copy"]
SentCopyStatus = Literal["skipped", "succeeded", "failed", "unknown"]
FlagOperation = Literal["add", "remove"]
MutableEmailFlag = Literal[r"\Seen", r"\Flagged", r"\Answered", r"\Draft"]
MUTABLE_EMAIL_FLAGS: frozenset[str] = frozenset({r"\Seen", r"\Flagged", r"\Answered", r"\Draft"})


ProviderResultT = TypeVar("ProviderResultT")


class MutationProviderError(RuntimeError):
    """A mutation provider failed before returning authoritative effect evidence."""


class MutationProjectionError(RuntimeError):
    """The rebuildable metadata projection could not be invalidated."""


class RecipientPolicyDeniedError(PermissionError):
    """Current account authority denies one or more message recipients."""


async def _bounded_provider_effect(operation: Awaitable[ProviderResultT]) -> ProviderResultT:
    async with asyncio.timeout(APPLICATION_LIMITS.provider_timeout_seconds):
        return await operation


@dataclass(frozen=True)
class MutationAccountSnapshot:
    account_name: str
    mode: RuntimeMode
    allowed_senders: tuple[str, ...]
    allowed_recipients: tuple[str, ...]
    report_blocked_mutations: bool
    # Non-secret capability evidence: whether the account has an outgoing binding.
    # Lets submission workflows refuse before any provider I/O without resolving
    # the outgoing secret; opening the outgoing provider remains the enforcement.
    # Required, not defaulted: an authority that forgets to state the capability
    # must fail loudly instead of silently opting into send.
    can_send: bool


@dataclass(frozen=True)
class TargetMutationOutcome:
    target: str
    status: MutationStatus
    detail: str | None = None


@dataclass(frozen=True)
class BatchMutationOutcome:
    outcomes: tuple[TargetMutationOutcome, ...]
    reconciliation_needed: bool = False

    def __post_init__(self) -> None:
        if not self.reconciliation_needed and any(item.status == "unknown" for item in self.outcomes):
            object.__setattr__(self, "reconciliation_needed", True)

    def targets(self, status: MutationStatus) -> list[str]:
        return [outcome.target for outcome in self.outcomes if outcome.status == status]

    @property
    def effect_may_have_started(self) -> bool:
        return any(outcome.status in ("succeeded", "unknown") for outcome in self.outcomes)


def _timeout_batch(targets: tuple[str, ...]) -> BatchMutationOutcome:
    return BatchMutationOutcome(
        tuple(TargetMutationOutcome(target, "unknown", "provider-timeout") for target in targets),
        reconciliation_needed=True,
    )


@dataclass(frozen=True)
class AppendMutationOutcome:
    status: MutationStatus
    message_id: str
    uid: str | None = None
    mailbox: str | None = None
    detail: str | None = None
    reconciliation_needed: bool = False

    def __post_init__(self) -> None:
        if self.status == "unknown" and not self.reconciliation_needed:
            object.__setattr__(self, "reconciliation_needed", True)


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

    def __post_init__(self) -> None:
        ambiguous = any(item.status == "unknown" for item in self.delivery) or self.sent_copy.status == "unknown"
        if ambiguous and not self.reconciliation_needed:
            object.__setattr__(self, "reconciliation_needed", True)

    def recipients(self, status: MutationStatus) -> list[str]:
        return [outcome.target for outcome in self.delivery if outcome.status == status]


@dataclass(frozen=True)
class SetEmailFlagsCommand:
    account_name: str
    email_ids: tuple[str, ...]
    operation: FlagOperation
    flags: tuple[MutableEmailFlag, ...]
    mailbox: str = "INBOX"

    def validate(self) -> None:
        _validate_account_name(self.account_name)
        _validate_email_ids(self.email_ids)
        validate_mailbox_name(self.mailbox)
        if self.operation not in ("add", "remove"):
            raise ValueError("operation must be 'add' or 'remove'")
        if not self.flags:
            raise ValueError("flags must not be empty")
        if len(self.flags) > len(MUTABLE_EMAIL_FLAGS):
            raise ValueError(f"flags must contain at most {len(MUTABLE_EMAIL_FLAGS)} values")
        if any(not isinstance(flag, str) for flag in self.flags):
            raise ValueError("flags must contain strings")
        if len(set(self.flags)) != len(self.flags):
            raise ValueError("flags must not contain duplicates")
        if any(flag not in MUTABLE_EMAIL_FLAGS for flag in self.flags):
            raise ValueError("flags contain an unsupported mutable email flag")


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
            for flag in self.flags:
                validate_controlled_string(
                    flag,
                    field_name="flag",
                    maximum_bytes=APPLICATION_LIMITS.flag_bytes,
                    allow_empty=True,
                )


@dataclass(frozen=True)
class SendCommand(ComposeCommand):
    reply_to: str | None = None

    def validate(self) -> None:
        super().validate()
        _validate_optional_header("reply_to", self.reply_to)


@dataclass(frozen=True)
class ForwardSourcePart:
    """One retained body part of a forwarded message.

    ``raw_part`` is opaque to the application layer: MIME construction stays in
    the provider adapter, while the application only bounds declared sizes.
    """

    byte_size: int
    raw_part: object


@dataclass(frozen=True)
class ForwardSource:
    """Provider evidence about the message a forward is derived from.

    ``body_text`` is the provider-composed forwarded block (its own quoting
    header plus the original text); the application only prefixes the caller's
    note and never formats the block itself. Only the fields the application
    actually bounds or derives from are carried — no write-only evidence.
    """

    subject: str
    body_text: str
    parts: tuple[ForwardSourcePart, ...]


@dataclass(frozen=True)
class ForwardCommand(ComposeCommand):
    source_email_id: str = ""
    source_mailbox: str = "INBOX"
    include_attachments: bool = True

    def validate(self) -> None:
        super().validate()
        validate_imap_uid(self.source_email_id, field_name="email_id")
        validate_mailbox_name(self.source_mailbox)
        # Lock the compose fields the forward workflow derives or does not
        # support, so a caller-supplied value is rejected instead of silently
        # overwritten (subject), mislabeling the plain-text block (html), or
        # sharing the attachment budget with forwarded parts (attachments).
        if self.subject:
            raise ValueError("forward subject is derived from the source message and must be empty")
        if self.html:
            raise ValueError("forwarded content is composed as plain text; html is not supported")
        if self.attachments:
            raise ValueError("forward does not accept caller attachments; the source's parts are re-attached")


class MutationAccountAuthority(Protocol):
    def resolve(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode | None = None,
    ) -> MutationAccountSnapshot: ...


class MutationProvider(Protocol):
    async def set_flags(
        self,
        command: SetEmailFlagsCommand,
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

    async def fetch_forward_source(
        self,
        command: ForwardCommand,
        account: MutationAccountSnapshot,
    ) -> ForwardSource: ...

    async def forward(
        self,
        command: ForwardCommand,
        source: ForwardSource,
        account: MutationAccountSnapshot,
    ) -> DeliveryMutationOutcome: ...


@dataclass(frozen=True)
class MutationProviderAccess:
    account: MutationAccountSnapshot
    provider: MutationProvider


class MutationProviderFactory(Protocol):
    def open(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode,
        purpose: MutationProviderPurpose,
    ) -> MutationProviderAccess: ...


class MutationProjection(Protocol):
    async def invalidate(self, mailboxes: tuple[str, ...]) -> None: ...


class MutationProjectionFactory(Protocol):
    async def open(self, account: MutationAccountSnapshot) -> MutationProjection: ...


def _validate_account_name(account_name: str) -> None:
    validate_controlled_string(
        account_name,
        field_name="account_name",
        maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
    )


def validate_mailbox_name(mailbox: str) -> None:
    validate_controlled_string(
        mailbox,
        field_name="mailbox",
        maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
    )


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
        validate_imap_uid(email_id, field_name="email_ids item")


def _validate_recipients(recipients: tuple[str, ...]) -> None:
    from email.utils import getaddresses

    if not recipients:
        raise ValueError("at least one recipient is required")
    if len(recipients) > APPLICATION_LIMITS.recipients:
        raise ValueError(f"recipient batch must contain at most {APPLICATION_LIMITS.recipients} values")
    if any(not isinstance(recipient, str) for recipient in recipients):
        raise ValueError("recipient values must be strings")
    for recipient in recipients:
        validate_controlled_string(
            recipient,
            field_name="recipient value",
            maximum_bytes=APPLICATION_LIMITS.address_bytes,
        )
    if any(len([address for _, address in getaddresses([recipient]) if address]) != 1 for recipient in recipients):
        raise ValueError("each recipient value must contain exactly one email address")


def _validate_content(subject: str, body: str, attachments: tuple[str, ...]) -> None:
    if not isinstance(body, str):
        raise ValueError("body must be a string")  # noqa: TRY004 - stable validation contract
    validate_controlled_string(
        subject,
        field_name="subject",
        maximum_bytes=APPLICATION_LIMITS.subject_bytes,
        allow_empty=True,
    )
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
        validate_controlled_string(
            raw_path,
            field_name="attachment path",
            maximum_bytes=APPLICATION_LIMITS.attachment_path_bytes,
        )
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


def _forwarded_subject(subject: str) -> str:
    """Return the derived forward subject without stacking a second prefix."""

    return subject if subject.casefold().startswith("fwd:") else f"Fwd: {subject}"


def _forwarded_body(note: str, block: str) -> str:
    """Join the caller's note with the provider-composed forwarded block."""

    return f"{note}\n\n{block}" if note else block


def _validate_forward_source(source: ForwardSource) -> None:
    """Bound in-memory forwarded parts before any delivery.

    ``_validate_attachments`` bounds caller-supplied filesystem paths; forwarded
    parts never touch the filesystem, so their declared sizes are bounded here
    against the same limits. The derived subject and body are validated once,
    by ``ComposeCommand.validate`` on the derived command — not duplicated here.
    """

    if len(source.parts) > APPLICATION_LIMITS.attachments:
        raise ValueError(
            f"forwarded message must contain at most {APPLICATION_LIMITS.attachments} parts; "
            "retry with include_attachments=false to forward the text without them"
        )
    total_size = 0
    for part in source.parts:
        if not isinstance(part.byte_size, int) or isinstance(part.byte_size, bool) or part.byte_size < 0:
            raise ValueError("forwarded part size must be a non-negative integer")
        if part.byte_size > APPLICATION_LIMITS.attachment_bytes:
            raise ValueError(f"a forwarded part exceeds {APPLICATION_LIMITS.attachment_bytes} bytes")
        total_size += part.byte_size
        if total_size > APPLICATION_LIMITS.total_attachment_bytes:
            raise ValueError(f"forwarded parts exceed {APPLICATION_LIMITS.total_attachment_bytes} bytes in total")
    if not isinstance(source.body_text, str):
        raise ValueError("body must be a string")  # noqa: TRY004 - stable validation contract
    if not isinstance(source.subject, str):
        raise ValueError("subject must be a string")  # noqa: TRY004 - stable validation contract


def _validate_optional_header(name: str, value: str | None) -> None:
    validate_optional_controlled_string(
        value,
        field_name=name,
        maximum_bytes=APPLICATION_LIMITS.header_bytes,
    )


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


def _result_limit_error() -> MutationProviderError:
    return MutationProviderError("limit_exceeded: mutation provider result exceeds application limits")


def _validate_result_string(
    value: object,
    *,
    field_name: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> str:
    try:
        return validate_controlled_string(
            value,
            field_name=field_name,
            maximum_bytes=maximum_bytes,
            allow_empty=allow_empty,
        )
    except ValueError:
        raise _result_limit_error() from None


def _validate_result_detail(detail: str | None) -> None:
    try:
        validate_optional_controlled_string(
            detail,
            field_name="mutation detail",
            maximum_bytes=APPLICATION_LIMITS.error_detail_bytes,
        )
    except ValueError:
        raise _result_limit_error() from None


def _outcome_payload(outcome: TargetMutationOutcome) -> dict[str, object]:
    return {"target": outcome.target, "status": outcome.status, "detail": outcome.detail}


def _validate_target_outcomes(outcomes: tuple[TargetMutationOutcome, ...]) -> None:
    if len(outcomes) > APPLICATION_LIMITS.warning_items:
        raise _result_limit_error()
    for outcome in outcomes:
        _validate_result_string(
            outcome.target,
            field_name="mutation target",
            maximum_bytes=APPLICATION_LIMITS.address_bytes,
        )
        if outcome.status not in ("succeeded", "failed", "unknown"):
            raise _result_limit_error()
        _validate_result_detail(outcome.detail)


def _validate_result_payload(payload: object) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        validate_serialized_result(serialized)
    except ValueError:
        raise _result_limit_error() from None


def _validate_batch_result(outcome: BatchMutationOutcome) -> BatchMutationOutcome:
    _validate_target_outcomes(outcome.outcomes)
    _validate_result_payload({
        "outcomes": [_outcome_payload(item) for item in outcome.outcomes],
        "reconciliation_needed": outcome.reconciliation_needed,
    })
    return outcome


def _validate_append_result(outcome: AppendMutationOutcome) -> AppendMutationOutcome:
    if outcome.status not in ("succeeded", "failed", "unknown"):
        raise _result_limit_error()
    _validate_result_string(
        outcome.message_id,
        field_name="message_id",
        maximum_bytes=APPLICATION_LIMITS.header_bytes,
        allow_empty=True,
    )
    if outcome.uid is not None:
        try:
            _validate_email_ids((outcome.uid,))
        except ValueError:
            raise _result_limit_error() from None
    if outcome.mailbox is not None:
        _validate_result_string(
            outcome.mailbox,
            field_name="result mailbox",
            maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
        )
    _validate_result_detail(outcome.detail)
    _validate_result_payload({
        "status": outcome.status,
        "message_id": outcome.message_id,
        "uid": outcome.uid,
        "mailbox": outcome.mailbox,
        "detail": outcome.detail,
        "reconciliation_needed": outcome.reconciliation_needed,
    })
    return outcome


def _validate_delivery_result(outcome: DeliveryMutationOutcome) -> DeliveryMutationOutcome:
    _validate_target_outcomes(outcome.outcomes)
    _validate_result_payload({"outcomes": [_outcome_payload(item) for item in outcome.outcomes]})
    return outcome


def _validate_sent_copy_result(outcome: SentCopyMutationOutcome) -> SentCopyMutationOutcome:
    if outcome.status not in ("skipped", "succeeded", "failed", "unknown"):
        raise _result_limit_error()
    if outcome.mailbox is not None:
        _validate_result_string(
            outcome.mailbox,
            field_name="sent-copy mailbox",
            maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
        )
    _validate_result_detail(outcome.detail)
    return outcome


def _validate_send_result(outcome: SendMutationOutcome) -> SendMutationOutcome:
    _validate_target_outcomes(outcome.delivery)
    _validate_sent_copy_result(outcome.sent_copy)
    _validate_result_payload({
        "delivery": [_outcome_payload(item) for item in outcome.delivery],
        "sent_copy": {
            "status": outcome.sent_copy.status,
            "mailbox": outcome.sent_copy.mailbox,
            "detail": outcome.sent_copy.detail,
        },
        "reconciliation_needed": outcome.reconciliation_needed,
    })
    return outcome


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

    def _open(
        self,
        account: MutationAccountSnapshot,
        *,
        purpose: MutationProviderPurpose = "incoming",
    ) -> MutationProviderAccess:
        return self._providers.open(account.account_name, expected_mode=account.mode, purpose=purpose)

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

    async def _complete_send(
        self,
        account: MutationAccountSnapshot,
        delivery: DeliveryMutationOutcome,
        bcc: tuple[str, ...],
    ) -> SendMutationOutcome:
        """Attach the independent Sent copy to already-authoritative delivery.

        Every submission workflow shares this tail so the ambiguity and
        ``reconciliation_needed`` semantics have exactly one owner.
        """

        if not delivery.has_accepted_recipient or delivery.sent_message is None:
            return _validate_send_result(SendMutationOutcome(delivery.outcomes, SentCopyMutationOutcome("skipped")))

        # Saving the copy is a separate provider effect and therefore gets a fresh
        # lifecycle/credential resolution. SMTP delivery is never rewritten.
        try:
            sent_access = self._open(account, purpose="sent-copy")
        except Exception:
            # SMTP delivery is already authoritative. Lifecycle or credential
            # failure before opening the independent copy cannot erase it.
            return _validate_send_result(
                SendMutationOutcome(
                    delivery.outcomes,
                    SentCopyMutationOutcome("failed", detail="sent-copy-unavailable"),
                )
            )
        try:
            sent_copy = await _bounded_provider_effect(sent_access.provider.save_sent_copy(delivery.sent_message, bcc))
        except MutationProviderError:
            # Typed APPEND-boundary cancellation is returned by the provider;
            # escaped setup/cancellation happened before APPEND started.
            return _validate_send_result(
                SendMutationOutcome(
                    delivery.outcomes,
                    SentCopyMutationOutcome("failed", detail="sent-copy-unavailable"),
                )
            )
        except TimeoutError:
            return _validate_send_result(
                SendMutationOutcome(
                    delivery.outcomes,
                    SentCopyMutationOutcome("unknown", detail="provider-timeout"),
                    reconciliation_needed=True,
                )
            )
        except asyncio.CancelledError:
            # Provider adapters use typed unknown outcomes once APPEND may have
            # started; an escaped cancellation is therefore pre-effect setup.
            return _validate_send_result(
                SendMutationOutcome(
                    delivery.outcomes,
                    SentCopyMutationOutcome("failed", detail="sent-copy-unavailable"),
                )
            )
        except Exception:
            # Treat an untyped provider escape conservatively rather than claim
            # that an APPEND definitely did not happen.
            return _validate_send_result(
                SendMutationOutcome(
                    delivery.outcomes,
                    SentCopyMutationOutcome("unknown", detail="sent-copy"),
                )
            )
        sent_copy = _validate_sent_copy_result(sent_copy)
        reconciliation_needed = False
        if sent_copy.status in ("succeeded", "unknown") and sent_copy.mailbox is not None:
            invalidated = await self._invalidate(sent_access.account, (sent_copy.mailbox,))
            reconciliation_needed = not invalidated
        return _validate_send_result(SendMutationOutcome(delivery.outcomes, sent_copy, reconciliation_needed))

    @staticmethod
    def _require_send_capability(account: MutationAccountSnapshot) -> None:
        if not account.can_send:
            raise MutationProviderError("capability_unavailable: SMTP is not configured for this account")

    @staticmethod
    def _delivery_timeout(command: ComposeCommand) -> SendMutationOutcome:
        recipients = (*command.recipients, *command.cc, *command.bcc)
        return _validate_send_result(
            SendMutationOutcome(
                tuple(TargetMutationOutcome(recipient, "unknown", "provider-timeout") for recipient in recipients),
                SentCopyMutationOutcome("skipped"),
                reconciliation_needed=True,
            )
        )


class SetEmailFlagsService(_MutationWorkflow):
    async def execute(self, command: SetEmailFlagsCommand) -> BatchMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        access = self._open(account)
        try:
            outcome = _validate_batch_result(
                await _bounded_provider_effect(access.provider.set_flags(command, access.account))
            )
        except TimeoutError:
            outcome = _validate_batch_result(_timeout_batch(command.email_ids))
        if not outcome.effect_may_have_started:
            return outcome
        invalidated = await self._invalidate(access.account, (command.mailbox,))
        return _validate_batch_result(
            BatchMutationOutcome(
                outcome.outcomes, reconciliation_needed=outcome.reconciliation_needed or not invalidated
            )
        )


class MarkReadService:
    """Focused mark-read executor backed by the generic mutable-flag workflow."""

    def __init__(self, set_flags: SetEmailFlagsService) -> None:
        self._set_flags = set_flags

    async def execute(self, command: MarkReadCommand) -> BatchMutationOutcome:
        return await self._set_flags.execute(
            SetEmailFlagsCommand(
                account_name=command.account_name,
                email_ids=command.email_ids,
                operation="add",
                flags=(r"\Seen",),
                mailbox=command.mailbox,
            )
        )


class SaveToMailboxService(_MutationWorkflow):
    async def execute(self, command: SaveToMailboxCommand) -> AppendMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        _validate_recipient_policy(command, account)
        access = self._open(account)
        _validate_recipient_policy(command, access.account)
        try:
            outcome = _validate_append_result(
                await _bounded_provider_effect(access.provider.save_to_mailbox(command, access.account))
            )
        except TimeoutError:
            outcome = _validate_append_result(
                AppendMutationOutcome(
                    status="unknown",
                    message_id="",
                    mailbox=command.mailbox,
                    detail="provider-timeout",
                    reconciliation_needed=True,
                )
            )
        if outcome.status not in ("succeeded", "unknown"):
            return outcome
        invalidated = await self._invalidate(access.account, (command.mailbox,))
        return _validate_append_result(
            AppendMutationOutcome(
                status=outcome.status,
                message_id=outcome.message_id,
                uid=outcome.uid,
                mailbox=outcome.mailbox,
                detail=outcome.detail,
                reconciliation_needed=outcome.reconciliation_needed or not invalidated,
            )
        )


class DeleteService(_MutationWorkflow):
    async def execute(self, command: DeleteCommand) -> BatchMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        access = self._open(account)
        try:
            outcome = _validate_batch_result(
                await _bounded_provider_effect(access.provider.delete(command, access.account))
            )
        except TimeoutError:
            outcome = _validate_batch_result(_timeout_batch(command.email_ids))
        if not outcome.effect_may_have_started:
            return outcome
        invalidated = await self._invalidate(access.account, (command.mailbox,))
        return _validate_batch_result(
            BatchMutationOutcome(
                outcome.outcomes, reconciliation_needed=outcome.reconciliation_needed or not invalidated
            )
        )


class MoveService(_MutationWorkflow):
    async def execute(self, command: MoveCommand) -> BatchMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        access = self._open(account)
        try:
            outcome = _validate_batch_result(
                await _bounded_provider_effect(access.provider.move(command, access.account))
            )
        except TimeoutError:
            outcome = _validate_batch_result(_timeout_batch(command.email_ids))
        if not outcome.effect_may_have_started:
            return outcome
        invalidated = await self._invalidate(access.account, (command.source_mailbox, command.destination_mailbox))
        return _validate_batch_result(
            BatchMutationOutcome(
                outcome.outcomes, reconciliation_needed=outcome.reconciliation_needed or not invalidated
            )
        )


@dataclass(frozen=True)
class ArchiveMutationOutcome:
    batch: BatchMutationOutcome
    archive_mailbox: str


class ArchiveService(_MutationWorkflow):
    async def execute(self, command: ArchiveCommand) -> ArchiveMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        discovery = self._open(account)
        try:
            archive_mailbox = await _bounded_provider_effect(
                discovery.provider.find_archive_mailbox(command.source_mailbox)
            )
        except TimeoutError:
            raise MutationProviderError("archive mailbox discovery timed out") from None
        move = MoveCommand(
            account_name=command.account_name,
            email_ids=command.email_ids,
            source_mailbox=command.source_mailbox,
            destination_mailbox=archive_mailbox,
        )
        move.validate()
        # Re-resolve selected-mode authority immediately before the move effect.
        access = self._providers.open(
            command.account_name,
            expected_mode=account.mode,
            purpose="incoming",
        )
        try:
            outcome = _validate_batch_result(await _bounded_provider_effect(access.provider.move(move, access.account)))
        except TimeoutError:
            outcome = _validate_batch_result(_timeout_batch(command.email_ids))
        if outcome.effect_may_have_started:
            invalidated = await self._invalidate(access.account, (command.source_mailbox, archive_mailbox))
            outcome = _validate_batch_result(
                BatchMutationOutcome(
                    outcome.outcomes, reconciliation_needed=outcome.reconciliation_needed or not invalidated
                )
            )
        result = ArchiveMutationOutcome(outcome, archive_mailbox)
        _validate_result_payload({
            "batch": {
                "outcomes": [_outcome_payload(item) for item in outcome.outcomes],
                "reconciliation_needed": outcome.reconciliation_needed,
            },
            "archive_mailbox": archive_mailbox,
        })
        return result


class SendService(_MutationWorkflow):
    async def execute(self, command: SendCommand) -> SendMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        # In compatibility mode the outgoing open does not itself enforce role
        # presence, so both submission workflows check the same non-secret flag.
        self._require_send_capability(account)
        _validate_recipient_policy(command, account)
        access = self._open(account, purpose="outgoing")
        self._require_send_capability(access.account)
        _validate_recipient_policy(command, access.account)
        try:
            delivery = _validate_delivery_result(
                await _bounded_provider_effect(access.provider.send(command, access.account))
            )
        except TimeoutError:
            return self._delivery_timeout(command)
        return await self._complete_send(account, delivery, command.bcc)


class ForwardService(_MutationWorkflow):
    async def execute(self, command: ForwardCommand) -> SendMutationOutcome:
        command.validate()
        account = self._resolve(command.account_name)
        # A forward is a submission: refuse a send-incapable account before the
        # source message is ever logged into, downloaded, or parsed. The flag is
        # non-secret authority evidence, so the outgoing secret stays unresolved
        # until the submission effect itself opens the outgoing provider.
        self._require_send_capability(account)
        _validate_recipient_policy(command, account)
        incoming = self._open(account, purpose="incoming")
        self._require_send_capability(incoming.account)
        _validate_recipient_policy(command, incoming.account)
        try:
            source = await _bounded_provider_effect(incoming.provider.fetch_forward_source(command, incoming.account))
        except TimeoutError:
            # Retrieval is a read, not an effect: a forward must never be
            # submitted without the content and parts it was meant to carry.
            raise MutationProviderError("forward source retrieval timed out") from None
        _validate_forward_source(source)
        forwarded = replace(
            command,
            subject=_forwarded_subject(source.subject),
            body=_forwarded_body(command.body, source.body_text),
        )
        # The derived command carries the derived subject, so it revalidates
        # through the shared compose contract; ForwardCommand.validate's input
        # locks (empty subject) applied to the caller's own input above.
        ComposeCommand.validate(forwarded)
        # Re-resolve authority immediately before the outgoing submission effect.
        access = self._open(account, purpose="outgoing")
        self._require_send_capability(access.account)
        _validate_recipient_policy(forwarded, access.account)
        try:
            delivery = _validate_delivery_result(
                await _bounded_provider_effect(access.provider.forward(forwarded, source, access.account))
            )
        except TimeoutError:
            return self._delivery_timeout(forwarded)
        return await self._complete_send(account, delivery, forwarded.bcc)


@dataclass(frozen=True)
class MutationServices:
    set_flags: SetEmailFlagsService
    mark_read: MarkReadService
    save_to_mailbox: SaveToMailboxService
    delete: DeleteService
    move: MoveService
    archive: ArchiveService
    send: SendService
    forward: ForwardService

    @classmethod
    def compose(
        cls,
        accounts: MutationAccountAuthority,
        providers: MutationProviderFactory,
        projections: MutationProjectionFactory,
    ) -> MutationServices:
        arguments = (accounts, providers, projections)
        set_flags = SetEmailFlagsService(*arguments)
        return cls(
            set_flags=set_flags,
            mark_read=MarkReadService(set_flags),
            save_to_mailbox=SaveToMailboxService(*arguments),
            delete=DeleteService(*arguments),
            move=MoveService(*arguments),
            archive=ArchiveService(*arguments),
            send=SendService(*arguments),
            forward=ForwardService(*arguments),
        )
