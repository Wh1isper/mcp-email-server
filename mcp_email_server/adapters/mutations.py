from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TypeVar

from mcp_email_server import config as config_module
from mcp_email_server.application.metadata import RuntimeMode
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    BatchMutationOutcome,
    DeleteCommand,
    DeliveryMutationOutcome,
    MarkReadCommand,
    MoveCommand,
    MutationAccountSnapshot,
    MutationProjection,
    MutationProjectionError,
    MutationProviderAccess,
    MutationProviderError,
    SaveToMailboxCommand,
    SendCommand,
    SentCopyMutationOutcome,
)
from mcp_email_server.bootstrap import process_bootstrap
from mcp_email_server.config import EmailSettings, ProviderSettings, Settings, get_settings
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

    async def mark_read(
        self,
        command: MarkReadCommand,
        account: MutationAccountSnapshot,
    ) -> BatchMutationOutcome:
        return await _bounded_mutation_call(
            self._handler.incoming_client.mark_emails_as_read_with_outcome(
                list(command.email_ids),
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
        client = self._handler.outgoing_client
        if client is None:
            return SentCopyMutationOutcome("failed", detail="smtp-config-changed")
        return await _bounded_mutation_call(
            client.append_to_sent_with_outcome(
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
        expected_mode: RuntimeMode | None = None,
    ) -> _ResolvedMutationAccount:
        mode = process_bootstrap(config_module.CONFIG_PATH).mode
        if expected_mode is not None and mode != expected_mode:
            raise RuntimeError("Configuration mode changed; restart required")
        settings = get_settings(reload=mode == "managed")
        account = settings.get_account(account_name)
        if isinstance(account, ProviderSettings):
            raise NotImplementedError
        if not isinstance(account, EmailSettings):
            account_names = [item.account_name for item in settings.get_accounts()]
            raise ValueError(  # noqa: TRY004 - preserve the public unknown-account category
                f"Account {account_name} not found, available accounts: {account_names}"
            )
        return _ResolvedMutationAccount(
            account=account,
            settings=settings,
            snapshot=MutationAccountSnapshot(
                account_name=account.account_name,
                mode=mode,
                allowed_senders=tuple(settings.allowed_senders),
                allowed_recipients=tuple(settings.allowed_recipients),
                report_blocked_mutations=settings.report_blocked_mutations,
            ),
        )

    def resolve(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode | None = None,
    ) -> MutationAccountSnapshot:
        return self._resolve(account_name, expected_mode=expected_mode).snapshot

    def open(self, account_name: str, *, expected_mode: RuntimeMode) -> MutationProviderAccess:
        resolved = self._resolve(account_name, expected_mode=expected_mode)
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
