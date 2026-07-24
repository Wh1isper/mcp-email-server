from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from loguru import logger as loguru_logger
from starlette.routing import Route
from starlette.types import Scope

from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.management import CredentialMutationResult
from mcp_email_server.web_ui.app import LocalUiState, _matched_management_operation, create_local_ui_app


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


async def _client(state: LocalUiState) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_local_ui_app(state))
    return httpx.AsyncClient(transport=transport, base_url=state.origin)


async def _exchange(
    client: httpx.AsyncClient,
    state: LocalUiState,
    token: str,
) -> tuple[str, httpx.Response]:
    response = await client.post(
        f"{state.route_prefix}/api/bootstrap",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": state.origin,
            "Sec-Fetch-Site": "same-origin",
        },
        content="{}",
    )
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    assert isinstance(csrf, str)
    return csrf, response


@pytest.mark.asyncio
async def test_bootstrap_is_one_time_and_creates_narrow_strict_session_cookie() -> None:
    token = "bootstrap-sentinel"  # noqa: S105 - synthetic one-time test value
    state = LocalUiState(port=8765, management=MagicMock(), bootstrap_token=token)
    async with await _client(state) as client:
        csrf, exchange = await _exchange(client, state, token)
        replay = await client.post(
            f"{state.route_prefix}/api/bootstrap",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Origin": state.origin,
            },
            content="{}",
        )
        session = await client.get(f"{state.route_prefix}/api/session")

    assert replay.status_code == 401
    assert replay.json() == {
        "category": "bootstrap_rejected",
        "message": "The launch link is invalid or expired.",
    }
    assert token not in replay.text
    assert session.status_code == 200
    assert session.json() == {"csrf": csrf}
    cookie = replay.request.headers.get("cookie", "")
    assert state.cookie_name in cookie
    set_cookie = exchange.headers["set-cookie"]
    assert state.cookie_name in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert f"Path={state.route_prefix}/" in set_cookie
    assert state.cookie_name in client.cookies
    assert client.cookies.get(state.cookie_name) is not None


@pytest.mark.asyncio
async def test_only_one_concurrent_bootstrap_exchange_succeeds() -> None:
    token = "concurrent-bootstrap"  # noqa: S105 - synthetic one-time test value
    state = LocalUiState(port=8766, management=MagicMock(), bootstrap_token=token)
    async with await _client(state) as client:

        async def exchange() -> int:
            response = await client.post(
                f"{state.route_prefix}/api/bootstrap",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Origin": state.origin,
                },
                content="{}",
            )
            return response.status_code

        statuses = await asyncio.gather(exchange(), exchange())

    assert sorted(statuses) == [200, 401]


@pytest.mark.asyncio
async def test_bootstrap_expiry_and_rate_limit_use_bounded_uniform_errors() -> None:
    clock = Clock()
    state = LocalUiState(
        port=8767,
        management=MagicMock(),
        bootstrap_token="expired-token",
        now=clock,
    )
    clock.value += 301
    async with await _client(state) as client:
        expired = await client.post(
            f"{state.route_prefix}/api/bootstrap",
            headers={
                "Authorization": "Bearer expired-token",
                "Content-Type": "application/json",
                "Origin": state.origin,
            },
            content="{}",
        )
        responses = []
        for _ in range(9):
            responses.append(
                await client.post(
                    f"{state.route_prefix}/api/bootstrap",
                    headers={
                        "Authorization": "Bearer wrong-token",
                        "Content-Type": "application/json",
                        "Origin": state.origin,
                    },
                    content="{}",
                )
            )

    assert expired.status_code == 401
    assert responses[-1].status_code == 429
    assert responses[-1].headers["retry-after"] == "60"
    assert responses[-1].json()["message"] == expired.json()["message"]


@pytest.mark.asyncio
async def test_host_origin_fetch_metadata_csrf_and_json_are_enforced() -> None:
    state = LocalUiState(port=8768, management=MagicMock(), bootstrap_token="valid-token")
    async with await _client(state) as client:
        bad_host = await client.get(
            f"{state.route_prefix}/",
            headers={"Host": "localhost:8768"},
        )
        csrf, _exchange_response = await _exchange(client, state, "valid-token")
        path = f"{state.route_prefix}/api/session/logout"
        missing_origin = await client.post(
            path,
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
            content="{}",
        )
        foreign_origin = await client.post(
            path,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Origin": "http://localhost:8768",
            },
            content="{}",
        )
        cross_site = await client.post(
            path,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Origin": state.origin,
                "Sec-Fetch-Site": "cross-site",
            },
            content="{}",
        )
        bad_csrf = await client.post(
            path,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": "wrong",
                "Origin": state.origin,
            },
            content="{}",
        )
        non_json = await client.post(
            path,
            headers={"Content-Type": "text/plain", "X-CSRF-Token": csrf, "Origin": state.origin},
            content="{}",
        )

    assert bad_host.status_code == 400
    assert missing_origin.status_code == 403
    assert foreign_origin.status_code == 403
    assert cross_site.status_code == 403
    assert bad_csrf.status_code == 403
    assert non_json.status_code == 415


@pytest.mark.asyncio
async def test_every_response_has_strict_headers_and_no_cors() -> None:
    state = LocalUiState(port=8769, management=MagicMock())
    async with await _client(state) as client:
        for path in (f"{state.route_prefix}/", "/not-a-route"):
            response = await client.get(path)
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-frame-options"] == "DENY"
            assert "access-control-allow-origin" not in response.headers
            assert "access-control-allow-credentials" not in response.headers
            assert "server" not in response.headers


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_before_json_parsing() -> None:
    state = LocalUiState(port=8770, management=MagicMock(), bootstrap_token="valid-token")
    async with await _client(state) as client:
        response = await client.post(
            f"{state.route_prefix}/api/bootstrap",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Origin": state.origin,
            },
            content=b" " * (APPLICATION_LIMITS.ui_json_body_bytes + 1),
        )

    assert response.status_code == 413
    assert len(response.content) < 500


@pytest.mark.asyncio
async def test_logout_and_state_close_invalidate_sessions() -> None:
    state = LocalUiState(port=8771, management=MagicMock(), bootstrap_token="valid-token")
    async with await _client(state) as client:
        csrf, _exchange_response = await _exchange(client, state, "valid-token")
        logout = await client.post(
            f"{state.route_prefix}/api/session/logout",
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Origin": state.origin,
            },
            content="{}",
        )
        after_logout = await client.get(f"{state.route_prefix}/api/session")
        await state.close()
        after_close = await client.get(f"{state.route_prefix}/api/session")

    assert logout.status_code == 200
    assert after_logout.status_code == 401
    assert after_close.status_code == 401


@pytest.mark.asyncio
async def test_management_access_log_uses_fixed_operations_without_sensitive_request_data() -> None:
    token = "bootstrap-log-sentinel"  # noqa: S105 - synthetic one-time test value
    secret = "credential-log-sentinel"  # noqa: S105 - synthetic test value
    account_name = "private-account-name"
    management = MagicMock()
    management.credentials.bind.return_value = management.credentials
    management.credentials.set.return_value = CredentialMutationResult(status="active", revision=2)
    state = LocalUiState(port=8772, management=management, bootstrap_token=token)
    messages: list[str] = []
    sink_id = loguru_logger.add(lambda message: messages.append(message.record["message"]), level="INFO")
    try:
        async with await _client(state) as client:
            csrf, _exchange_response = await _exchange(client, state, token)
            response = await client.post(
                f"{state.route_prefix}/api/accounts/{account_name}/credentials/incoming/set",
                headers={
                    "Content-Type": "application/json",
                    "Origin": state.origin,
                    "Sec-Fetch-Site": "same-origin",
                    "X-CSRF-Token": csrf,
                },
                json={
                    "expected_bootstrap_revision": 1,
                    "expected_catalog": "/private/catalog.sqlite3",
                    "expected_revision": 1,
                    "secret": secret,
                },
            )
    finally:
        loguru_logger.remove(sink_id)

    assert response.status_code == 200
    output = "\n".join(messages)
    assert "operation=management.bootstrap.exchange method=POST status=200 duration_ms=" in output
    assert "operation=management.credentials.set method=POST status=200 duration_ms=" in output
    for sensitive in (token, secret, account_name, csrf, state.route_segment, state.cookie_name):
        assert sensitive not in output


def test_management_log_matching_prefers_full_dynamic_route_over_partial_static_route() -> None:
    state = LocalUiState(port=8773, management=MagicMock())
    app = create_local_ui_app(state)
    routes = [route for route in app.routes if isinstance(route, Route)]
    scope = cast(
        Scope,
        {
            "type": "http",
            "path": f"{state.route_prefix}/api/accounts/create",
            "method": "GET",
        },
    )

    assert _matched_management_operation(scope, routes) == "management.accounts.show"


def test_process_route_cookie_and_bootstrap_are_unique() -> None:
    first = LocalUiState(port=8774, management=MagicMock())
    second = LocalUiState(port=8774, management=MagicMock())

    assert first.route_prefix != second.route_prefix
    assert first.cookie_name != second.cookie_name
    assert first.bootstrap_token != second.bootstrap_token


def test_route_inventory_has_no_mail_rpc_debug_openapi_or_filesystem_surface() -> None:
    state = LocalUiState(port=8775, management=MagicMock())
    app = create_local_ui_app(state)
    paths = {getattr(route, "path", "") for route in app.routes}

    assert len(paths) == 25
    assert f"{state.route_prefix}/api/catalog/initialize-default" in paths
    assert not any(path.endswith("/repair") for path in paths)
    assert not any(
        forbidden in path
        for path in paths
        for forbidden in ("openapi", "swagger", "debug", "rpc", "mail", "filesystem", "metrics")
    )
    assert all(path.startswith(state.route_prefix) for path in paths)
