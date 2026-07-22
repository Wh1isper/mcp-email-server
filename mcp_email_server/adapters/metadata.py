from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from mcp_email_server import config as config_module
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
from mcp_email_server.config import AccountAttributes, EmailSettings, ProviderSettings, Settings, get_settings
from mcp_email_server.emails.classic import (
    MAX_INDEXED_UID_WINDOW,
    ClassicEmailHandler,
    MetadataPayloadTooLargeError,
)
from mcp_email_server.emails.models import EmailMetadataPageResponse
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

    async def list_metadata(self, query: ListEmailMetadataQuery) -> EmailMetadataPageResponse:
        return await _bounded_provider_call(
            self._handler.get_emails_metadata(
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
            )
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
        expected_mode: RuntimeMode | None = None,
    ) -> _ResolvedAccount:
        bootstrap = process_bootstrap(config_module.CONFIG_PATH)
        mode = bootstrap.mode
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
        return _ResolvedAccount(
            account=account,
            settings=settings,
            snapshot=MetadataAccountSnapshot(
                account_name=account.account_name,
                mode=mode,
                allowed_senders=tuple(settings.allowed_senders),
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
        resolved = self._resolve(account_name, expected_mode=expected_mode)
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
        mode = process_bootstrap(config_module.CONFIG_PATH).mode
        settings = get_settings(reload=mode == "managed")
        return [account.masked() for account in settings.get_accounts()]


class LocalMetadataProjectionFactory:
    """Expose the backend's projection constructor as its narrow port."""

    def __init__(self, backend: LocalMetadataBackend) -> None:
        self._backend = backend

    async def open(self, account: MetadataAccountSnapshot) -> MetadataProjection:
        return await self._backend.open_projection(account)
