"""copy_emails_to_account service, IMAP session, adapter, and MCP tests (spec 07 Copy Between Accounts)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, call, patch

import aioimaplib
import pytest

from mcp_email_server import app as app_module
from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application import mutations as mutations_module
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.mutations import (
    BatchMutationOutcome,
    CopyAppendEvidence,
    CopyAppendNotStartedError,
    CopySourceInventory,
    CopySourceMessage,
    CopyToAccountCommand,
    MutationAccountSnapshot,
    MutationProviderAccess,
    MutationProviderError,
    MutationServices,
    TargetMutationOutcome,
)
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import EmailClient
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
        raw={"4": b"From: a\r\n\r\nnul\x00byte", "5": ValueError("read failed")},
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


# --- IMAP sessions (emails/classic.py) -------------------------------------------------------


def _copy_imap(*, capabilities=("IMAP4REV1",), fetch=None, append=("OK", [b"APPEND completed"])):
    mock = AsyncMock()
    mock._client_task = asyncio.Future()
    mock._client_task.set_result(None)
    mock.wait_hello_from_server = AsyncMock()
    mock.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
    mock.logout = AsyncMock()
    mock.protocol = MagicMock(capabilities=set(capabilities))
    mock.protocol.capability = AsyncMock()
    mock.protocol.append = (
        AsyncMock(return_value=append) if not isinstance(append, BaseException) else AsyncMock(side_effect=append)
    )
    mock.examine = AsyncMock(return_value=("OK", [b"EXAMINE completed"]))
    mock.select = AsyncMock()
    mock.enable = AsyncMock(return_value=("OK", [b"ENABLED UTF8=ACCEPT", b"ENABLE completed"]))
    if fetch is not None:
        mock.uid = AsyncMock(side_effect=fetch)
    return mock


@pytest.fixture
def client():
    server = EmailServer(user_name="u", password="p", host="imap.example.com", port=993, use_ssl=True)
    return EmailClient(server, sender="Test <test@example.com>")


def _inventory_response(*lines: bytes):
    return "OK", [*lines, b"FETCH completed"]


def _body_response(uid: str, raw: bytes):
    return "OK", [f"1 FETCH (UID {uid} BODY[] {{{len(raw)}}}".encode(), bytearray(raw), b")", b"FETCH completed"]


@pytest.mark.asyncio
async def test_source_session_examines_and_reads_with_peek(client):
    calls = []

    async def uid(command, *args):
        calls.append((command, *args))
        if args[-1] == "(UID RFC822.SIZE FLAGS INTERNALDATE)":
            return _inventory_response(
                b'1 FETCH (UID 7 RFC822.SIZE 42 FLAGS (\\Seen $Label) INTERNALDATE "17-Jul-1996 02:44:25 -0700")',
                b'2 FETCH (UID 99 RFC822.SIZE 1 FLAGS () INTERNALDATE "01-Jan-2000 00:00:00 +0000")',
                b'3 FETCH (UID 8 RFC822.SIZE 5 FLAGS () INTERNALDATE "bogus")',
            )
        return _body_response(args[0], RAW)

    mock = _copy_imap(fetch=uid)
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_source("Účty") as source:
            inventory = await source.inventory(("7", "8", "9"), ())
            raw = await source.read("7")

    mock.examine.assert_awaited_once_with('"&ANoBDQ-ty"')
    mock.select.assert_not_awaited()
    assert inventory.blocked == frozenset()
    assert inventory.messages == {
        "7": CopySourceMessage("7", 42, (r"\Seen", "$Label"), DATE),
        "8": CopySourceMessage("8", 5, (), None),
    }
    assert raw == RAW
    assert calls == [
        ("fetch", "7,8,9", "(UID RFC822.SIZE FLAGS INTERNALDATE)"),
        ("fetch", "7", "BODY.PEEK[]"),
    ]
    assert all("STORE" not in str(call).upper() for call in calls)
    mock.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_examine_enters_selected_state_for_uid_commands(client):
    # aioimaplib only tracks SELECT; UID FETCH is refused in AUTH state after EXAMINE.
    mock = _copy_imap()
    mock.protocol.state = aioimaplib.AUTH
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_source("INBOX"):
            assert mock.protocol.state == aioimaplib.SELECTED


@pytest.mark.asyncio
async def test_source_inventory_skips_blocked_uids(client):
    mock = _copy_imap(fetch=AsyncMock(return_value=_inventory_response()))
    with (
        patch.object(client, "imap_class", return_value=mock),
        patch.object(client, "_blocked_uids", AsyncMock(return_value={"2"})) as blocked,
    ):
        async with client.open_copy_source("INBOX") as source:
            inventory = await source.inventory(("1", "2"), ("ok@example.test",))
    blocked.assert_awaited_once_with(mock, ["1", "2"], ["ok@example.test"])
    assert inventory.blocked == frozenset({"2"})
    mock.uid.assert_awaited_once_with("fetch", "1", "(UID RFC822.SIZE FLAGS INTERNALDATE)")


@pytest.mark.asyncio
async def test_source_inventory_all_blocked_skips_fetch(client):
    mock = _copy_imap(fetch=AsyncMock())
    with (
        patch.object(client, "imap_class", return_value=mock),
        patch.object(client, "_blocked_uids", AsyncMock(return_value={"1"})),
    ):
        async with client.open_copy_source("INBOX") as source:
            inventory = await source.inventory(("1",), ("ok@example.test",))
    assert inventory.messages == {}
    mock.uid.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_examine_failure_raises_and_logs_out(client):
    mock = _copy_imap()
    mock.examine.return_value = ("NO", [b"no such mailbox"])
    with patch.object(client, "imap_class", return_value=mock), pytest.raises(RuntimeError, match="EXAMINE"):
        async with client.open_copy_source("Missing"):
            pass
    mock.logout.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [("NO", [b"FETCH failed"]), ("OK", [b"FETCH completed"])],
    ids=["rejected", "no-literal"],
)
async def test_source_read_without_message_raises(client, response):
    mock = _copy_imap(fetch=AsyncMock(return_value=response))
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_source("INBOX") as source:
            with pytest.raises(ValueError, match="could not be read"):
                await source.read("5")
    mock.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_interrupted_read_aborts_connection(client):
    mock = _copy_imap(fetch=AsyncMock(side_effect=asyncio.CancelledError()))
    with (
        patch.object(client, "imap_class", return_value=mock),
        patch.object(EmailClient, "_abort_imap_connection", AsyncMock()) as abort,
    ):
        async with client.open_copy_source("INBOX") as source:
            with pytest.raises(asyncio.CancelledError):
                await source.read("5")
    abort.assert_awaited_once_with(mock)
    mock.logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_session_appends_verbatim_bytes_with_date_and_flags(client):
    mock = _copy_imap(append=("OK", [b"[APPENDUID 1 5] APPEND completed"]))
    guard = MagicMock()
    client.mutation_guard = guard
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_target("techem") as target:
            evidence = await target.append(RAW, flags=(r"\Seen", r"\Flagged"), internal_date=DATE)
            empty = await target.append(RAW, flags=(), internal_date=None)
    assert evidence == CopyAppendEvidence("succeeded")
    assert empty == CopyAppendEvidence("succeeded")
    assert mock.protocol.append.await_args_list == [
        call(RAW, '"techem"', r"(\Seen \Flagged)", f'"{DATE}"', timeout=APPLICATION_LIMITS.provider_timeout_seconds),
        call(RAW, '"techem"', "()", None, timeout=APPLICATION_LIMITS.provider_timeout_seconds),
    ]
    assert guard.call_count == 2
    mock.select.assert_not_awaited()
    mock.enable.assert_not_awaited()
    mock.logout.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (("NO", [b"[TRYCREATE] Mailbox does not exist"]), CopyAppendEvidence("failed", "destination-missing")),
        (("NO", [b"[OVERQUOTA] quota exceeded"]), CopyAppendEvidence("failed", "append-rejected")),
        (("BAD", [b"syntax"]), CopyAppendEvidence("failed", "append-rejected")),
        (("BYE", [b"gone"]), CopyAppendEvidence("unknown", "copy-unknown")),
    ],
)
async def test_target_append_classification(client, response, expected):
    mock = _copy_imap(append=response)
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_target("techem") as target:
            assert await target.append(RAW, flags=(), internal_date=None) == expected


@pytest.mark.asyncio
async def test_target_guard_refusal_prevents_append(client):
    mock = _copy_imap()
    client.mutation_guard = MagicMock(side_effect=PermissionError("revoked"))
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_target("techem") as target:
            with pytest.raises(PermissionError):
                await target.append(RAW, flags=(), internal_date=None)
    mock.protocol.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_transport_failure_aborts_connection(client):
    mock = _copy_imap(append=ConnectionError("reset"))
    with (
        patch.object(client, "imap_class", return_value=mock),
        patch.object(EmailClient, "_abort_imap_connection", AsyncMock()) as abort,
    ):
        async with client.open_copy_target("techem") as target:
            with pytest.raises(ConnectionError):
                await target.append(RAW, flags=(), internal_date=None)
    abort.assert_awaited_once_with(mock)
    mock.logout.assert_not_awaited()


UTF8_RAW = "From: a@example.test\r\nSubject: Vyúčtování\r\n\r\nbody\r\n".encode()


@pytest.mark.asyncio
async def test_target_utf8_header_enables_utf8_and_uses_utf8_literal(client):
    mock = _copy_imap(capabilities=("IMAP4REV1", "ENABLE", "UTF8=ACCEPT"))
    with (
        patch.object(client, "imap_class", return_value=mock),
        patch("mcp_email_server.emails.classic._append_utf8_literal", AsyncMock(return_value=("OK", []))) as utf8,
    ):
        async with client.open_copy_target("Účty") as target:
            evidence = await target.append(UTF8_RAW, flags=(r"\Seen",), internal_date=DATE)
            await target.append(RAW, flags=(), internal_date=None)
    assert evidence == CopyAppendEvidence("succeeded")
    mock.enable.assert_awaited_once_with("UTF8=ACCEPT")
    utf8.assert_awaited_once_with(
        mock,
        UTF8_RAW,
        mailbox='"Účty"',
        flags=r"(\Seen)",
        date=f'"{DATE}"',
        timeout=APPLICATION_LIMITS.provider_timeout_seconds,
    )
    # A later ASCII message in the UTF-8 session still uses the quoted UTF-8 mailbox name.
    assert mock.protocol.append.await_args.args[1] == '"Účty"'


@pytest.mark.asyncio
async def test_target_utf8_header_without_support_fails_before_append(client):
    mock = _copy_imap()
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_target("techem") as target:
            first = await target.append(UTF8_RAW, flags=(), internal_date=None)
            second = await target.append(UTF8_RAW, flags=(), internal_date=None)
    assert first == second == CopyAppendEvidence("failed", "utf8-append-unsupported")
    mock.protocol.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_utf8_only_server_enables_at_open(client):
    mock = _copy_imap(capabilities=("IMAP4REV1", "ENABLE", "UTF8=ONLY"))
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_target("techem"):
            pass
    mock.enable.assert_awaited_once_with("UTF8=ACCEPT")


# --- Adapter (adapters/mutations.py) ----------------------------------------------------------


def _adapter(fresh: MutationAccountSnapshot, *, source_session=None, target_session=None, fail_open=None):
    handler = MagicMock()

    @asynccontextmanager
    async def opened(session, mailbox):
        handler.opened_mailboxes.append(mailbox)
        if fail_open is not None:
            raise fail_open
        yield session

    handler.opened_mailboxes = []
    handler.incoming_client.open_copy_source = lambda mailbox: opened(source_session, mailbox)
    handler.incoming_client.open_copy_target = lambda mailbox: opened(target_session, mailbox)
    return ClassicMutationProvider(handler, lambda: fresh), handler


@pytest.mark.asyncio
async def test_adapter_target_installs_append_guard():
    account = _account(account_name="dst", allowed_mutations=("append",))
    target = FakeTarget()
    provider, handler = _adapter(account, target_session=target)
    async with provider.open_copy_target(_command("1"), account) as session:
        assert session is target
    assert handler.opened_mailboxes == ["techem"]
    handler.incoming_client.mutation_guard()


@pytest.mark.asyncio
async def test_adapter_target_refuses_without_fresh_append_before_connecting():
    account = _account(account_name="dst", allowed_mutations=("append",))
    provider, handler = _adapter(replace(account, allowed_mutations=()), target_session=FakeTarget())
    with pytest.raises(PermissionError):
        async with provider.open_copy_target(_command("1"), account):
            pass
    assert handler.opened_mailboxes == []


@pytest.mark.asyncio
async def test_adapter_source_opens_requested_mailbox_without_guard():
    account = _account(account_name="src", allowed_mutations=())
    source = FakeSource({"1": _message("1")})
    provider, handler = _adapter(account, source_session=source)
    async with provider.open_copy_source(_command("1", source_mailbox="2025"), account) as session:
        inventory = await session.inventory(("1",), ())
        assert await session.read("1") == RAW
    assert handler.opened_mailboxes == ["2025"]
    assert inventory.messages == {"1": _message("1")}


@pytest.mark.asyncio
async def test_adapter_sanitizes_provider_failures():
    account = _account(account_name="src", allowed_mutations=())
    provider, _ = _adapter(account, fail_open=ConnectionError("imap.secret.example refused"))
    with pytest.raises(MutationProviderError, match="provider_failure") as raised:
        async with provider.open_copy_source(_command("1"), account):
            pass
    assert "secret" not in str(raised.value)

    class BrokenSource(FakeSource):
        async def inventory(self, email_ids, allowed_senders):
            raise RuntimeError("server said secret things")

    provider, _ = _adapter(account, source_session=BrokenSource({}))
    async with provider.open_copy_source(_command("1"), account) as session:
        with pytest.raises(MutationProviderError, match="provider_failure"):
            await session.inventory(("1",), ())


# --- MCP tool (app.py) -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_is_advertised_as_additive_nondestructive_mutation():
    tools = {tool.name: tool for tool in await app_module.mcp.list_tools()}
    tool = tools["copy_emails_to_account"]
    assert tool.inputSchema["required"] == ["source_account", "email_ids", "target_account", "target_mailbox"]
    properties = tool.inputSchema["properties"]
    assert properties["source_mailbox"]["default"] == "INBOX"
    assert properties["email_ids"]["maxItems"] == APPLICATION_LIMITS.mutation_uids
    for name in ("source_account", "target_account"):
        assert properties[name]["maxLength"] == APPLICATION_LIMITS.account_name_bytes
    for name in ("source_mailbox", "target_mailbox"):
        assert properties[name]["maxLength"] == APPLICATION_LIMITS.mailbox_bytes
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is False
    assert tool.annotations.openWorldHint is True


@pytest.mark.asyncio
async def test_tool_reports_full_success():
    handler = AsyncMock(return_value=BatchMutationOutcome((TargetMutationOutcome("7", "succeeded"),)))
    with patch("mcp_email_server.app.copy_emails_to_account_command", handler):
        result = await app_module.copy_emails_to_account(
            source_account="Seznam", email_ids=["7"], target_account="Home", target_mailbox="techem"
        )
    assert result == "Successfully copied 1 email(s) to techem in Home"
    assert handler.await_args.args[0] == CopyToAccountCommand("Seznam", ("7",), "Home", "techem", "INBOX")


@pytest.mark.asyncio
async def test_tool_reports_failed_and_unknown_details_in_order():
    handler = AsyncMock(
        return_value=BatchMutationOutcome((
            TargetMutationOutcome("1", "succeeded"),
            TargetMutationOutcome("2", "failed", "too-large"),
            TargetMutationOutcome("3", "unknown", "copy-unknown"),
            TargetMutationOutcome("4", "failed", "not-attempted"),
        ))
    )
    with patch("mcp_email_server.app.copy_emails_to_account_command", handler):
        result = await app_module.copy_emails_to_account(
            source_account="Gmail",
            email_ids=["1", "2", "3", "4"],
            target_account="Home",
            target_mailbox="techem",
            source_mailbox="[Gmail]/All Mail",
        )
    assert result == (
        "Copy result [succeeded: 1; failed: 2 (too-large); unknown: 3 (copy-unknown); "
        "failed: 4 (not-attempted); warning: reconciliation needed]"
    )
    assert handler.await_args.args[0].source_mailbox == "[Gmail]/All Mail"


@pytest.mark.asyncio
async def test_tool_hides_unlisted_detail_text():
    handler = AsyncMock(
        return_value=BatchMutationOutcome((TargetMutationOutcome("1", "failed", "server said something"),))
    )
    with patch("mcp_email_server.app.copy_emails_to_account_command", handler):
        result = await app_module.copy_emails_to_account(
            source_account="a", email_ids=["1"], target_account="b", target_mailbox="x"
        )
    assert result == "Copy result [failed: 1]"


# --- Review regressions -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_source_read_failure_stops_batch():
    source = FakeSource({"1": _message("1"), "2": _message("2")}, raw={"1": RuntimeError("CommandTimeout")})
    target = FakeTarget()
    services, *_ = _copy_services(source, target)
    outcome = await services.copy_to_account.execute(_command("1", "2"))
    assert _summary(outcome) == [("1", "failed", "source-read-failed"), ("2", "failed", "not-attempted")]
    assert source.reads == ["1"]


@pytest.mark.asyncio
async def test_failure_before_append_bytes_is_not_attempted_not_unknown():
    source = FakeSource({"1": _message("1"), "2": _message("2")})
    target = FakeTarget([CopyAppendNotStartedError("enable failed")])
    services, *_ = _copy_services(source, target)
    outcome = await services.copy_to_account.execute(_command("1", "2"))
    assert _summary(outcome) == [("1", "failed", "not-attempted"), ("2", "failed", "not-attempted")]
    assert outcome.reconciliation_needed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lines",
    [
        [b"1 FETCH (UID 8 BODY[] {%d}" % len(RAW), bytearray(RAW), b")"],
        [b"1 FETCH (BODY[] {%d}" % len(RAW), bytearray(RAW), b" UID 8)"],
    ],
    ids=["uid-before", "uid-after"],
)
async def test_source_read_rejects_literal_for_another_uid(client, lines):
    mock = _copy_imap(fetch=AsyncMock(return_value=("OK", [*lines, b"FETCH completed"])))
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_source("INBOX") as source:
            with pytest.raises(ValueError, match="could not be read"):
                await source.read("5")


@pytest.mark.asyncio
async def test_source_read_accepts_uid_after_literal(client):
    lines = [b"1 FETCH (BODY[] {%d}" % len(RAW), bytearray(RAW), b" UID 5)", b"FETCH completed"]
    mock = _copy_imap(fetch=AsyncMock(return_value=("OK", lines)))
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_source("INBOX") as source:
            assert await source.read("5") == RAW


@pytest.mark.asyncio
async def test_source_read_on_broken_session_refuses(client):
    mock = _copy_imap(fetch=AsyncMock(side_effect=[ConnectionError("reset"), _body_response("6", RAW)]))
    with (
        patch.object(client, "imap_class", return_value=mock),
        patch.object(EmailClient, "_abort_imap_connection", AsyncMock()),
    ):
        async with client.open_copy_source("INBOX") as source:
            with pytest.raises(ConnectionError):
                await source.read("5")
            with pytest.raises(RuntimeError, match="broken"):
                await source.read("6")
    assert mock.uid.await_count == 1


@pytest.mark.asyncio
async def test_unsolicited_flag_fetch_does_not_replace_inventory(client):
    response = _inventory_response(
        b'1 FETCH (UID 7 RFC822.SIZE 42 FLAGS (\\Seen) INTERNALDATE "17-Jul-1996 02:44:25 -0700")',
        b"1 FETCH (UID 7 FLAGS (\\Seen \\Flagged))",
    )
    mock = _copy_imap(fetch=AsyncMock(return_value=response))
    with patch.object(client, "imap_class", return_value=mock):
        async with client.open_copy_source("INBOX") as source:
            inventory = await source.inventory(("7",), ())
    assert inventory.messages["7"] == CopySourceMessage("7", 42, (r"\Seen",), DATE)


@pytest.mark.asyncio
async def test_target_enable_transport_failure_is_not_started(client):
    mock = _copy_imap(capabilities=("IMAP4REV1", "ENABLE", "UTF8=ACCEPT"))
    mock.enable.side_effect = ConnectionError("reset")
    with (
        patch.object(client, "imap_class", return_value=mock),
        patch.object(EmailClient, "_abort_imap_connection", AsyncMock()),
    ):
        async with client.open_copy_target("techem") as target:
            with pytest.raises(CopyAppendNotStartedError):
                await target.append(UTF8_RAW, flags=(), internal_date=None)
    mock.protocol.append.assert_not_awaited()
