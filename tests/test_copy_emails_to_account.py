"""copy_emails_to_account service, IMAP session, adapter, and MCP tests (spec 07 Copy Between Accounts)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_email_server.application import mutations as mutations_module
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.mutations import (
    CopyAppendEvidence,
    CopySourceInventory,
    CopySourceMessage,
    CopyToAccountCommand,
    MutationAccountSnapshot,
    MutationProviderAccess,
    MutationProviderError,
    MutationServices,
    TargetMutationOutcome,
)
from tests.test_mutation_application import _account

RAW = b"From: a@example.test\r\nSubject: s\r\n\r\nbody\r\n"
DATE = "17-Jul-1996 02:44:25 -0700"


def _message(uid: str, *, size: int = len(RAW), flags: tuple[str, ...] = (), date: str | None = DATE):
    return CopySourceMessage(uid=uid, size=size, flags=flags, internal_date=date)


class FakeSource:
    def __init__(
        self,
        messages: dict[str, CopySourceMessage],
        raw: dict[str, bytes | BaseException] | None = None,
        blocked: frozenset[str] = frozenset(),
    ) -> None:
        self.messages = messages
        self.raw = raw or {}
        self.blocked = blocked
        self.inventory_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.reads: list[str] = []

    async def inventory(self, email_ids: tuple[str, ...], allowed_senders: tuple[str, ...]) -> CopySourceInventory:
        self.inventory_calls.append((email_ids, allowed_senders))
        return CopySourceInventory(
            messages={uid: self.messages[uid] for uid in email_ids if uid in self.messages and uid not in self.blocked},
            blocked=frozenset(uid for uid in email_ids if uid in self.blocked),
        )

    async def read(self, email_id: str) -> bytes:
        self.reads.append(email_id)
        value = self.raw.get(email_id, RAW)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeTarget:
    def __init__(self, results: list[CopyAppendEvidence | BaseException] | None = None) -> None:
        self.results = list(results or [])
        self.appends: list[tuple[bytes, tuple[str, ...], str | None]] = []

    async def append(self, message: bytes, *, flags: tuple[str, ...], internal_date: str | None) -> CopyAppendEvidence:
        self.appends.append((message, flags, internal_date))
        result = self.results.pop(0) if self.results else CopyAppendEvidence("succeeded")
        if isinstance(result, BaseException):
            raise result
        return result


def _session(session: object, events: list[str], name: str):
    @asynccontextmanager
    async def manager(*_args: object) -> AsyncIterator[object]:
        events.append(f"{name}-open")
        try:
            yield session
        finally:
            events.append(f"{name}-close")

    return MagicMock(side_effect=manager)


def _copy_services(
    source: FakeSource,
    target: FakeTarget,
    *,
    source_account: MutationAccountSnapshot | None = None,
    target_account: MutationAccountSnapshot | None = None,
):
    accounts = {
        "src": source_account or _account(account_name="src", allowed_mutations=()),
        "dst": target_account or _account(account_name="dst", allowed_mutations=("append",)),
    }
    events: list[str] = []
    authority = MagicMock()
    authority.resolve.side_effect = lambda name, **_: accounts[name]
    source_provider = MagicMock()
    source_provider.open_copy_source = _session(source, events, "source")
    target_provider = MagicMock()
    target_provider.open_copy_target = _session(target, events, "target")
    providers = {"src": source_provider, "dst": target_provider}
    factory = MagicMock()
    factory.open.side_effect = lambda name, **_: MutationProviderAccess(accounts[name], providers[name])
    projection = MagicMock()
    projection.invalidate = AsyncMock()
    projections = MagicMock()
    projections.open = AsyncMock(return_value=projection)
    services = MutationServices.compose(authority, factory, projections)
    return services, accounts, factory, projections, projection, events


def _command(*uids: str, **changes: object) -> CopyToAccountCommand:
    return replace(CopyToAccountCommand("src", uids or ("1",), "dst", "techem"), **changes)


def _summary(outcome) -> list[tuple[str, str, str | None]]:
    return [(item.target, item.status, item.detail) for item in outcome.outcomes]


@pytest.mark.asyncio
async def test_copies_verbatim_bytes_with_internaldate_and_system_flags_only():
    flags = (r"\seen", r"\Answered", r"\Flagged", r"\Draft", r"\Deleted", r"\Recent", "$Label1", "Work")
    source = FakeSource({"7": _message("7", flags=flags)}, raw={"7": RAW})
    target = FakeTarget()
    services, _, factory, projections, projection, events = _copy_services(source, target)

    outcome = await services.copy_to_account.execute(_command("7"))

    assert _summary(outcome) == [("7", "succeeded", None)]
    assert outcome.reconciliation_needed is False
    assert target.appends == [(RAW, (r"\Seen", r"\Answered", r"\Flagged", r"\Draft"), DATE)]
    assert source.inventory_calls == [(("7",), ())]
    assert [call.kwargs["purpose"] for call in factory.open.call_args_list] == ["incoming", "incoming"]
    assert events == ["source-open", "target-open", "target-close", "source-close"]
    projections.open.assert_awaited_once()
    assert projections.open.await_args.args[0].account_name == "dst"
    projection.invalidate.assert_awaited_once_with(("techem",))


@pytest.mark.asyncio
@pytest.mark.parametrize("target_name", ["src", "src "])
async def test_same_account_rejected_before_provider_access(target_name):
    services, _, factory, *_ = _copy_services(FakeSource({}), FakeTarget())
    command = _command("1", target_account=target_name)
    if target_name == "src":
        with pytest.raises(ValueError, match="must differ"):
            await services.copy_to_account.execute(command)
        factory.open.assert_not_called()
    else:
        # Account names compare exactly; a different name is a different account.
        command.validate()


@pytest.mark.asyncio
async def test_target_without_append_denied_before_provider_access():
    services, _, factory, *_ = _copy_services(
        FakeSource({}), FakeTarget(), target_account=_account(account_name="dst", allowed_mutations=("organize",))
    )
    with pytest.raises(PermissionError):
        await services.copy_to_account.execute(_command("1"))
    factory.open.assert_not_called()


@pytest.mark.asyncio
async def test_source_needs_no_mutation_grant():
    source = FakeSource({"1": _message("1")})
    services, *_ = _copy_services(
        source, FakeTarget(), source_account=_account(account_name="src", allowed_mutations=())
    )
    outcome = await services.copy_to_account.execute(_command("1"))
    assert _summary(outcome) == [("1", "succeeded", None)]


@pytest.mark.asyncio
async def test_target_append_revoked_at_open_denies_before_session():
    services, accounts, factory, *_ = _copy_services(FakeSource({"1": _message("1")}), FakeTarget())
    revoked = replace(accounts["dst"], allowed_mutations=())
    providers = {name: factory.open.side_effect(name).provider for name in ("src", "dst")}
    factory.open.side_effect = lambda name, **_: MutationProviderAccess(
        revoked if name == "dst" else accounts[name], providers[name]
    )
    with pytest.raises(PermissionError):
        await services.copy_to_account.execute(_command("1"))
    providers["src"].open_copy_source.assert_not_called()
    providers["dst"].open_copy_target.assert_not_called()


@pytest.mark.asyncio
async def test_per_uid_classification_preserves_caller_order():
    limit = APPLICATION_LIMITS.total_attachment_bytes
    source = FakeSource(
        {
            "1": _message("1"),
            "3": _message("3", size=limit + 1),
            "4": _message("4"),
            "5": _message("5"),
            "6": _message("6"),
            "7": _message("7"),
            "8": _message("8"),
        },
        raw={"4": b"From: a\r\n\r\nnul\x00byte", "5": MutationProviderError("read failed")},
    )
    target = FakeTarget([
        CopyAppendEvidence("failed", "append-rejected"),
        CopyAppendEvidence("failed", "utf8-append-unsupported"),
        CopyAppendEvidence("succeeded"),
    ])
    services, _, _, _, projection, _ = _copy_services(source, target)

    outcome = await services.copy_to_account.execute(_command("8", "2", "3", "4", "5", "6", "7", "1"))

    assert _summary(outcome) == [
        ("8", "failed", "append-rejected"),
        ("2", "failed", "not-found"),
        ("3", "failed", "too-large"),
        ("4", "failed", "binary-unsupported"),
        ("5", "failed", "source-read-failed"),
        ("6", "failed", "utf8-append-unsupported"),
        ("7", "succeeded", None),
        ("1", "succeeded", None),
    ]
    # Classified UIDs are never read; NUL and failed reads never reach APPEND.
    assert source.reads == ["8", "4", "5", "6", "7", "1"]
    assert len(target.appends) == 4
    projection.invalidate.assert_awaited_once_with(("techem",))


@pytest.mark.asyncio
async def test_read_larger_than_limit_is_too_large_without_append():
    limit = APPLICATION_LIMITS.total_attachment_bytes
    source = FakeSource({"1": _message("1", size=10)}, raw={"1": b"x" * (limit + 1)})
    target = FakeTarget()
    services, *_ = _copy_services(source, target)
    outcome = await services.copy_to_account.execute(_command("1"))
    assert _summary(outcome) == [("1", "failed", "too-large")]
    assert target.appends == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("report", "expected"), [(False, ("succeeded", None)), (True, ("failed", "sender-policy"))])
async def test_sender_policy_parity(report, expected):
    source = FakeSource({"1": _message("1"), "2": _message("2")}, blocked=frozenset({"2"}))
    target = FakeTarget()
    source_account = _account(
        account_name="src", allowed_mutations=(), allowed_senders=("ok@example.test",), report_blocked_mutations=report
    )
    services, *_ = _copy_services(source, target, source_account=source_account)

    outcome = await services.copy_to_account.execute(_command("1", "2"))

    assert _summary(outcome) == [("1", "succeeded", None), ("2", *expected)]
    assert source.inventory_calls == [(("1", "2"), ("ok@example.test",))]
    assert source.reads == ["1"]
    assert len(target.appends) == 1


@pytest.mark.asyncio
async def test_nothing_placed_skips_target_session_and_invalidation():
    source = FakeSource({})
    services, _, _, projections, _, events = _copy_services(source, FakeTarget())
    outcome = await services.copy_to_account.execute(_command("1", "2"))
    assert _summary(outcome) == [("1", "failed", "not-found"), ("2", "failed", "not-found")]
    assert events == ["source-open", "source-close"]
    projections.open.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_sender_policy_change_before_append_is_mutation_policy():
    source = FakeSource({"1": _message("1"), "2": _message("2")})
    target = FakeTarget()
    services, accounts, *_ = _copy_services(source, target)
    original = accounts["src"]
    calls = {"count": 0}

    def resolve(name, **_):
        if name == "src":
            calls["count"] += 1
            if calls["count"] > 1:
                return replace(original, allowed_senders=("new@example.test",))
        return accounts[name]

    services.copy_to_account._accounts.resolve.side_effect = resolve
    outcome = await services.copy_to_account.execute(_command("1", "2"))
    assert _summary(outcome) == [("1", "failed", "mutation-policy"), ("2", "failed", "mutation-policy")]
    assert target.appends == []


@pytest.mark.asyncio
async def test_source_authority_unavailable_before_append_is_mutation_policy():
    source = FakeSource({"1": _message("1")})
    target = FakeTarget()
    services, accounts, *_ = _copy_services(source, target)
    calls = {"count": 0}

    def resolve(name, **_):
        if name == "src":
            calls["count"] += 1
            if calls["count"] > 1:
                raise ValueError("account removed")
        return accounts[name]

    services.copy_to_account._accounts.resolve.side_effect = resolve
    outcome = await services.copy_to_account.execute(_command("1"))
    assert _summary(outcome) == [("1", "failed", "mutation-policy")]
    assert target.appends == []


@pytest.mark.asyncio
async def test_target_guard_revocation_is_mutation_policy_and_continues():
    source = FakeSource({"1": _message("1"), "2": _message("2")})
    target = FakeTarget([PermissionError("revoked"), CopyAppendEvidence("succeeded")])
    services, *_ = _copy_services(source, target)
    outcome = await services.copy_to_account.execute(_command("1", "2"))
    assert _summary(outcome) == [("1", "failed", "mutation-policy"), ("2", "succeeded", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), MutationProviderError("transport"), CopyAppendEvidence("unknown", "copy-unknown")],
    ids=["timeout", "transport", "unknown-evidence"],
)
async def test_ambiguous_append_is_unknown_without_replay(failure):
    source = FakeSource({"1": _message("1"), "2": _message("2"), "3": _message("3")})
    target = FakeTarget([CopyAppendEvidence("succeeded"), failure])
    services, _, _, _, projection, _ = _copy_services(source, target)

    outcome = await services.copy_to_account.execute(_command("1", "2", "3"))

    assert _summary(outcome) == [
        ("1", "succeeded", None),
        ("2", "unknown", "copy-unknown"),
        ("3", "failed", "not-attempted"),
    ]
    assert outcome.reconciliation_needed is True
    assert len(target.appends) == 2
    assert source.reads == ["1", "2"]
    projection.invalidate.assert_awaited_once_with(("techem",))


@pytest.mark.asyncio
async def test_append_deadline_reports_unknown(monkeypatch):
    monkeypatch.setattr(
        mutations_module, "APPLICATION_LIMITS", replace(APPLICATION_LIMITS, provider_timeout_seconds=0.01)
    )

    class SlowTarget(FakeTarget):
        async def append(self, message, *, flags, internal_date):
            self.appends.append((message, flags, internal_date))
            await asyncio.sleep(1)
            return CopyAppendEvidence("succeeded")

    source = FakeSource({"1": _message("1"), "2": _message("2")})
    target = SlowTarget()
    services, *_ = _copy_services(source, target)
    outcome = await services.copy_to_account.execute(_command("1", "2"))
    assert _summary(outcome) == [("1", "unknown", "copy-unknown"), ("2", "failed", "not-attempted")]


@pytest.mark.asyncio
async def test_destination_missing_stops_without_reading_more():
    source = FakeSource({"1": _message("1"), "2": _message("2")})
    target = FakeTarget([CopyAppendEvidence("failed", "destination-missing")])
    services, _, _, projections, _, _ = _copy_services(source, target)
    outcome = await services.copy_to_account.execute(_command("1", "2"))
    assert _summary(outcome) == [("1", "failed", "destination-missing"), ("2", "failed", "not-attempted")]
    assert source.reads == ["1"]
    projections.open.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_read_timeout_stops_remaining():
    source = FakeSource({"1": _message("1"), "2": _message("2")}, raw={"1": TimeoutError()})
    target = FakeTarget()
    services, *_ = _copy_services(source, target)
    outcome = await services.copy_to_account.execute(_command("1", "2"))
    assert _summary(outcome) == [("1", "failed", "source-read-failed"), ("2", "failed", "not-attempted")]
    assert outcome.reconciliation_needed is False
    assert target.appends == []


@pytest.mark.asyncio
async def test_projection_failure_requests_reconciliation():
    source = FakeSource({"1": _message("1")})
    services, _, _, _, projection, _ = _copy_services(source, FakeTarget())
    projection.invalidate.side_effect = RuntimeError("db")
    outcome = await services.copy_to_account.execute(_command("1"))
    assert _summary(outcome) == [("1", "succeeded", None)]
    assert outcome.reconciliation_needed is True


@pytest.mark.parametrize(
    "changes",
    [
        {"email_ids": ()},
        {"email_ids": ("1", "1")},
        {"email_ids": ("0",)},
        {"target_mailbox": ""},
        {"source_mailbox": "bad\x00"},
        {"source_account": ""},
    ],
)
def test_command_validation(changes):
    with pytest.raises(ValueError):
        _command("1", **changes).validate()


@pytest.mark.asyncio
async def test_results_never_contain_message_bytes():
    secret = b"From: a@example.test\r\nSubject: TOPSECRET\r\n\r\nTOPSECRET body\r\n"
    source = FakeSource({"1": _message("1")}, raw={"1": secret})
    services, *_ = _copy_services(source, FakeTarget())
    outcome = await services.copy_to_account.execute(_command("1"))
    assert "TOPSECRET" not in repr(outcome)
    assert isinstance(outcome.outcomes[0], TargetMutationOutcome)
