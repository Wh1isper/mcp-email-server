from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any, TypeVar

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match, Route
from starlette.types import Scope

from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.management import (
    AccountDetails,
    BindingRole,
    CreateAccountCommand,
    ManagementError,
    ManagementServices,
    RevisionConflictError,
)
from mcp_email_server.log import logger
from mcp_email_server.runtime import close_application_runtime, get_application_runtime
from mcp_email_server.web_ui.models import (
    AccountLifecycleRequest,
    ApplyImportRequest,
    CleanupCredentialsRequest,
    CreateAccountRequest,
    EmptyRequest,
    ExpectedRevisionRequest,
    InitializeDefaultCatalogRequest,
    RemoveAccountRequest,
    RequestModel,
    SelectCatalogRequest,
    SetCredentialRequest,
    UpdateAccountRequest,
    UpdatePolicyRequest,
)

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "font-src 'self'; connect-src 'self'; manifest-src 'none'; worker-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
_BOOTSTRAP_TTL_SECONDS = 300.0
_SESSION_TTL_SECONDS = 8 * 60 * 60.0
_BOOTSTRAP_ATTEMPT_WINDOW_SECONDS = 60.0
_BOOTSTRAP_ATTEMPTS_PER_WINDOW = 8
_DUMMY_TOKEN = "0" * 43

ModelT = TypeVar("ModelT", bound=RequestModel)
Handler = Callable[[Request], Awaitable[Response]]


@dataclass
class _Session:
    csrf: str
    expires_at: float


class LocalUiState:
    """Process-local authentication state and immutable route authority."""

    def __init__(
        self,
        *,
        port: int,
        management: ManagementServices | None = None,
        bootstrap_token: str | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if port < 1 or port > 65535:
            raise ValueError("The local UI requires its actual bound port")
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.route_segment = f"manage-{secrets.token_urlsafe(12)}"
        self.route_prefix = f"/{self.route_segment}"
        self.cookie_name = f"mcp_email_ui_{secrets.token_hex(8)}"
        self.bootstrap_token = bootstrap_token or secrets.token_urlsafe(32)
        self.bootstrap_expires_at = now() + _BOOTSTRAP_TTL_SECONDS
        self.management = management or get_application_runtime().management
        self._now = now
        self._sessions: dict[str, _Session] = {}
        self._attempts: list[float] = []
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def launch_url(self) -> str:
        token = self.bootstrap_token
        if token is None:
            raise RuntimeError("The bootstrap token has already been consumed")
        return f"{self.origin}{self.route_prefix}/#bootstrap={token}"

    async def exchange(self, presented: str) -> tuple[str, str, bool]:
        """Constant-time compare and atomically consume one valid bootstrap token."""
        async with self._lock:
            now = self._now()
            self._attempts = [
                attempt for attempt in self._attempts if now - attempt <= _BOOTSTRAP_ATTEMPT_WINDOW_SECONDS
            ]
            limited = len(self._attempts) >= _BOOTSTRAP_ATTEMPTS_PER_WINDOW
            expected = self.bootstrap_token if self.bootstrap_token is not None else _DUMMY_TOKEN
            presented_digest = hashlib.sha256(presented.encode("utf-8")).digest()
            expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
            matched = secrets.compare_digest(presented_digest, expected_digest)
            valid = (
                not self._closed
                and not limited
                and self.bootstrap_token is not None
                and now <= self.bootstrap_expires_at
                and matched
            )
            if not valid:
                self._attempts.append(now)
                return "", "", limited
            self.bootstrap_token = None
            session_id = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self._sessions[session_id] = _Session(csrf=csrf, expires_at=now + _SESSION_TTL_SECONDS)
            return session_id, csrf, False

    async def session(self, session_id: str | None) -> _Session | None:
        if session_id is None:
            return None
        async with self._lock:
            session = self._sessions.get(session_id)
            now = self._now()
            if self._closed or session is None or session.expires_at < now:
                self._sessions.pop(session_id, None)
                return None
            session.expires_at = now + _SESSION_TTL_SECONDS
            return session

    async def logout(self, session_id: str | None) -> None:
        if session_id is None:
            return
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self.bootstrap_token = None
            self._sessions.clear()
            self._attempts.clear()


def _json(data: object, *, status_code: int = 200) -> JSONResponse:
    encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > APPLICATION_LIMITS.ui_json_body_bytes:
        return JSONResponse(
            {"category": "response_too_large", "message": "The management response exceeded its safe limit."},
            status_code=500,
        )
    return Response(encoded, status_code=status_code, media_type="application/json")  # type: ignore[return-value]


def _error(status_code: int, category: str, message: str, *, current: object | None = None) -> JSONResponse:
    body: dict[str, object] = {"category": category, "message": message[:500]}
    if current is not None:
        body["current"] = current
    return _json(body, status_code=status_code)


async def _parse_json(request: Request, model: type[ModelT]) -> ModelT:
    content_type = request.headers.get("content-type", "")
    media_type, _, parameters = content_type.partition(";")
    if media_type.strip().lower() != "application/json":
        raise _HttpError(415, "json_required", "A JSON request body is required.")
    if parameters and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}:
        raise _HttpError(415, "json_required", "A UTF-8 JSON request body is required.")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > APPLICATION_LIMITS.ui_json_body_bytes:
                raise _HttpError(413, "request_too_large", "The request body exceeded its safe limit.")
        except ValueError as exc:
            raise _HttpError(400, "invalid_request", "The request metadata is invalid.") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > APPLICATION_LIMITS.ui_json_body_bytes:
            raise _HttpError(413, "request_too_large", "The request body exceeded its safe limit.")
    try:
        return model.model_validate_json(bytes(body))
    except ValidationError as exc:
        raise _HttpError(422, "invalid_request", "The request body is invalid.") from exc


class _HttpError(Exception):
    def __init__(self, status_code: int, category: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.category = category
        self.message = message


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or authorization.count(" ") != 1:
        return ""
    token = authorization[7:]
    return token if len(token) <= 4096 else ""


def _same_origin_mutation(request: Request, state: LocalUiState) -> None:
    if request.headers.get("origin") != state.origin:
        raise _HttpError(403, "origin_rejected", "The request origin is not allowed.")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        raise _HttpError(403, "request_context_rejected", "The request context is not allowed.")


async def _require_session(request: Request, state: LocalUiState) -> _Session:
    session = await state.session(request.cookies.get(state.cookie_name))
    if session is None:
        raise _HttpError(401, "session_required", "This management session is not authenticated.")
    return session


async def _authorize_mutation(request: Request, state: LocalUiState) -> _Session:
    _same_origin_mutation(request, state)
    session = await _require_session(request, state)
    csrf = request.headers.get("x-csrf-token", "")
    if not csrf or not secrets.compare_digest(csrf, session.csrf):
        raise _HttpError(403, "csrf_rejected", "The request verification token is invalid.")
    return session


def _account_name(request: Request) -> str:
    value = request.path_params.get("name")
    if not isinstance(value, str) or not value:
        raise _HttpError(404, "not_found", "The requested management route was not found.")
    return value


def _role(request: Request) -> BindingRole:
    value = request.path_params.get("role")
    if value == "incoming":
        return "incoming"
    if value == "outgoing":
        return "outgoing"
    raise _HttpError(404, "not_found", "The requested management route was not found.")


def _asdict(value: object) -> dict[str, object]:
    return asdict(value)  # type: ignore[arg-type]


def _account_details(value: AccountDetails) -> dict[str, object]:
    result = _asdict(value)
    result["has_outgoing"] = value.outgoing is not None
    return result


async def _current_summary(request: Request, state: LocalUiState) -> dict[str, object]:
    path = request.url.path
    try:
        if "/accounts/" in path:
            account = await run_in_threadpool(state.management.accounts.show, _account_name(request))
            return _account_details(account)
        status = await run_in_threadpool(state.management.catalog.status)
        return _asdict(status)
    except Exception:
        return {}


def _mutation(model: type[ModelT]) -> Callable[[Callable[[Request, LocalUiState, ModelT], Awaitable[object]]], Handler]:
    def decorate(function: Callable[[Request, LocalUiState, ModelT], Awaitable[object]]) -> Handler:
        async def handler(request: Request) -> Response:
            state: LocalUiState = request.app.state.local_ui
            await _authorize_mutation(request, state)
            payload = await _parse_json(request, model)
            result = await function(request, state, payload)
            return result if isinstance(result, Response) else _json(result)

        return handler

    return decorate


async def _bootstrap(request: Request) -> Response:
    state: LocalUiState = request.app.state.local_ui
    _same_origin_mutation(request, state)
    await _parse_json(request, EmptyRequest)
    session_id, csrf, limited = await state.exchange(_bearer_token(request))
    if not session_id:
        status = 429 if limited else 401
        response = _error(status, "bootstrap_rejected", "The launch link is invalid or expired.")
        if limited:
            response.headers["Retry-After"] = "60"
        return response
    response = _json({"csrf": csrf})
    response.set_cookie(
        state.cookie_name,
        session_id,
        httponly=True,
        secure=False,
        samesite="strict",
        path=f"{state.route_prefix}/",
        max_age=int(_SESSION_TTL_SECONDS),
    )
    return response


async def _session_status(request: Request) -> Response:
    state: LocalUiState = request.app.state.local_ui
    session = await _require_session(request, state)
    return _json({"csrf": session.csrf})


@_mutation(EmptyRequest)
async def _logout(request: Request, state: LocalUiState, _payload: EmptyRequest) -> Response:
    await state.logout(request.cookies.get(state.cookie_name))
    response = _json({"status": "logged_out"})
    response.delete_cookie(state.cookie_name, path=f"{state.route_prefix}/")
    return response


async def _status(request: Request) -> Response:
    state: LocalUiState = request.app.state.local_ui
    await _require_session(request, state)
    return _json(_asdict(await run_in_threadpool(state.management.catalog.status)))


@_mutation(InitializeDefaultCatalogRequest)
async def _initialize_default(
    _request: Request,
    state: LocalUiState,
    payload: InitializeDefaultCatalogRequest,
) -> object:
    result = await run_in_threadpool(
        state.management.catalog.initialize_default,
        expected_bootstrap_revision=payload.expected_bootstrap_revision,
        require_empty_install=payload.require_empty_install,
    )
    return {"status": "initialized", **_asdict(result)}


def _bound_service(service: Any, payload: Any) -> Any:
    return service.bind(
        expected_bootstrap_revision=payload.expected_bootstrap_revision,
        expected_catalog=payload.expected_catalog,
    )


@_mutation(SelectCatalogRequest)
async def _select(_request: Request, state: LocalUiState, payload: SelectCatalogRequest) -> object:
    await run_in_threadpool(
        state.management.catalog.select,
        payload.mode,
        expected_bootstrap_revision=payload.expected_bootstrap_revision,
        expected_catalog_revision=payload.expected_catalog_revision,
    )
    return {"status": "selected"}


async def _accounts(request: Request) -> Response:
    state: LocalUiState = request.app.state.local_ui
    await _require_session(request, state)
    result = await run_in_threadpool(state.management.accounts.list)
    return _json([_asdict(item) for item in result])


async def _account(request: Request) -> Response:
    state: LocalUiState = request.app.state.local_ui
    await _require_session(request, state)
    result = await run_in_threadpool(state.management.accounts.show, _account_name(request))
    return _json(_account_details(result))


@_mutation(CreateAccountRequest)
async def _create_account(_request: Request, state: LocalUiState, payload: CreateAccountRequest) -> object:
    accounts = _bound_service(state.management.accounts, payload)
    result = await run_in_threadpool(
        accounts.create,
        CreateAccountCommand(
            expected_catalog_revision=payload.expected_catalog_revision,
            name=payload.name,
            full_name=payload.full_name,
            email_address=payload.email_address,
            incoming=payload.incoming.summary(),
            incoming_secret=payload.credentials.incoming,
            outgoing=payload.outgoing.summary()
            if payload.outgoing is not None and payload.credentials.outgoing is not None
            else None,
            outgoing_secret=payload.credentials.outgoing,
            save_to_sent=payload.save_to_sent,
            sent_folder_name=payload.sent_folder_name,
        ),
    )
    return {
        "incoming": {
            "state": result.incoming.status,
            "revision": result.incoming.revision,
            "cleanup_required": result.incoming.cleanup_required,
        },
        "outgoing": {
            "state": result.outgoing.status,
            "revision": result.outgoing.revision,
            "cleanup_required": result.outgoing.cleanup_required,
        }
        if result.outgoing is not None
        else None,
    }


@_mutation(UpdateAccountRequest)
async def _update_account(request: Request, state: LocalUiState, payload: UpdateAccountRequest) -> object:
    current_name = _account_name(request)
    accounts = _bound_service(state.management.accounts, payload)
    current = await run_in_threadpool(accounts.show, current_name)
    await run_in_threadpool(
        accounts.update,
        payload.command(current_name, had_outgoing=current.outgoing is not None),
    )
    return _account_details(await run_in_threadpool(accounts.show, payload.name))


@_mutation(AccountLifecycleRequest)
async def _enable_account(request: Request, state: LocalUiState, payload: AccountLifecycleRequest) -> object:
    name = _account_name(request)
    accounts = _bound_service(state.management.accounts, payload)
    await run_in_threadpool(accounts.enable, name, expected_revision=payload.expected_revision)
    return _account_details(await run_in_threadpool(accounts.show, name))


@_mutation(AccountLifecycleRequest)
async def _disable_account(request: Request, state: LocalUiState, payload: AccountLifecycleRequest) -> object:
    name = _account_name(request)
    accounts = _bound_service(state.management.accounts, payload)
    await run_in_threadpool(accounts.disable, name, expected_revision=payload.expected_revision)
    return _account_details(await run_in_threadpool(accounts.show, name))


@_mutation(RemoveAccountRequest)
async def _remove_account(request: Request, state: LocalUiState, payload: RemoveAccountRequest) -> object:
    accounts = _bound_service(state.management.accounts, payload)
    result = await run_in_threadpool(
        accounts.soft_remove,
        _account_name(request),
        expected_revision=payload.expected_revision,
        confirmation=payload.confirmation,
    )
    return {"status": "removed", **_asdict(result)}


@_mutation(SetCredentialRequest)
async def _set_credential(request: Request, state: LocalUiState, payload: SetCredentialRequest) -> object:
    credentials = _bound_service(state.management.credentials, payload)
    result = await run_in_threadpool(
        credentials.set,
        _account_name(request),
        _role(request),
        payload.secret.get_secret_value(),
        expected_revision=payload.expected_revision,
    )
    return {"state": result.status, "revision": result.revision, "cleanup_required": result.cleanup_required}


@_mutation(ExpectedRevisionRequest)
async def _remove_credential(request: Request, state: LocalUiState, payload: ExpectedRevisionRequest) -> object:
    credentials = _bound_service(state.management.credentials, payload)
    result = await run_in_threadpool(
        credentials.remove,
        _account_name(request),
        _role(request),
        expected_revision=payload.expected_revision,
    )
    return {
        "state": result.status,
        "revision": result.revision,
        "cleanup_required": result.cleanup_required,
    }


@_mutation(CleanupCredentialsRequest)
async def _cleanup(_request: Request, state: LocalUiState, payload: CleanupCredentialsRequest) -> object:
    credentials = _bound_service(state.management.credentials, payload)
    result = await run_in_threadpool(
        credentials.cleanup,
        limit=payload.limit,
        expected_revision=payload.expected_revision,
    )
    return _asdict(result)


async def _policy(request: Request) -> Response:
    state: LocalUiState = request.app.state.local_ui
    await _require_session(request, state)
    return _json(_asdict(await run_in_threadpool(state.management.policy.get)))


@_mutation(UpdatePolicyRequest)
async def _update_policy(_request: Request, state: LocalUiState, payload: UpdatePolicyRequest) -> object:
    policy = _bound_service(state.management.policy, payload)
    return _asdict(await run_in_threadpool(policy.update, payload.policy()))


@_mutation(EmptyRequest)
async def _preview_import(_request: Request, state: LocalUiState, _payload: EmptyRequest) -> object:
    return _asdict(await run_in_threadpool(state.management.legacy_import.preview))


@_mutation(ApplyImportRequest)
async def _apply_import(_request: Request, state: LocalUiState, payload: ApplyImportRequest) -> object:
    result = await run_in_threadpool(
        state.management.legacy_import.apply,
        preview_token=payload.preview_token,
        expected_revision=payload.expected_revision,
        confirmation=payload.confirmation,
    )
    return {
        "created": list(result.created),
        "resumed": list(result.resumed),
        "attention_required": list(result.attention_required),
        "mode": result.mode,
        "bootstrap_revision": result.bootstrap_revision,
        "restart_required": result.restart_required,
    }


async def _doctor(request: Request) -> Response:
    state: LocalUiState = request.app.state.local_ui
    await _require_session(request, state)
    return _json(_asdict(await run_in_threadpool(state.management.catalog.doctor)))


async def _index_health(request: Request) -> Response:
    state: LocalUiState = request.app.state.local_ui
    await _require_session(request, state)
    return _json(_asdict(await run_in_threadpool(state.management.index_health.get)))


def _load_static_assets() -> dict[str, tuple[bytes, str]]:
    root = files("mcp_email_server.web_ui").joinpath("static")
    assets: dict[str, tuple[bytes, str]] = {}

    def visit(node: Any, prefix: PurePosixPath) -> None:
        for child in node.iterdir():
            relative = prefix / child.name
            if child.is_dir():
                visit(child, relative)
            elif child.is_file():
                content_type = mimetypes.guess_type(child.name)[0] or "application/octet-stream"
                assets[relative.as_posix()] = (child.read_bytes(), content_type)

    visit(root, PurePosixPath())
    if "index.html" not in assets:
        raise RuntimeError("Packaged local UI assets are missing")
    return assets


def _matched_management_operation(scope: Scope, routes: list[Route]) -> str | None:
    partial_operation: str | None = None
    for route in routes:
        match, _child_scope = route.matches(scope)
        operation = route.name if isinstance(route.name, str) and route.name.startswith("management.") else None
        if match is Match.FULL:
            return operation
        if match is Match.PARTIAL and partial_operation is None:
            partial_operation = operation
    return partial_operation


def _finalize_management_response(
    response: Response,
    *,
    operation: str | None,
    method: str,
    started_at: float,
) -> Response:
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    if "access-control-allow-origin" in response.headers:
        del response.headers["access-control-allow-origin"]
    if "access-control-allow-credentials" in response.headers:
        del response.headers["access-control-allow-credentials"]
    if operation is not None:
        safe_method = method if method in {"GET", "POST"} else "OTHER"
        duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
        logger.info(
            "Local management request operation={} method={} status={} duration_ms={}",
            operation,
            safe_method,
            response.status_code,
            duration_ms,
        )
    return response


def create_local_ui_app(state: LocalUiState) -> Starlette:  # noqa: C901 - explicit closed route inventory
    """Create the explicit loopback-only management ASGI application."""

    prefix = state.route_prefix
    static_assets = _load_static_assets()

    async def static(request: Request) -> Response:
        path = request.path_params.get("asset", "index.html")
        if path == "":
            path = "index.html"
        if not isinstance(path, str) or path not in static_assets:
            return _error(404, "not_found", "The requested management route was not found.")
        content, content_type = static_assets[path]
        return Response(content, media_type=content_type)

    routes = [
        Route(f"{prefix}/api/bootstrap", _bootstrap, methods=["POST"], name="management.bootstrap.exchange"),
        Route(f"{prefix}/api/session", _session_status, methods=["GET"], name="management.session.status"),
        Route(
            f"{prefix}/api/session/logout",
            _logout,
            methods=["POST"],
            name="management.session.logout",
        ),
        Route(f"{prefix}/api/status", _status, methods=["GET"], name="management.status.get"),
        Route(
            f"{prefix}/api/catalog/initialize-default",
            _initialize_default,
            methods=["POST"],
            name="management.catalog.initialize_default",
        ),
        Route(
            f"{prefix}/api/catalog/select",
            _select,
            methods=["POST"],
            name="management.catalog.select",
        ),
        Route(f"{prefix}/api/accounts", _accounts, methods=["GET"], name="management.accounts.list"),
        Route(
            f"{prefix}/api/accounts/create",
            _create_account,
            methods=["POST"],
            name="management.accounts.create",
        ),
        Route(
            f"{prefix}/api/accounts/{{name}}",
            _account,
            methods=["GET"],
            name="management.accounts.show",
        ),
        Route(
            f"{prefix}/api/accounts/{{name}}/update",
            _update_account,
            methods=["POST"],
            name="management.accounts.update",
        ),
        Route(
            f"{prefix}/api/accounts/{{name}}/enable",
            _enable_account,
            methods=["POST"],
            name="management.accounts.enable",
        ),
        Route(
            f"{prefix}/api/accounts/{{name}}/disable",
            _disable_account,
            methods=["POST"],
            name="management.accounts.disable",
        ),
        Route(
            f"{prefix}/api/accounts/{{name}}/remove",
            _remove_account,
            methods=["POST"],
            name="management.accounts.remove",
        ),
        Route(
            f"{prefix}/api/accounts/{{name}}/credentials/{{role}}/set",
            _set_credential,
            methods=["POST"],
            name="management.credentials.set",
        ),
        Route(
            f"{prefix}/api/accounts/{{name}}/credentials/{{role}}/remove",
            _remove_credential,
            methods=["POST"],
            name="management.credentials.remove",
        ),
        Route(
            f"{prefix}/api/credentials/cleanup",
            _cleanup,
            methods=["POST"],
            name="management.credentials.cleanup",
        ),
        Route(f"{prefix}/api/policy", _policy, methods=["GET"], name="management.policy.get"),
        Route(
            f"{prefix}/api/policy/update",
            _update_policy,
            methods=["POST"],
            name="management.policy.update",
        ),
        Route(
            f"{prefix}/api/import/preview",
            _preview_import,
            methods=["POST"],
            name="management.import.preview",
        ),
        Route(
            f"{prefix}/api/import/apply",
            _apply_import,
            methods=["POST"],
            name="management.import.apply",
        ),
        Route(f"{prefix}/api/doctor", _doctor, methods=["GET"], name="management.doctor"),
        Route(
            f"{prefix}/api/index-health",
            _index_health,
            methods=["GET"],
            name="management.index_health",
        ),
        Route(f"{prefix}/", static, methods=["GET"], name="static.index"),
        Route(f"{prefix}/{{asset:path}}", static, methods=["GET"], name="static.asset"),
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await state.close()
            await close_application_runtime()

    async def security_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started_at = time.monotonic()
        operation = _matched_management_operation(request.scope, routes)
        host_rejected = request.headers.get("host") != f"127.0.0.1:{state.port}"
        if host_rejected:
            response = _error(400, "host_rejected", "The request host is not allowed.")
        else:
            try:
                response = await call_next(request)
            except _HttpError as exc:
                response = _error(exc.status_code, exc.category, exc.message)
            except ManagementError as exc:
                if isinstance(exc, RevisionConflictError):
                    response = _error(
                        409,
                        "conflict",
                        "The managed configuration changed. Review the current state before retrying.",
                        current=await _current_summary(request, state),
                    )
                else:
                    response = _error(
                        400,
                        exc.reason,
                        "The management operation could not be completed.",
                    )
            except ValueError:
                response = _error(422, "invalid_request", "The request values are invalid.")
            except Exception:
                response = _error(500, "internal_error", "The management operation could not be completed.")
        if host_rejected:
            operation = "management.security.host_rejected"
        return _finalize_management_response(
            response,
            operation=operation,
            method=request.method,
            started_at=started_at,
        )

    app = Starlette(
        debug=False,
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=security_boundary)],
    )
    app.state.local_ui = state
    return app
