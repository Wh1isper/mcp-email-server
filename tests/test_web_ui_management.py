from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from mcp_email_server.application.management import (
    AccountCreationResult,
    AccountDetails,
    CredentialMutationResult,
    EndpointSummary,
    RevisionConflictError,
)
from mcp_email_server.web_ui.app import LocalUiState, create_local_ui_app


def _endpoint() -> EndpointSummary:
    return EndpointSummary(
        host="imap.example.test",
        port=993,
        use_ssl=True,
        start_ssl=False,
        verify_ssl=True,
        user_name="alice@example.test",
    )


def _details(*, revision: int = 4) -> AccountDetails:
    return AccountDetails(
        name="alice",
        full_name="Alice",
        email_address="alice@example.test",
        enabled=True,
        revision=revision,
        save_to_sent=True,
        sent_folder_name=None,
        incoming=_endpoint(),
        outgoing=None,
        incoming_binding="ACTIVE",
        outgoing_binding=None,
    )


async def _authenticated(
    management: MagicMock,
    *,
    port: int,
) -> tuple[LocalUiState, httpx.AsyncClient, str]:
    state = LocalUiState(
        port=port,
        management=management,
        bootstrap_token="management-bootstrap",
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_local_ui_app(state)),
        base_url=state.origin,
    )
    response = await client.post(
        f"{state.route_prefix}/api/bootstrap",
        headers={
            "Authorization": "Bearer management-bootstrap",
            "Content-Type": "application/json",
            "Origin": state.origin,
        },
        content="{}",
    )
    assert response.status_code == 200
    return state, client, response.json()["csrf"]


def _mutation_headers(state: LocalUiState, csrf: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": state.origin,
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": csrf,
    }


@pytest.mark.asyncio
async def test_selection_uses_separate_bootstrap_and_catalog_revisions() -> None:
    management = MagicMock()
    state, client, csrf = await _authenticated(management, port=8779)
    try:
        legacy_response = await client.post(
            f"{state.route_prefix}/api/catalog/select",
            headers=_mutation_headers(state, csrf),
            json={
                "mode": "legacy",
                "expected_bootstrap_revision": 4,
                "expected_catalog_revision": None,
            },
        )
        managed_response = await client.post(
            f"{state.route_prefix}/api/catalog/select",
            headers=_mutation_headers(state, csrf),
            json={
                "mode": "managed",
                "expected_bootstrap_revision": 5,
                "expected_catalog_revision": 9,
            },
        )
    finally:
        await client.aclose()

    assert legacy_response.status_code == 200
    assert managed_response.status_code == 200
    assert management.lifecycle.select.call_args_list[0].kwargs == {
        "expected_bootstrap_revision": 4,
        "expected_catalog_revision": None,
    }
    assert management.lifecycle.select.call_args_list[1].kwargs == {
        "expected_bootstrap_revision": 5,
        "expected_catalog_revision": 9,
    }


@pytest.mark.asyncio
async def test_create_account_passes_secret_once_and_never_echoes_it() -> None:
    management = MagicMock()
    management.accounts.create.return_value = AccountCreationResult(
        incoming=CredentialMutationResult(status="active", revision=2),
        outgoing=None,
    )
    state, client, csrf = await _authenticated(management, port=8780)
    sentinel = "never-echo-this-credential"
    try:
        response = await client.post(
            f"{state.route_prefix}/api/accounts/create",
            headers=_mutation_headers(state, csrf),
            json={
                "expected_catalog_revision": 1,
                "name": "alice",
                "full_name": "Alice",
                "email_address": "alice@example.test",
                "save_to_sent": True,
                "sent_folder_name": None,
                "incoming": {
                    "host": "imap.example.test",
                    "port": 993,
                    "use_ssl": True,
                    "start_ssl": False,
                    "verify_ssl": True,
                    "user_name": "alice@example.test",
                },
                "outgoing": None,
                "credentials": {"incoming": sentinel, "outgoing": None},
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert sentinel not in response.text
    assert response.json() == {
        "incoming": {"state": "active", "revision": 2},
        "outgoing": None,
    }
    command = management.accounts.create.call_args.args[0]
    assert command.incoming_secret.get_secret_value() == sentinel
    assert sentinel not in repr(command)
    assert command.incoming.host == "imap.example.test"


@pytest.mark.asyncio
async def test_typed_revision_conflict_returns_bounded_non_secret_current_summary() -> None:
    management = MagicMock()
    management.accounts.disable.side_effect = RevisionConflictError("account", name="alice")
    management.accounts.show.return_value = _details(revision=7)
    state, client, csrf = await _authenticated(management, port=8781)
    try:
        response = await client.post(
            f"{state.route_prefix}/api/accounts/alice/disable",
            headers=_mutation_headers(state, csrf),
            json={"expected_revision": 4},
        )
    finally:
        await client.aclose()

    assert response.status_code == 409
    body = response.json()
    assert body["category"] == "conflict"
    assert body["current"]["name"] == "alice"
    assert body["current"]["revision"] == 7
    assert body["current"]["has_outgoing"] is False
    assert "secret" not in response.text.casefold()
    assert len(response.content) < 4096


@pytest.mark.asyncio
async def test_credential_wire_mapping_and_repair_are_explicit() -> None:
    management = MagicMock()
    management.credentials.set.return_value = CredentialMutationResult(
        status="active_cleanup_required",
        revision=5,
        cleanup_required=1,
    )
    management.credentials.repair.return_value.status = "rolled_back"
    management.credentials.repair.return_value.revision = 6
    management.credentials.repair.return_value.cleanup_required = 0
    state, client, csrf = await _authenticated(management, port=8782)
    try:
        set_response = await client.post(
            f"{state.route_prefix}/api/accounts/alice/credentials/incoming/set",
            headers=_mutation_headers(state, csrf),
            json={"secret": "new-secret", "expected_revision": 4},
        )
        repair_response = await client.post(
            f"{state.route_prefix}/api/accounts/alice/credentials/incoming/repair",
            headers=_mutation_headers(state, csrf),
            json={"action": "rollback", "expected_revision": 5},
        )
    finally:
        await client.aclose()

    assert set_response.json() == {
        "state": "active_cleanup_required",
        "revision": 5,
        "cleanup_required": 1,
    }
    assert "new-secret" not in set_response.text
    assert repair_response.json() == {
        "state": "rolled_back",
        "revision": 6,
        "cleanup_required": 0,
    }
    management.credentials.repair.assert_called_once_with(
        "alice",
        "incoming",
        action="rollback",
        expected_revision=5,
    )


@pytest.mark.asyncio
async def test_invalid_secret_request_uses_fixed_error_without_input_echo() -> None:
    management = MagicMock()
    state, client, csrf = await _authenticated(management, port=8783)
    sentinel = "invalid-secret-sentinel"
    try:
        response = await client.post(
            f"{state.route_prefix}/api/accounts/alice/credentials/incoming/set",
            headers=_mutation_headers(state, csrf),
            json={"secret": sentinel, "expected_revision": "not-an-integer"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 422
    assert response.json()["message"] == "The request body is invalid."
    assert sentinel not in response.text
    management.credentials.set.assert_not_called()
