from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from mcp_email_server.application.management import (
    AccountCreationResult,
    AccountDetails,
    CredentialMutationResult,
    EndpointSummary,
    ManagementError,
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
    for service_name in ("lifecycle", "accounts", "credentials", "policy", "connectivity"):
        service = getattr(management, service_name)
        service.bind.return_value = service
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


def _target_payload(**values: object) -> dict[str, object]:
    return {
        "expected_bootstrap_revision": 2,
        "expected_catalog": "/private/catalog.sqlite3",
        **values,
    }


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
            json=_target_payload(
                expected_catalog_revision=1,
                name="alice",
                full_name="Alice",
                email_address="alice@example.test",
                save_to_sent=True,
                sent_folder_name=None,
                incoming={
                    "host": "imap.example.test",
                    "port": 993,
                    "use_ssl": True,
                    "start_ssl": False,
                    "verify_ssl": True,
                    "user_name": "alice@example.test",
                },
                outgoing=None,
                credentials={"incoming": sentinel, "outgoing": None},
            ),
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert sentinel not in response.text
    assert response.json() == {
        "incoming": {"state": "active", "revision": 2, "cleanup_required": 0},
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
            json=_target_payload(expected_revision=4),
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
async def test_credential_wire_mapping_is_explicit() -> None:
    management = MagicMock()
    management.credentials.set.return_value = CredentialMutationResult(
        status="active_cleanup_required",
        revision=5,
        cleanup_required=1,
    )
    state, client, csrf = await _authenticated(management, port=8782)
    try:
        response = await client.post(
            f"{state.route_prefix}/api/accounts/alice/credentials/incoming/set",
            headers=_mutation_headers(state, csrf),
            json=_target_payload(secret="new-secret", expected_revision=4),
        )
    finally:
        await client.aclose()

    assert response.json() == {
        "state": "active_cleanup_required",
        "revision": 5,
        "cleanup_required": 1,
    }
    assert "new-secret" not in response.text
    management.credentials.set.assert_called_once_with(
        "alice",
        "incoming",
        "new-secret",
        expected_revision=4,
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
            json=_target_payload(secret=sentinel, expected_revision="not-an-integer"),
        )
    finally:
        await client.aclose()

    assert response.status_code == 422
    assert response.json()["message"] == "The request body is invalid."
    assert sentinel not in response.text
    management.credentials.set.assert_not_called()


@pytest.mark.asyncio
async def test_typed_management_error_uses_safe_recoverable_category() -> None:
    management = MagicMock()
    management.lifecycle.status.side_effect = ManagementError(
        "duplicate private-account-name at /private/catalog.sqlite3",
        reason="account_name_exists",
    )
    state, client, _csrf = await _authenticated(management, port=8784)
    try:
        response = await client.get(f"{state.route_prefix}/api/status")
    finally:
        await client.aclose()

    assert response.status_code == 400
    assert response.json() == {
        "category": "account_name_exists",
        "message": "The management operation could not be completed.",
    }
    assert "private-account-name" not in response.text
    assert "catalog.sqlite3" not in response.text


@pytest.mark.asyncio
async def test_application_value_error_is_a_safe_invalid_request() -> None:
    management = MagicMock()
    management.lifecycle.status.side_effect = ValueError("private invalid input")
    state, client, _csrf = await _authenticated(management, port=8785)
    try:
        response = await client.get(f"{state.route_prefix}/api/status")
    finally:
        await client.aclose()

    assert response.status_code == 422
    assert response.json() == {
        "category": "invalid_request",
        "message": "The request values are invalid.",
    }
    assert "private invalid input" not in response.text


@pytest.mark.asyncio
async def test_default_initialization_uses_csrf_and_expected_bootstrap_revision() -> None:
    management = MagicMock()
    management.lifecycle.initialize_default.return_value = Path("/private/managed.sqlite3")
    state, client, csrf = await _authenticated(management, port=8786)
    try:
        response = await client.post(
            f"{state.route_prefix}/api/catalog/initialize-default",
            headers=_mutation_headers(state, csrf),
            json={"expected_bootstrap_revision": 7, "require_empty_install": False},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.json() == {
        "status": "initialized",
        "database": "/private/managed.sqlite3",
    }
    management.lifecycle.initialize_default.assert_called_once_with(
        expected_bootstrap_revision=7,
        require_empty_install=False,
    )
