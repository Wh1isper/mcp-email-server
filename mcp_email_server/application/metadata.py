from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from mcp_email_server.config import sender_allowed
from mcp_email_server.emails.models import EmailMetadata, EmailMetadataPageResponse, MailboxInfo
from mcp_email_server.log import logger

RuntimeMode = Literal["legacy", "managed"]
MAX_METADATA_SNAPSHOT_ROWS = 1_000


class MetadataQueryTooBroadError(ValueError):
    """A metadata request cannot prove an exact result within its work budget."""


class MetadataProjectionError(RuntimeError):
    """The rebuildable metadata projection is unavailable or inconsistent."""


class MetadataProviderObservationError(RuntimeError):
    """A provider observation cannot safely satisfy the metadata contract."""


class MetadataProviderError(RuntimeError):
    """A provider request failed without exposing transport-controlled detail."""


@dataclass(frozen=True)
class ListEmailMetadataQuery:
    account_name: str
    page: int = 1
    page_size: int = 10
    before: datetime | None = None
    since: datetime | None = None
    subject: str | None = None
    from_address: str | None = None
    to_address: str | None = None
    order: Literal["asc", "desc"] = "desc"
    mailbox: str = "INBOX"
    seen: bool | None = None
    flagged: bool | None = None
    answered: bool | None = None
    body: str | None = None
    text: str | None = None
    has_attachment: bool | None = None

    def validate(self) -> None:
        if not self.account_name.strip():
            raise ValueError("account_name must not be empty")
        if self.page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if self.order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        if not self.mailbox.strip():
            raise ValueError("mailbox must not be empty")

    @property
    def index_supported(self) -> bool:
        # Provider text/date matching and mutable flag filters remain on IMAP
        # until equivalent projection semantics and invalidation are proven.
        return all(
            value is None
            for value in (
                self.before,
                self.since,
                self.subject,
                self.from_address,
                self.to_address,
                self.seen,
                self.flagged,
                self.answered,
                self.body,
                self.text,
                self.has_attachment,
            )
        )


@dataclass(frozen=True)
class MetadataAccountSnapshot:
    account_name: str
    mode: RuntimeMode
    allowed_senders: tuple[str, ...]


@dataclass(frozen=True)
class MailboxState:
    uidvalidity: int
    uidnext: int
    message_count: int


@dataclass(frozen=True)
class MailboxMetadataSnapshot:
    state: MailboxState
    mailbox: MailboxInfo
    emails: tuple[dict[str, object], ...]
    complete: bool
    observed_at: datetime


class MetadataAccountAuthority(Protocol):
    def resolve(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode | None = None,
    ) -> MetadataAccountSnapshot: ...


class MetadataProvider(Protocol):
    async def list_metadata(
        self,
        query: ListEmailMetadataQuery,
        account: MetadataAccountSnapshot,
    ) -> EmailMetadataPageResponse: ...

    async def mailbox_state(self, mailbox: str) -> MailboxState: ...

    async def mailbox_snapshot(self, mailbox: str) -> MailboxMetadataSnapshot: ...


@dataclass(frozen=True)
class MetadataProviderAccess:
    account: MetadataAccountSnapshot
    provider: MetadataProvider


class MetadataProviderFactory(Protocol):
    def open(self, account_name: str, *, expected_mode: RuntimeMode) -> MetadataProviderAccess: ...


class MetadataProjection(Protocol):
    async def read_complete(
        self,
        mailbox: str,
        state: MailboxState,
    ) -> list[dict[str, object]] | None: ...

    async def write_snapshot(self, mailbox: str, snapshot: MailboxMetadataSnapshot) -> None: ...


class MetadataProjectionFactory(Protocol):
    async def open(self, account: MetadataAccountSnapshot) -> MetadataProjection: ...


def _validate_snapshot(query: ListEmailMetadataQuery, snapshot: MailboxMetadataSnapshot) -> None:
    state = snapshot.state
    if state.uidvalidity < 1 or state.uidnext < 1 or state.message_count < 0:
        raise MetadataProviderObservationError("Provider metadata state is invalid")
    if snapshot.mailbox.name != query.mailbox:
        raise MetadataProviderObservationError("Provider metadata mailbox does not match the request")
    if len(snapshot.emails) > MAX_METADATA_SNAPSHOT_ROWS:
        raise MetadataProviderObservationError("Provider metadata snapshot exceeds the retention bound")
    raw_uids = [email.get("email_id") for email in snapshot.emails]
    if any(not isinstance(uid, str) or not uid.isdigit() for uid in raw_uids):
        raise MetadataProviderObservationError("Provider metadata UID is invalid")
    uids = [int(uid) for uid in raw_uids if isinstance(uid, str)]
    if any(uid <= 0 for uid in uids) or len(set(uids)) != len(uids):
        raise MetadataProviderObservationError("Provider metadata UIDs are invalid")
    if snapshot.complete and (
        len(uids) != state.message_count
        or any(uid >= state.uidnext for uid in uids)
        or any(not isinstance(email.get("_internal_date"), datetime) for email in snapshot.emails)
    ):
        raise MetadataProviderObservationError("Complete provider metadata is inconsistent with mailbox state")


def _sort_key(email: dict[str, object], *, descending: bool) -> tuple[bool, datetime, int]:
    internal_date = email.get("_internal_date")
    uid_text = email.get("email_id")
    uid = int(uid_text) if isinstance(uid_text, str) and uid_text.isdigit() else 0
    if isinstance(internal_date, datetime):
        normalized = internal_date if internal_date.tzinfo is not None else internal_date.replace(tzinfo=UTC)
        return (descending, normalized.astimezone(UTC), uid)
    boundary = datetime.min.replace(tzinfo=UTC) if descending else datetime.max.replace(tzinfo=UTC)
    return (not descending, boundary, uid)


def _page_response(
    query: ListEmailMetadataQuery,
    emails: list[dict[str, object]],
    allowed_senders: tuple[str, ...],
) -> EmailMetadataPageResponse:
    visible = [email for email in emails if sender_allowed(str(email.get("from", "")), list(allowed_senders))]
    visible.sort(key=lambda email: _sort_key(email, descending=query.order == "desc"), reverse=query.order == "desc")
    start = (query.page - 1) * query.page_size
    page = visible[start : start + query.page_size]
    return EmailMetadataPageResponse(
        page=query.page,
        page_size=query.page_size,
        before=query.before,
        since=query.since,
        subject=query.subject,
        emails=[EmailMetadata.from_email(email) for email in page],
        total=len(visible),
    )


class MetadataQueryService:
    """Own metadata index eligibility, qualification, refresh, and fallback."""

    def __init__(
        self,
        accounts: MetadataAccountAuthority,
        providers: MetadataProviderFactory,
        projections: MetadataProjectionFactory,
    ) -> None:
        self._accounts = accounts
        self._providers = providers
        self._projections = projections

    def _open_provider(self, query: ListEmailMetadataQuery, mode: RuntimeMode) -> MetadataProviderAccess:
        return self._providers.open(query.account_name, expected_mode=mode)

    async def _provider_fallback(
        self,
        query: ListEmailMetadataQuery,
        mode: RuntimeMode,
    ) -> EmailMetadataPageResponse:
        access = self._open_provider(query, mode)
        return await access.provider.list_metadata(query, access.account)

    async def execute(  # noqa: C901 - explicit bounded workflow branches
        self,
        query: ListEmailMetadataQuery,
    ) -> EmailMetadataPageResponse:
        query.validate()
        account = self._accounts.resolve(query.account_name)
        if not query.index_supported:
            return await self._provider_fallback(query, account.mode)

        try:
            projection = await self._projections.open(account)
        except MetadataProjectionError:
            if account.mode == "managed":
                raise
            logger.warning("Operational metadata index is unavailable; using bounded provider fallback")
            return await self._provider_fallback(query, account.mode)

        # Re-resolve lifecycle authority immediately before each provider access.
        access = self._open_provider(query, account.mode)
        try:
            state = await access.provider.mailbox_state(query.mailbox)
        except Exception:
            logger.warning("Metadata index qualification failed; using bounded provider fallback")
            return await self._provider_fallback(query, account.mode)

        try:
            indexed = await projection.read_complete(query.mailbox, state)
        except MetadataProjectionError:
            if account.mode == "managed":
                raise
            logger.warning("Operational metadata index read failed; using bounded provider fallback")
            return await self._provider_fallback(query, account.mode)
        if indexed is not None:
            return _page_response(query, indexed, access.account.allowed_senders)

        access = self._open_provider(query, account.mode)
        try:
            snapshot = await access.provider.mailbox_snapshot(query.mailbox)
        except MetadataQueryTooBroadError:
            raise
        except Exception:
            logger.warning("Metadata projection refresh failed; using bounded provider fallback")
            return await self._provider_fallback(query, account.mode)

        _validate_snapshot(query, snapshot)
        try:
            await projection.write_snapshot(query.mailbox, snapshot)
        except MetadataProjectionError:
            if account.mode == "managed":
                raise
            # Legacy projection storage is optional after the provider
            # observation has independently satisfied the public contract.
            logger.warning("Metadata projection write failed; returning bounded provider observation")
        if snapshot.complete:
            return _page_response(query, [dict(email) for email in snapshot.emails], access.account.allowed_senders)
        return await self._provider_fallback(query, account.mode)
