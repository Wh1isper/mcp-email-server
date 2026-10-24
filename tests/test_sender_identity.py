from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    MutationProviderAccess,
    SaveDraftCommand,
    SendCommand,
    SenderIdentityDeniedError,
)
from mcp_email_server.config import Settings, from_address_allowed
from mcp_email_server.emails.classic import EmailClient
from tests.test_mutation_application import _account, _services
from tests.test_mutation_provider_outcomes import _imap

IDENTITIES = ("*@lukaselsner.de", "*@lukius.de")


@pytest.mark.parametrize(
    ("from_address", "patterns", "expected"),
    [
        ("lu@lukius.de", IDENTITIES, True),
        ("Lukas <LU@Lukius.DE>", IDENTITIES, True),
        ("lu@lukius.de", (), False),
        ("lu@lukius.de.evil", IDENTITIES, False),
        ("lu@sub.lukius.de", IDENTITIES, False),
        ("lu@lukius.de, other@lukius.de", IDENTITIES, False),
        ("", IDENTITIES, False),
        ("exact@example.test", ("exact@example.test",), True),
    ],
)
def test_from_address_allowlist_requires_one_matching_address(
    from_address: str, patterns: tuple[str, ...], expected: bool
) -> None:
    assert from_address_allowed(from_address, patterns) is expected


def test_env_configures_allowed_from_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_ALLOWED_FROM_ADDRESSES", "*@Lukius.de, lu@lukaselsner.de ,")
    assert Settings().allowed_from_addresses == ["*@lukius.de", "lu@lukaselsner.de"]


def _draft_services(**account_changes: object) -> tuple[object, MagicMock]:
    provider = MagicMock()
    provider.save_to_mailbox = AsyncMock(return_value=AppendMutationOutcome("succeeded", "draft-id", mailbox="Drafts"))
    provider.find_reply_identity = AsyncMock(return_value="lu@lukius.de")
    account = _account(**{"drafts_mailbox": "Drafts", "allowed_from_addresses": IDENTITIES, **account_changes})
    services, _, _, _ = _services(account=account, provider=provider)
    return services, provider


@pytest.mark.asyncio
async def test_draft_uses_allowed_explicit_from_without_lookup() -> None:
    services, provider = _draft_services()
    await services.save_draft.execute(
        SaveDraftCommand("primary", (), "subject", "body", in_reply_to="<a@b>", from_address="Lu <lu@lukaselsner.de>")
    )
    assert provider.save_to_mailbox.await_args.args[0].from_address == "Lu <lu@lukaselsner.de>"
    provider.find_reply_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_denies_explicit_from_outside_allowlist_before_provider_open() -> None:
    services, provider = _draft_services()
    with pytest.raises(SenderIdentityDeniedError):
        await services.save_draft.execute(
            SaveDraftCommand("primary", (), "subject", "body", from_address="someone@example.test")
        )
    provider.save_to_mailbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_draft_derives_from_replied_to_message() -> None:
    services, provider = _draft_services()
    await services.save_draft.execute(SaveDraftCommand("primary", (), "Re: x", "body", in_reply_to="<a@b>"))
    assert provider.find_reply_identity.await_args.args[0] == "<a@b>"
    assert provider.save_to_mailbox.await_args.args[0].from_address == "lu@lukius.de"


@pytest.mark.asyncio
async def test_reply_draft_keeps_account_from_when_nothing_matches() -> None:
    services, provider = _draft_services()
    provider.find_reply_identity.return_value = None
    await services.save_draft.execute(SaveDraftCommand("primary", (), "Re: x", "body", in_reply_to="<a@b>"))
    assert provider.save_to_mailbox.await_args.args[0].from_address is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_reply_to", "allowed"),
    [(None, IDENTITIES), ("<a@b>", ())],
    ids=["not-a-reply", "no-identities"],
)
async def test_no_lookup_without_reply_or_identities(in_reply_to: str | None, allowed: tuple[str, ...]) -> None:
    services, provider = _draft_services(allowed_from_addresses=allowed)
    await services.save_draft.execute(SaveDraftCommand("primary", (), "subject", "body", in_reply_to=in_reply_to))
    provider.find_reply_identity.assert_not_awaited()
    assert provider.save_to_mailbox.await_args.args[0].from_address is None


@pytest.mark.asyncio
async def test_draft_rechecks_identity_against_fresh_authority() -> None:
    provider = MagicMock()
    provider.save_to_mailbox = AsyncMock()
    provider.find_reply_identity = AsyncMock(return_value="lu@lukius.de")
    account = _account(drafts_mailbox="Drafts", allowed_from_addresses=IDENTITIES)
    services, _, factory, _ = _services(account=account, provider=provider)
    factory.open.side_effect = [
        MutationProviderAccess(account, provider),
        MutationProviderAccess(replace(account, allowed_from_addresses=()), provider),
    ]
    with pytest.raises(SenderIdentityDeniedError):
        await services.save_draft.execute(SaveDraftCommand("primary", (), "Re: x", "body", in_reply_to="<a@b>"))
    provider.save_to_mailbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_send_derives_from_through_incoming_lookup() -> None:
    provider = MagicMock()
    provider.find_reply_identity = AsyncMock(return_value="lu@lukius.de")
    provider.send = AsyncMock(side_effect=RuntimeError("stop after capture"))
    account = _account(allowed_from_addresses=IDENTITIES)
    services, _, factory, _ = _services(account=account, provider=provider)
    with pytest.raises(RuntimeError, match="stop after capture"):
        await services.send.execute(
            SendCommand("primary", ("recipient@example.test",), "Re: x", "body", in_reply_to="<a@b>")
        )
    purposes = [call.kwargs["purpose"] for call in factory.open.call_args_list]
    assert purposes == ["incoming", "outgoing"]
    assert provider.send.await_args.args[0].from_address == "lu@lukius.de"


def _client(email_server) -> EmailClient:
    return EmailClient(email_server, sender_name="Lukas Elsner", sender_address="mail@lukaselsner.de")


def test_compose_uses_per_message_from_with_account_name_fallback(email_server) -> None:
    client = _client(email_server)
    default = client.compose_message(["to@example.test"], "s", "b")
    derived = client.compose_message(["to@example.test"], "s", "b", from_address="lu@lukius.de")
    explicit = client.compose_message(["to@example.test"], "s", "b", from_address="Other <lu@lukius.de>")
    assert default["From"] == "Lukas Elsner <mail@lukaselsner.de>"
    assert derived["From"] == "Lukas Elsner <lu@lukius.de>"
    assert explicit["From"] == "Other <lu@lukius.de>"
    assert client.envelope_sender == "mail@lukaselsner.de"


def _headers_fetch(uid: str, headers: str) -> tuple[str, list[object]]:
    raw = headers.replace("\n", "\r\n").encode()
    return (
        "OK",
        [f"{uid} FETCH (UID {uid} BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(raw)}}}".encode(), bytearray(raw), b")"],
    )


def _reply_imap(search: dict[str, list[bytes]], fetches: dict[str, str]) -> AsyncMock:
    imap = _imap()
    imap.timeout = 10.0
    imap.list.return_value = (
        "OK",
        [b'(\\HasNoChildren) "/" "Archive"', b'(\\Noselect) "/" "Parent"', b'(\\HasNoChildren) "/" "INBOX"'],
    )
    selected: list[str] = []

    async def select(mailbox: str) -> tuple[str, list[bytes]]:
        selected.append(mailbox.strip('"'))
        return ("OK", [])

    async def uid_search(*criteria: str, charset: object = None) -> tuple[str, list[bytes]]:
        return ("OK", search.get(selected[-1], [b""]))

    async def uid(command: str, uid_value: str, item: str) -> tuple[str, list[object]]:
        return _headers_fetch(uid_value, fetches[uid_value])

    imap.select.side_effect = select
    imap.uid_search = AsyncMock(side_effect=uid_search)
    imap.uid.side_effect = uid
    imap.selected = selected
    return imap


@pytest.mark.asyncio
async def test_reply_identity_prefers_to_over_delivery_mailbox(email_server) -> None:
    client = _client(email_server)
    imap = _reply_imap(
        {"INBOX": [b"7"]},
        {
            "7": "Message-ID: <a@b>\nFrom: x@example.test\nTo: Customer <c@example.test>, Lu <LU@lukius.de>\n"
            "X-Original-To: mail@lukaselsner.de\n\n"
        },
    )
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        identity = await client.find_reply_identity("<a@b>", list(IDENTITIES))
    assert identity == "Lu <LU@lukius.de>"
    assert imap.selected == ["INBOX"]
    assert imap.timeout == 30.0


@pytest.mark.asyncio
async def test_reply_identity_searches_other_mailboxes_and_requires_exact_message_id(email_server) -> None:
    client = _client(email_server)
    imap = _reply_imap(
        {"INBOX": [b"3"], "Archive": [b"9"]},
        {
            "3": "Message-ID: <a@b.longer>\nTo: lu@lukius.de\n\n",
            "9": "Message-ID: <a@b>\nTo: info@lukaselsner.de\n\n",
        },
    )
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        identity = await client.find_reply_identity("<a@b>", list(IDENTITIES))
    assert identity == "info@lukaselsner.de"
    assert imap.selected == ["INBOX", "Archive"]


@pytest.mark.asyncio
async def test_reply_identity_respects_read_sender_allowlist(email_server) -> None:
    client = _client(email_server)
    imap = _reply_imap({"INBOX": [b"7"]}, {"7": "Message-ID: <a@b>\nFrom: blocked@example.test\nTo: lu@lukius.de\n\n"})
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        identity = await client.find_reply_identity("<a@b>", list(IDENTITIES), ["friend@example.test"])
    assert identity is None


@pytest.mark.asyncio
async def test_reply_identity_uses_first_hop_envelope_recipient_only(email_server) -> None:
    client = _client(email_server)
    forwarded = (
        "X-Original-To: relay@lukaselsner.de\nDelivered-To: mail@lukaselsner.de\n"
        "X-Original-To: mail@foreign.test\nDelivered-To: mail@foreign.test\n"
        "To: mail@foreign.test\nMessage-ID: <a@b>\n\n"
    )
    bcc = "X-Original-To: alias@lukius.de\nDelivered-To: mail@lukaselsner.de\nTo: list@example.test\nMessage-ID: <c@d>\n\n"
    imap = _reply_imap({"INBOX": [b"7"]}, {"7": forwarded})
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        assert await client.find_reply_identity("<a@b>", list(IDENTITIES)) is None
    imap = _reply_imap({"INBOX": [b"8"]}, {"8": bcc})
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        assert await client.find_reply_identity("<c@d>", list(IDENTITIES)) == "alias@lukius.de"


@pytest.mark.asyncio
async def test_reply_identity_ignores_foreign_recipients(email_server) -> None:
    client = _client(email_server)
    imap = _reply_imap({"INBOX": [b"7"]}, {"7": "Message-ID: <a@b>\nTo: someone@example.test\n\n"})
    with patch.object(client, "_connect_imap", AsyncMock(return_value=imap)):
        assert await client.find_reply_identity("<a@b>", list(IDENTITIES)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["send_email", "forward_email", "save_draft"])
async def test_tools_pass_from_address_and_map_identity_denial(tool: str) -> None:
    from mcp_email_server import app as app_module

    handler = AsyncMock(side_effect=SenderIdentityDeniedError("sender identity policy denied the from_address"))
    if tool == "send_email":
        target = patch("mcp_email_server.app.send_email_command", handler)
        call = app_module.send_email(
            account_name="main", recipients=["to@example.test"], subject="S", body="B", from_address="x@evil.test"
        )
    elif tool == "forward_email":
        target = patch("mcp_email_server.app.forward_email_command", handler)
        call = app_module.forward_email(
            account_name="main", email_id="7", recipients=["to@example.test"], from_address="x@evil.test"
        )
    else:
        runtime = MagicMock()
        runtime.mutations.save_draft.execute = handler
        target = patch("mcp_email_server.app.get_application_runtime", return_value=runtime)
        call = app_module.save_draft(account_name="main", subject="S", body="B", from_address="x@evil.test")
    with target, pytest.raises(ValueError, match="not an allowed sender identity"):
        await call
    assert handler.await_args.args[0].from_address == "x@evil.test"


def test_v6_catalog_migrates_with_empty_from_addresses_and_stores_updates(tmp_path) -> None:
    import sqlite3
    from contextlib import closing

    from mcp_email_server import managed as managed_module
    from mcp_email_server.managed import ManagedCatalog, ManagedSqliteSecretStore
    from tests.test_managed_catalog import _harden_private_test_file, _private_directory

    parent = _private_directory(tmp_path / "managed-v6")
    path = parent / "catalog.sqlite3"
    path.touch(mode=0o600)
    _harden_private_test_file(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(managed_module._SCHEMA_V6)
        connection.execute("INSERT INTO schema_metadata(singleton, version) VALUES (1, 6)")
        connection.execute(
            """INSERT INTO catalog(
                   id, revision, enable_attachment_download, enable_attachment_content,
                   allowed_recipients_json, allowed_senders_json, report_blocked_mutations
               ) VALUES ('local', 3, 0, 0, '[]', '[]', 0)"""
        )
        connection.commit()
    catalog = ManagedCatalog(path, secret_store=ManagedSqliteSecretStore(path))

    policy = catalog.policy()
    assert policy.allowed_from_addresses == ()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == 7

    revision = catalog.update_policy(
        expected_revision=policy.revision,
        enable_attachment_download=False,
        allowed_recipients=(),
        allowed_senders=(),
        report_blocked_mutations=False,
        allowed_from_addresses=("*@example.test",),
    )
    updated = catalog.policy()
    assert updated.revision == revision
    assert updated.allowed_from_addresses == ("*@example.test",)
    assert catalog.load_settings().allowed_from_addresses == ["*@example.test"]
