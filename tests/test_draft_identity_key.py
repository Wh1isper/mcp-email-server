from __future__ import annotations

from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    DraftAppendCommand,
    SaveToMailboxCommand,
    validate_draft_identity_key,
)
from mcp_email_server.config import EmailSettings
from tests.test_managed_catalog import FakeSecretStore, _catalog, _server
from tests.test_mutation_application import _account


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "draft", "expected"), [("id4", True, "id4"), ("id4", False, None), (None, True, None)])
async def test_draft_append_writes_identity_key_only_for_drafts(
    email_settings, key: str | None, draft: bool, expected: str | None
) -> None:
    message = MIMEText("body")
    handler = MagicMock()
    handler.email_settings = email_settings
    handler.incoming_client.compose_message = MagicMock(return_value=message)
    handler.incoming_client.append_to_mailbox_with_outcome = AsyncMock(
        return_value=AppendMutationOutcome("succeeded", "id", mailbox="Drafts")
    )
    command_type = DraftAppendCommand if draft else SaveToMailboxCommand
    command = command_type("primary", (), "Subject", "Body", mailbox="Drafts", flags=(r"\Draft",))
    await ClassicMutationProvider(handler).save_to_mailbox(command, _account(draft_identity_key=key))
    assert message["X-Identity-Key"] == expected


@pytest.mark.parametrize("key", ["", "id 4", "id4\r\nBcc: x@evil.test", "x" * 65, "ïd4"])
def test_draft_identity_key_rejects_header_unsafe_values(key: str) -> None:
    with pytest.raises(ValueError, match="draft_identity_key"):
        validate_draft_identity_key(key)


def test_env_account_carries_draft_identity_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "mail@example.test")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "imap.example.test")
    monkeypatch.setenv("MCP_EMAIL_SERVER_DRAFT_IDENTITY_KEY", "id4")
    account = EmailSettings.from_env()
    assert account is not None
    assert account.draft_identity_key == "id4"


def test_managed_account_stores_and_updates_draft_identity_key(tmp_path) -> None:
    catalog = _catalog(tmp_path, FakeSecretStore())
    catalog.add_account(
        name="main",
        full_name="Main",
        email_address="mail@example.test",
        incoming=_server(),
        outgoing=None,
        draft_identity_key="id4",
    )
    catalog.set_secret("main", "incoming", "secret")
    assert catalog.show_account("main").draft_identity_key == "id4"
    assert catalog.load_account("main", roles=("incoming",)).draft_identity_key == "id4"
    assert catalog.load_settings().emails[0].draft_identity_key == "id4"

    revision = catalog.show_account("main").revision
    catalog.update_account("main", expected_revision=revision, draft_identity_key=None, update_draft_identity_key=True)
    assert catalog.show_account("main").draft_identity_key is None
    with pytest.raises(ValueError, match="draft_identity_key"):
        catalog.update_account(
            "main", expected_revision=revision + 1, draft_identity_key="bad key", update_draft_identity_key=True
        )
