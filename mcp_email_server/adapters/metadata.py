from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from mcp_email_server import config as config_module
from mcp_email_server.adapters.authority import resolve_local_account
from mcp_email_server.application.accounts import EffectiveConfiguration
from mcp_email_server.application.management import BindingRole
from mcp_email_server.application.metadata import (
    ListEmailMetadataQuery,
    MailboxMetadataSnapshot,
    MailboxState,
    MetadataAccountSnapshot,
    MetadataProjection,
    MetadataProjectionError,
    MetadataProviderAccess,
    MetadataProviderError,
    MetadataProviderObservationError,
    MetadataQueryTooBroadError,
    RuntimeMode,
)
from mcp_email_server.bootstrap import process_bootstrap
from mcp_email_server.config import AccountAttributes, EmailSettings, Settings, get_settings
from mcp_email_server.emails.classic import (
    MAX_INDEXED_UID_WINDOW,
    ClassicEmailHandler,
    MetadataPayloadTooLargeError,
)
from mcp_email_server.emails.models import EmailMetadata, EmailMetadataPageResponse
from mcp_email_server.imap_keywords import ImapKeywordRegistry
from mcp_email_server.metadata_index import MetadataIndex, MetadataIndexError

_T = TypeVar("_T")


async def _bounded_provider_call(awaitable: Awaitable[_T]) -> _T:
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except (MetadataPayloadTooLargeError, MetadataProviderObservationError, MetadataQueryTooBroadError):
        raise
    except Exception:
        raise MetadataProviderError("provider_failure: metadata provider request failed") from None


@dataclass(frozen=True)
class _ResolvedAccount:
    account: EmailSettings
    settings: Settings
    snapshot: MetadataAccountSnapshot


class ClassicMetadataProvider:
    """Adapt the classic IMAP handler to the metadata provider port."""

    def __init__(self, handler: ClassicEmailHandler) -> None:
        self._handler = handler

    async def list_metadata(
        self,
        query: ListEmailMetadataQuery,
        account: MetadataAccountSnapshot,
    ) -> EmailMetadataPageResponse:
        total, email_dicts = await _bounded_provider_call(
            self._handler.incoming_client.get_emails_metadata(
                page=query.page,
                page_size=query.page_size,
                before=query.before,
                since=query.since,
                subject=query.subject,
                from_address=query.from_address,
                to_address=query.to_address,
                order=query.order,
                mailbox=query.mailbox,
                seen=query.seen,
                flagged=query.flagged,
                answered=query.answered,
                body=query.body,
                text=query.text,
                has_attachment=query.has_attachment,
                tag_keywords=list(query.provider_keywords),
                tag_match=query.tag_match,
                allowed_senders=list(account.allowed_senders),
            )
        )
        return EmailMetadataPageResponse(
            page=query.page,
            page_size=query.page_size,
            before=query.before,
            since=query.since,
            subject=query.subject,
            emails=[EmailMetadata.from_email(email) for email in email_dicts],
            total=total,
        )

    async def mailbox_state(self, mailbox: str) -> MailboxState:
        return await _bounded_provider_call(self._handler.incoming_client.get_mailbox_state(mailbox))

    async def mailbox_snapshot(self, mailbox: str) -> MailboxMetadataSnapshot:
        return await _bounded_provider_call(
            self._handler.incoming_client.get_mailbox_metadata_snapshot(
                mailbox,
                maximum_window=MAX_INDEXED_UID_WINDOW,
            )
        )

    async def flags_for(self, mailbox: str, email_ids: tuple[str, ...]) -> dict[str, list[str]]:
        return await _bounded_provider_call(self._handler.incoming_client.get_email_flags(list(email_ids), mailbox))


class SQLiteMetadataProjection:
    """Bind one operational account identity to the SQLite projection port."""

    def __init__(self, index: MetadataIndex, operational_account_id: str) -> None:
        self._index = index
        self._operational_account_id = operational_account_id

    async def read_complete(
        self,
        mailbox: str,
        state: MailboxState,
    ) -> list[dict[str, object]] | None:
        try:
            snapshot = await asyncio.to_thread(
                self._index.read_complete,
                self._operational_account_id,
                mailbox,
                uidvalidity=state.uidvalidity,
                uidnext=state.uidnext,
                message_count=state.message_count,
            )
        except MetadataIndexError as exc:
            raise MetadataProjectionError("Operational metadata projection read failed") from exc
        if snapshot is None:
            return None
        return [dict(email) for email in snapshot.emails]

    async def write_snapshot(self, mailbox: str, snapshot: MailboxMetadataSnapshot) -> None:
        try:
            await asyncio.to_thread(
                self._index.write_snapshot,
                self._operational_account_id,
                mailbox,
                delimiter=snapshot.mailbox.delimiter or None,
                attributes=snapshot.mailbox.flags,
                uidvalidity=snapshot.state.uidvalidity,
                uidnext=snapshot.state.uidnext,
                message_count=snapshot.state.message_count,
                emails=[dict(email) for email in snapshot.emails],
                complete=snapshot.complete,
                observed_at=snapshot.observed_at,
            )
        except MetadataIndexError as exc:
            raise MetadataProjectionError("Operational metadata projection write failed") from exc


class LocalMetadataBackend:
    """Config, classic-provider, and SQLite adapters for local composition."""

    @staticmethod
    def _resolve(
        account_name: str,
        *,
        roles: tuple[BindingRole, ...] = (),
        expected_mode: RuntimeMode | None = None,
    ) -> _ResolvedAccount:
        resolved = (
            resolve_local_account(account_name, roles=roles, expected_mode=expected_mode)
            if roles
            else resolve_local_account(account_name, expected_mode=expected_mode)
        )
        return _ResolvedAccount(
            account=resolved.account,
            settings=resolved.settings,
            snapshot=MetadataAccountSnapshot(
                account_name=resolved.account.account_name,
                mode=resolved.mode,
                allowed_senders=tuple(resolved.settings.allowed_senders),
                tag_registry=ImapKeywordRegistry.from_tags(resolved.account.tags),
            ),
        )

    def resolve(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode | None = None,
    ) -> MetadataAccountSnapshot:
        return self._resolve(account_name, expected_mode=expected_mode).snapshot

    def open(self, account_name: str, *, expected_mode: RuntimeMode) -> MetadataProviderAccess:
        resolved = self._resolve(
            account_name,
            roles=("incoming",),
            expected_mode=expected_mode,
        )
        return MetadataProviderAccess(
            account=resolved.snapshot,
            provider=ClassicMetadataProvider(ClassicEmailHandler(resolved.account)),
        )

    async def open_projection(self, account: MetadataAccountSnapshot) -> MetadataProjection:
        resolved = self._resolve(account.account_name, expected_mode=account.mode)
        index = MetadataIndex(Path(resolved.settings.db_location), account.mode)
        try:
            operational_account_id = await asyncio.to_thread(index.resolve_operational_account, resolved.account)
        except MetadataIndexError as exc:
            raise MetadataProjectionError("Operational metadata projection is unavailable") from exc
        return SQLiteMetadataProjection(index, operational_account_id)

    def list_effective_accounts(self) -> list[AccountAttributes]:
        return list(self.effective_configuration().accounts)

    def effective_configuration(self) -> EffectiveConfiguration:
        mode = process_bootstrap(config_module.CONFIG_PATH).mode
        settings = get_settings(reload=mode == "managed")
        return EffectiveConfiguration(
            accounts=tuple(account.masked() for account in settings.get_accounts()),
            allowed_recipients=tuple(settings.allowed_recipients),
            allowed_senders=tuple(settings.allowed_senders),
        )


class LocalMetadataProjectionFactory:
    """Expose the backend's projection constructor as its narrow port."""

    def __init__(self, backend: LocalMetadataBackend) -> None:
        self._backend = backend

    async def open(self, account: MetadataAccountSnapshot) -> MetadataProjection:
        return await self._backend.open_projection(account)
