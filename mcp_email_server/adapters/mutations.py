from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TypeVar

from mcp_email_server.adapters.authority import resolve_local_account
from mcp_email_server.application.management import BindingRole
from mcp_email_server.application.metadata import RuntimeMode
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    BatchMutationOutcome,
    DeleteCommand,
    DeliveryMutationOutcome,
    MoveCommand,
    MutationAccountSnapshot,
    MutationProjection,
    MutationProjectionError,
    MutationProviderAccess,
    MutationProviderError,
    MutationProviderPurpose,
    SaveToMailboxCommand,
    SendCommand,
    SentCopyMutationOutcome,
    SetEmailFlagsCommand,
)
from mcp_email_server.config import EmailSettings, Settings
from mcp_email_server.emails.classic import ClassicEmailHandler, _validate_flags
from mcp_email_server.metadata_index import MetadataIndex, MetadataIndexError

_T = TypeVar("_T")


async def _bounded_mutation_call(awaitable: Awaitable[_T]) -> _T:
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except (ValueError, PermissionError):
        raise
    except Exception:
        raise MutationProviderError("provider_failure: mutation provider request failed") from None


@dataclass(frozen=True)
class _ResolvedMutationAccount:
    account: EmailSettings
    settings: Settings
    snapshot: MutationAccountSnapshot


class ClassicMutationProvider:
    """Adapt classic SMTP/IMAP primitives to effect-aware mutation ports."""

    def __init__(self, handler: ClassicEmailHandler) -> None:
        self._handler = handler

    async def set_flags(
        self,
        command: SetEmailFlagsCommand,
        account: MutationAccountSnapshot,
    ) -> BatchMutationOutcome:
        return await _bounded_mutation_call(
            self._handler.incoming_client.set_email_flags_with_outcome(
                list(command.email_ids),
                command.operation,
                list(command.flags),
                command.mailbox,
                list(account.allowed_senders),
                account.report_blocked_mutations,
            )
        )

    async def save_to_mailbox(
        self,
        command: SaveToMailboxCommand,
        account: MutationAccountSnapshot,
    ) -> AppendMutationOutcome:
        del account
        message = self._handler.incoming_client.compose_message(
            list(command.recipients),
            command.subject,
            command.body,
            list(command.cc) or None,
            list(command.bcc) or None,
            command.html,
            list(command.attachments) or None,
            command.in_reply_to,
            command.references,
            include_bcc_header=True,
        )
        flags = r"(\Draft \Seen)" if command.flags is None else _validate_flags(list(command.flags))
        return await _bounded_mutation_call(
            self._handler.incoming_client.append_to_mailbox_with_outcome(
                message,
                self._handler.email_settings.incoming,
                command.mailbox,
                flags,
            )
        )

    async def delete(
        self,
        command: DeleteCommand,
        account: MutationAccountSnapshot,
    ) -> BatchMutationOutcome:
        return await _bounded_mutation_call(
            self._handler.incoming_client.delete_emails_with_outcome(
                list(command.email_ids),
                command.mailbox,
                list(account.allowed_senders),
                account.report_blocked_mutations,
            )
        )

    async def move(
        self,
        command: MoveCommand,
        account: MutationAccountSnapshot,
    ) -> BatchMutationOutcome:
        return await _bounded_mutation_call(
            self._handler.incoming_client.move_emails_with_outcome(
                list(command.email_ids),
                command.source_mailbox,
                command.destination_mailbox,
                list(account.allowed_senders),
                account.report_blocked_mutations,
            )
        )

    async def find_archive_mailbox(self, source_mailbox: str) -> str:
        archive_mailbox = await _bounded_mutation_call(self._handler._find_archive_folder())
        if archive_mailbox is None or archive_mailbox == source_mailbox:
            raise ValueError(
                "No distinct Archive folder found (looked for the RFC 6154 \\Archive flag and common names)"
            )
        return archive_mailbox

    async def send(
        self,
        command: SendCommand,
        account: MutationAccountSnapshot,
    ) -> DeliveryMutationOutcome:
        del account
        client = self._handler.outgoing_client
        if client is None:
            raise MutationProviderError("capability_unavailable: SMTP is not configured for this account")
        return await _bounded_mutation_call(
            client.send_email_with_outcome(
                list(command.recipients),
                command.subject,
                command.body,
                list(command.cc) or None,
                list(command.bcc) or None,
                command.html,
                list(command.attachments) or None,
                command.in_reply_to,
                command.references,
                command.reply_to,
            )
        )

    async def save_sent_copy(
        self,
        sent_message: object,
        bcc: tuple[str, ...],
    ) -> SentCopyMutationOutcome:
        if not self._handler.save_to_sent:
            return SentCopyMutationOutcome("skipped")
        if not isinstance(sent_message, (MIMEText, MIMEMultipart)):
            raise MutationProviderError("provider_failure: sent message evidence is invalid")
        # BCC belongs only in the local copy and must be added after SMTP submission.
        if bcc and sent_message["Bcc"] is None:
            sent_message["Bcc"] = ", ".join(bcc)
        return await _bounded_mutation_call(
            self._handler.incoming_client.append_to_sent_with_outcome(
                sent_message,
                self._handler.email_settings.incoming,
                self._handler.sent_folder_name,
            )
        )


class SQLiteMutationProjection:
    def __init__(self, index: MetadataIndex, operational_account_id: str) -> None:
        self._index = index
        self._operational_account_id = operational_account_id

    async def invalidate(self, mailboxes: tuple[str, ...]) -> None:
        try:
            await asyncio.to_thread(
                self._index.invalidate_mailboxes,
                self._operational_account_id,
                mailboxes,
            )
        except MetadataIndexError as exc:
            raise MutationProjectionError("Operational metadata projection invalidation failed") from exc


class LocalMutationBackend:
    """Resolve current local authority for each independent mutation effect."""

    @staticmethod
    def _resolve(
        account_name: str,
        *,
        roles: tuple[BindingRole, ...] = (),
        expected_mode: RuntimeMode | None = None,
    ) -> _ResolvedMutationAccount:
        resolved = (
            resolve_local_account(account_name, roles=roles, expected_mode=expected_mode)
            if roles
            else resolve_local_account(account_name, expected_mode=expected_mode)
        )
        return _ResolvedMutationAccount(
            account=resolved.account,
            settings=resolved.settings,
            snapshot=MutationAccountSnapshot(
                account_name=resolved.account.account_name,
                mode=resolved.mode,
                allowed_senders=tuple(resolved.settings.allowed_senders),
                allowed_recipients=tuple(resolved.settings.allowed_recipients),
                report_blocked_mutations=resolved.settings.report_blocked_mutations,
            ),
        )

    def resolve(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode | None = None,
    ) -> MutationAccountSnapshot:
        return self._resolve(account_name, expected_mode=expected_mode).snapshot

    def open(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode,
        purpose: MutationProviderPurpose,
    ) -> MutationProviderAccess:
        roles: tuple[BindingRole, ...] = ("outgoing",) if purpose == "outgoing" else ("incoming",)
        resolved = self._resolve(account_name, roles=roles, expected_mode=expected_mode)
        return MutationProviderAccess(
            account=resolved.snapshot,
            provider=ClassicMutationProvider(ClassicEmailHandler(resolved.account)),
        )

    async def open_projection(self, account: MutationAccountSnapshot) -> MutationProjection:
        resolved = self._resolve(account.account_name, expected_mode=account.mode)
        index = MetadataIndex(Path(resolved.settings.db_location), account.mode)
        try:
            operational_account_id = await asyncio.to_thread(index.resolve_operational_account, resolved.account)
        except MetadataIndexError as exc:
            raise MutationProjectionError("Operational metadata projection is unavailable") from exc
        return SQLiteMutationProjection(index, operational_account_id)


class LocalMutationProjectionFactory:
    def __init__(self, backend: LocalMutationBackend) -> None:
        self._backend = backend

    async def open(self, account: MutationAccountSnapshot) -> MutationProjection:
        return await self._backend.open_projection(account)
