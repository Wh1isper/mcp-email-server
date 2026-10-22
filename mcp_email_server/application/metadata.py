from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeVar

from mcp_email_server.application.limits import (
    APPLICATION_LIMITS,
    metadata_snapshot_row_limit,
    validate_controlled_string,
    validate_optional_controlled_string,
    validate_serialized_result,
)
from mcp_email_server.config import sender_allowed
from mcp_email_server.emails.models import EmailMetadata, EmailMetadataPageResponse, MailboxInfo
from mcp_email_server.imap_keywords import ImapKeywordRegistry, ImapKeywordTag
from mcp_email_server.log import log_safe_failure, logger

RuntimeMode = Literal["legacy", "managed"]
ProviderResultT = TypeVar("ProviderResultT")


class MetadataQueryTooBroadError(ValueError):
    """A metadata request cannot prove an exact result within its work budget."""


class MetadataProjectionError(RuntimeError):
    """The rebuildable metadata projection is unavailable or inconsistent."""


class MetadataProviderObservationError(RuntimeError):
    """A provider observation cannot safely satisfy the metadata contract."""


class MetadataProviderError(RuntimeError):
    """A provider request failed without exposing transport-controlled detail."""

    def __init__(self, message: str, *, diagnostic: BaseException | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class _DefaultProviderTimeout:
    """Use the shared provider deadline, read when the call starts."""


_DEFAULT_PROVIDER_TIMEOUT = _DefaultProviderTimeout()


async def _bounded_provider_call(
    operation: Awaitable[ProviderResultT],
    *,
    timeout: float | _DefaultProviderTimeout | None = _DEFAULT_PROVIDER_TIMEOUT,
) -> ProviderResultT:
    """Bound one provider call.

    A metadata snapshot passes ``timeout=None``. Each of its UID batches applies
    ``MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS`` on its own, so one
    60-second deadline cannot cancel a refresh that has already stored progress.
    """

    limit = APPLICATION_LIMITS.provider_timeout_seconds if isinstance(timeout, _DefaultProviderTimeout) else timeout
    try:
        if limit is None:
            return await operation
        async with asyncio.timeout(limit):
            return await operation
    except TimeoutError as exc:
        raise MetadataProviderError("provider request timed out", diagnostic=exc) from None


def normalize_datetime_boundary(value: datetime | None, *, field_name: str) -> datetime | None:
    """Return one timezone-aware query boundary as a UTC instant."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ValueError(f"{field_name} cannot be represented in UTC") from None


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
    semantic_tags: tuple[str, ...] = ()
    tag_match: Literal["all", "any"] = "all"
    provider_keywords: tuple[str, ...] = ()

    def validate(self) -> None:
        validate_controlled_string(
            self.account_name,
            field_name="account_name",
            maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
        )
        if self.page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if self.order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        normalize_datetime_boundary(self.before, field_name="before")
        normalize_datetime_boundary(self.since, field_name="since")
        validate_controlled_string(
            self.mailbox,
            field_name="mailbox",
            maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
        )
        validate_optional_controlled_string(
            self.subject,
            field_name="subject query",
            maximum_bytes=APPLICATION_LIMITS.query_bytes,
        )
        if len(self.semantic_tags) > APPLICATION_LIMITS.flags:
            raise ValueError(f"semantic_tags must contain at most {APPLICATION_LIMITS.flags} values")
        if len({tag.casefold() for tag in self.semantic_tags}) != len(self.semantic_tags):
            raise ValueError("semantic_tags must not contain duplicates, ignoring case")
        for tag in self.semantic_tags:
            validate_controlled_string(
                tag,
                field_name="semantic_tags item",
                maximum_bytes=APPLICATION_LIMITS.flag_bytes,
            )
        if self.tag_match not in ("all", "any"):
            raise ValueError("tag_match must be 'all' or 'any'")
        validate_optional_controlled_string(
            self.from_address,
            field_name="from_address query",
            maximum_bytes=APPLICATION_LIMITS.address_bytes,
        )
        validate_optional_controlled_string(
            self.to_address,
            field_name="to_address query",
            maximum_bytes=APPLICATION_LIMITS.address_bytes,
        )
        validate_optional_controlled_string(
            self.body,
            field_name="body query",
            maximum_bytes=APPLICATION_LIMITS.query_bytes,
        )
        validate_optional_controlled_string(
            self.text,
            field_name="text query",
            maximum_bytes=APPLICATION_LIMITS.query_bytes,
        )

    @property
    def index_supported(self) -> bool:
        # Provider text/date matching and mutable flag filters remain on IMAP
        # until equivalent projection semantics and invalidation are proven.
        return not self.provider_keywords and all(
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
    tag_registry: ImapKeywordRegistry = field(default_factory=ImapKeywordRegistry)


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


@dataclass(frozen=True)
class MetadataIncrementalBaseline:
    """One complete same-epoch projection that can accept newer UIDs."""

    state: MailboxState
    high_uid: int | None
    mailbox: MailboxInfo
    emails: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class MailboxUidDelta:
    """UIDs observed strictly above a cached high-water mark."""

    state: MailboxState
    mailbox: MailboxInfo
    emails: tuple[dict[str, object], ...]
    observed_at: datetime
    uidvalidity_changed: bool
    resumable: bool


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

    async def mailbox_snapshot(
        self,
        mailbox: str,
        *,
        retained_emails: tuple[dict[str, object], ...] = (),
        retained_uidvalidity: int | None = None,
    ) -> MailboxMetadataSnapshot: ...

    async def mailbox_uid_delta(
        self,
        mailbox: str,
        minimum_exclusive_uid: int,
        *,
        maximum_new_messages: int,
    ) -> MailboxUidDelta: ...

    async def flags_for(self, mailbox: str, email_ids: tuple[str, ...]) -> dict[str, list[str]]: ...


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

    async def read_incremental_baseline(
        self,
        mailbox: str,
        uidvalidity: int,
    ) -> MetadataIncrementalBaseline | None: ...

    async def read_partial_emails(
        self,
        mailbox: str,
        uidvalidity: int,
    ) -> tuple[dict[str, object], ...] | None: ...

    async def write_snapshot(self, mailbox: str, snapshot: MailboxMetadataSnapshot) -> None: ...


class MetadataProjectionFactory(Protocol):
    async def open(self, account: MetadataAccountSnapshot) -> MetadataProjection: ...


def _validate_snapshot(  # noqa: C901 - validate every independent provider-observation bound
    query: ListEmailMetadataQuery,
    snapshot: MailboxMetadataSnapshot,
) -> None:
    state = snapshot.state
    if state.uidvalidity < 1 or state.uidnext < 1 or state.message_count < 0:
        raise MetadataProviderObservationError("Provider metadata state is invalid")
    if snapshot.mailbox.name != query.mailbox:
        raise MetadataProviderObservationError("Provider metadata mailbox does not match the request")
    if len(snapshot.emails) > metadata_snapshot_row_limit():
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
    for email in snapshot.emails:
        flags = email.get("_flags", [])
        if (
            not isinstance(flags, list)
            or len(flags) > APPLICATION_LIMITS.flags
            or any(not isinstance(flag, str) for flag in flags)
        ):
            raise MetadataProviderObservationError("Provider metadata flags are invalid")
        try:
            for flag in flags:
                validate_controlled_string(
                    flag,
                    field_name="provider flag",
                    maximum_bytes=APPLICATION_LIMITS.flag_bytes,
                )
        except ValueError:
            raise MetadataProviderObservationError("Provider metadata flags are invalid") from None


def _sort_key(email: dict[str, object], *, descending: bool) -> tuple[bool, datetime, int]:
    internal_date = email.get("_internal_date")
    uid_text = email.get("email_id")
    uid = int(uid_text) if isinstance(uid_text, str) and uid_text.isdigit() else 0
    if isinstance(internal_date, datetime):
        normalized = internal_date if internal_date.tzinfo is not None else internal_date.replace(tzinfo=UTC)
        return (descending, normalized.astimezone(UTC), uid)
    boundary = datetime.min.replace(tzinfo=UTC) if descending else datetime.max.replace(tzinfo=UTC)
    return (not descending, boundary, uid)


def _email_flag_set(email: dict[str, object]) -> set[str]:
    raw = email.get("_flags", [])
    if not isinstance(raw, list) or any(not isinstance(flag, str) for flag in raw):
        raise MetadataProviderObservationError("Provider metadata flags are invalid")
    return set(raw)


def _validate_metadata_response(
    query: ListEmailMetadataQuery,
    response: EmailMetadataPageResponse,
) -> EmailMetadataPageResponse:
    if len(response.emails) > query.page_size:
        raise MetadataProviderObservationError("Provider metadata page exceeds the requested page size")
    if len(response.warnings) > APPLICATION_LIMITS.warning_items:
        raise MetadataProviderObservationError("Provider metadata warning count exceeds the limit")
    for warning in response.warnings:
        try:
            validate_controlled_string(
                warning,
                field_name="metadata warning",
                maximum_bytes=APPLICATION_LIMITS.error_detail_bytes,
            )
        except ValueError:
            raise MetadataProviderObservationError("Provider metadata warning is invalid") from None
    header_bytes = sum(
        len(email.email_id.encode("utf-8"))
        + len((email.message_id or "").encode("utf-8"))
        + len(email.subject.encode("utf-8"))
        + len(email.sender.encode("utf-8"))
        + sum(len(value.encode("utf-8")) for value in email.recipients)
        + sum(len(value.encode("utf-8")) for value in email.attachments)
        + sum(len(value.encode("utf-8")) for value in email.provider_keywords)
        + sum(len(value.encode("utf-8")) for value in email.semantic_tags)
        for email in response.emails
    )
    if header_bytes > APPLICATION_LIMITS.aggregate_header_bytes:
        raise MetadataProviderObservationError("Provider metadata headers exceed the aggregate size limit")
    try:
        validate_serialized_result(response.model_dump_json().encode("utf-8"))
    except ValueError:
        raise MetadataProviderObservationError("Provider metadata serialized result exceeds the size limit") from None
    return response


def _page_response(
    query: ListEmailMetadataQuery,
    emails: list[dict[str, object]],
    allowed_senders: tuple[str, ...],
    *,
    warnings: tuple[Literal["projection_write_failed"], ...] = (),
) -> EmailMetadataPageResponse:
    visible = [email for email in emails if sender_allowed(str(email.get("from", "")), list(allowed_senders))]
    if query.provider_keywords:
        requested = set(query.provider_keywords)
        visible = [
            email
            for email in visible
            if (
                requested.issubset(_email_flag_set(email))
                if query.tag_match == "all"
                else bool(requested.intersection(_email_flag_set(email)))
            )
        ]
    visible.sort(key=lambda email: _sort_key(email, descending=query.order == "desc"), reverse=query.order == "desc")
    start = (query.page - 1) * query.page_size
    page = visible[start : start + query.page_size]
    return _validate_metadata_response(
        query,
        EmailMetadataPageResponse(
            page=query.page,
            page_size=query.page_size,
            before=query.before,
            since=query.since,
            subject=query.subject,
            emails=[EmailMetadata.from_email(email) for email in page],
            total=len(visible),
            warnings=list(warnings),
        ),
    )


def _with_projection_warning(
    query: ListEmailMetadataQuery,
    response: EmailMetadataPageResponse,
) -> EmailMetadataPageResponse:
    warnings = list(dict.fromkeys((*response.warnings, "projection_write_failed")))
    return _validate_metadata_response(query, response.model_copy(update={"warnings": warnings}))


def _log_projection_refresh_failure(exc: BaseException, *, incremental: bool) -> None:
    """Record the causal exception type and value-free traceback locations."""

    diagnostic: BaseException = exc
    if isinstance(exc, MetadataProviderError) and exc.diagnostic is not None:
        diagnostic = exc.diagnostic
    if incremental:
        message = "Incremental metadata sync failed; refreshing the full snapshot"
    else:
        message = "Metadata projection refresh failed; using bounded provider fallback"
    log_safe_failure(message, diagnostic)


def _merge_append_only_snapshot(  # noqa: C901 - explicit append-only UID and coverage checks
    mailbox: str,
    baseline: MetadataIncrementalBaseline,
    delta: MailboxUidDelta,
) -> MailboxMetadataSnapshot | None:
    """Extend a complete projection only when new UIDs prove an append-only epoch.

    UIDVALIDITY must be unchanged. Every new UID must be above the cached high
    water mark, and the provider message count must equal the cached count plus
    those new UIDs. A deletion, a gap, or a mailbox that no longer fits the row
    ceiling returns None so the caller performs a full bounded refresh.
    """

    if not delta.resumable or delta.uidvalidity_changed:
        return None
    if delta.mailbox.name != mailbox or delta.state.uidvalidity != baseline.state.uidvalidity:
        return None
    if baseline.state.message_count != len(baseline.emails):
        return None
    if any(not isinstance(email.get("_internal_date"), datetime) for email in baseline.emails):
        return None
    floor = 0 if baseline.high_uid is None else baseline.high_uid
    seen: set[int] = set()
    for email in baseline.emails:
        raw_uid = email.get("email_id")
        if not isinstance(raw_uid, str) or not raw_uid.isdigit():
            return None
        uid = int(raw_uid)
        if uid <= 0 or uid in seen or uid > floor:
            return None
        seen.add(uid)
    if baseline.emails and (baseline.high_uid is None or max(seen) != baseline.high_uid):
        return None
    if not baseline.emails and baseline.high_uid is not None:
        return None
    new_emails: list[dict[str, object]] = []
    for email in delta.emails:
        raw_uid = email.get("email_id")
        if not isinstance(raw_uid, str) or not raw_uid.isdigit():
            return None
        uid = int(raw_uid)
        if uid <= floor or uid >= delta.state.uidnext or uid in seen:
            return None
        if not isinstance(email.get("_internal_date"), datetime):
            return None
        seen.add(uid)
        new_emails.append(dict(email))
    if delta.state.message_count != baseline.state.message_count + len(new_emails):
        return None
    if len(seen) > metadata_snapshot_row_limit():
        return None
    merged = tuple(dict(email) for email in baseline.emails) + tuple(new_emails)
    return MailboxMetadataSnapshot(
        state=delta.state,
        mailbox=delta.mailbox,
        emails=merged,
        complete=True,
        observed_at=delta.observed_at,
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

    def list_tags(self, account_name: str) -> tuple[ImapKeywordTag, ...]:
        return self._accounts.resolve(account_name).tag_registry.tags

    def _decorate(
        self,
        query: ListEmailMetadataQuery,
        response: EmailMetadataPageResponse,
        registry: ImapKeywordRegistry,
    ) -> EmailMetadataPageResponse:
        decorated = response.model_copy(
            update={
                "emails": [
                    email.model_copy(update={"semantic_tags": registry.semantic_names(email.provider_keywords)})
                    for email in response.emails
                ]
            }
        )
        return _validate_metadata_response(query, decorated)

    async def _refresh_cached_tags(
        self,
        query: ListEmailMetadataQuery,
        response: EmailMetadataPageResponse,
        provider: MetadataProvider,
    ) -> EmailMetadataPageResponse:
        if not response.emails:
            return response
        email_ids = tuple(email.email_id for email in response.emails)
        current_flags = await _bounded_provider_call(provider.flags_for(query.mailbox, email_ids))
        if set(current_flags) != set(email_ids):
            raise MetadataProviderObservationError("Provider returned incomplete flag metadata")
        for flags in current_flags.values():
            if len(flags) > APPLICATION_LIMITS.flags:
                raise MetadataProviderObservationError("Provider metadata flags are invalid")
            try:
                for flag in flags:
                    validate_controlled_string(
                        flag,
                        field_name="provider flag",
                        maximum_bytes=APPLICATION_LIMITS.flag_bytes,
                    )
            except ValueError:
                raise MetadataProviderObservationError("Provider metadata flags are invalid") from None
        emails = [
            email.model_copy(
                update={
                    "provider_keywords": [flag for flag in current_flags[email.email_id] if not flag.startswith("\\")]
                }
            )
            for email in response.emails
        ]
        return response.model_copy(update={"emails": emails})

    def _open_provider(self, query: ListEmailMetadataQuery, mode: RuntimeMode) -> MetadataProviderAccess:
        return self._providers.open(query.account_name, expected_mode=mode)

    @staticmethod
    def _resolve_tags_for_access(
        query: ListEmailMetadataQuery,
        access: MetadataProviderAccess,
    ) -> ListEmailMetadataQuery:
        if not query.semantic_tags:
            return query
        return replace(
            query,
            provider_keywords=access.account.tag_registry.resolve(query.semantic_tags),
        )

    async def _provider_fallback(
        self,
        query: ListEmailMetadataQuery,
        mode: RuntimeMode,
    ) -> tuple[EmailMetadataPageResponse, MetadataAccountSnapshot]:
        access = self._open_provider(query, mode)
        current_query = self._resolve_tags_for_access(query, access)
        response = await _bounded_provider_call(access.provider.list_metadata(current_query, access.account))
        return _validate_metadata_response(current_query, response), access.account

    async def _load_mailbox_snapshot(  # noqa: C901 - explicit refresh, resume, and fallback branches
        self,
        query: ListEmailMetadataQuery,
        account: MetadataAccountSnapshot,
        projection: MetadataProjection,
        access: MetadataProviderAccess,
        state: MailboxState,
    ) -> tuple[MailboxMetadataSnapshot, bool, bool] | None:
        """Refresh one mailbox, preferring UIDs above a complete cached epoch.

        Returns the snapshot, whether it was an append-only extension, and
        whether resumed rows need a fresh flag read. None means the full refresh
        failed after its exception was logged and the caller should use the
        bounded provider fallback. ``query_too_broad`` still propagates.
        """

        try:
            baseline = await projection.read_incremental_baseline(query.mailbox, state.uidvalidity)
        except MetadataProjectionError:
            if account.mode == "managed":
                raise
            logger.warning("Operational metadata index read failed; using bounded provider fallback")
            return None
        if (
            baseline is not None
            and baseline.state.uidvalidity == state.uidvalidity
            and baseline.state.message_count < metadata_snapshot_row_limit()
        ):
            remaining = metadata_snapshot_row_limit() - baseline.state.message_count
            try:
                delta = await _bounded_provider_call(
                    access.provider.mailbox_uid_delta(
                        query.mailbox,
                        0 if baseline.high_uid is None else baseline.high_uid,
                        maximum_new_messages=remaining,
                    )
                )
            except MetadataQueryTooBroadError:
                raise
            except Exception as exc:
                _log_projection_refresh_failure(exc, incremental=True)
            else:
                merged = _merge_append_only_snapshot(query.mailbox, baseline, delta)
                if merged is not None:
                    return merged, True, False
        if baseline is not None:
            retained = baseline.emails
        else:
            try:
                partial_rows = await projection.read_partial_emails(query.mailbox, state.uidvalidity)
            except MetadataProjectionError:
                if account.mode == "managed":
                    raise
                logger.warning("Operational metadata index read failed; using bounded provider fallback")
                return None
            retained = partial_rows or ()
        try:
            # Batches inside the snapshot carry the refresh timeout. One outer
            # deadline would cancel a large mailbox before a later batch starts.
            snapshot = await _bounded_provider_call(
                access.provider.mailbox_snapshot(
                    query.mailbox, retained_emails=retained, retained_uidvalidity=state.uidvalidity
                ),
                timeout=None,
            )
        except MetadataQueryTooBroadError:
            raise
        except Exception as exc:
            _log_projection_refresh_failure(exc, incremental=False)
            return None
        return snapshot, False, bool(retained)

    async def execute(  # noqa: C901 - explicit bounded workflow branches
        self,
        query: ListEmailMetadataQuery,
    ) -> EmailMetadataPageResponse:
        query.validate()
        account = self._accounts.resolve(query.account_name)
        if query.semantic_tags:
            query = replace(
                query,
                provider_keywords=account.tag_registry.resolve(query.semantic_tags),
            )
        if not query.index_supported:
            response, current_account = await self._provider_fallback(query, account.mode)
            return self._decorate(query, response, current_account.tag_registry)

        try:
            projection = await self._projections.open(account)
        except MetadataProjectionError:
            if account.mode == "managed":
                raise
            logger.warning("Operational metadata index is unavailable; using bounded provider fallback")
            response, current_account = await self._provider_fallback(query, account.mode)
            return self._decorate(query, response, current_account.tag_registry)

        # Re-resolve lifecycle authority immediately before each provider access.
        access = self._open_provider(query, account.mode)
        current_query = self._resolve_tags_for_access(query, access)
        try:
            state = await _bounded_provider_call(access.provider.mailbox_state(current_query.mailbox))
        except Exception:
            logger.warning("Metadata index qualification failed; using bounded provider fallback")
            response, current_account = await self._provider_fallback(query, account.mode)
            return self._decorate(query, response, current_account.tag_registry)

        try:
            indexed = await projection.read_complete(query.mailbox, state)
        except MetadataProjectionError:
            if account.mode == "managed":
                raise
            logger.warning("Operational metadata index read failed; using bounded provider fallback")
            response, current_account = await self._provider_fallback(query, account.mode)
            return self._decorate(query, response, current_account.tag_registry)
        if indexed is not None:
            cached = _page_response(current_query, indexed, access.account.allowed_senders)
            refreshed = await self._refresh_cached_tags(current_query, cached, access.provider)
            return self._decorate(current_query, refreshed, access.account.tag_registry)

        access = self._open_provider(query, account.mode)
        current_query = self._resolve_tags_for_access(query, access)
        loaded = await self._load_mailbox_snapshot(current_query, account, projection, access, state)
        if loaded is None:
            response, current_account = await self._provider_fallback(query, account.mode)
            return self._decorate(query, response, current_account.tag_registry)
        snapshot, incremental, refresh_flags = loaded

        _validate_snapshot(current_query, snapshot)
        projection_write_failed = False
        try:
            await projection.write_snapshot(query.mailbox, snapshot)
        except Exception:
            # The validated provider observation already satisfies mail-read
            # authority. Rebuildable projection persistence cannot erase it.
            projection_write_failed = True
            logger.warning("Metadata projection write failed; returning bounded provider observation")
        if snapshot.complete:
            observed = _page_response(
                current_query,
                [dict(email) for email in snapshot.emails],
                access.account.allowed_senders,
                warnings=("projection_write_failed",) if projection_write_failed else (),
            )
            if incremental or refresh_flags:
                # Older projected rows can carry stale keywords. Refresh only
                # the returned page, matching a later complete-cache hit.
                observed = await self._refresh_cached_tags(current_query, observed, access.provider)
            return self._decorate(current_query, observed, access.account.tag_registry)
        response, current_account = await self._provider_fallback(query, account.mode)
        response = _with_projection_warning(query, response) if projection_write_failed else response
        return self._decorate(query, response, current_account.tag_registry)
