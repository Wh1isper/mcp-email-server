"""Scher Extensions — additional MCP tools layered on top of mcp-email-server.

These tools are registered via :func:`register_scher_tools` which app.py calls
exactly once. Keeping them in this file makes upstream sync trivial:
``mcp_email_server/app.py`` only gains a single import + one call.

See ``PATCH.md`` in the repo root for the full list of upstream-touched files
and the rationale for each extension.
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from mcp_email_server.config import EmailSettings, get_settings
from mcp_email_server.emails.dispatcher import dispatch_handler
from mcp_email_server.log import logger

# Env vars whose values are credentials and must never be echoed.
_SECRET_ENV_VARS = {
    "MCP_EMAIL_SERVER_PASSWORD",
    "MCP_EMAIL_SERVER_IMAP_PASSWORD",
    "MCP_EMAIL_SERVER_SMTP_PASSWORD",
}

# Env vars that diag will report on (presence + masked value).
_REPORTED_ENV_VARS = (
    "MCP_EMAIL_SERVER_CONFIG_PATH",
    "MCP_EMAIL_SERVER_ACCOUNT_NAME",
    "MCP_EMAIL_SERVER_EMAIL_ADDRESS",
    "MCP_EMAIL_SERVER_USER_NAME",
    "MCP_EMAIL_SERVER_IMAP_HOST",
    "MCP_EMAIL_SERVER_IMAP_PORT",
    "MCP_EMAIL_SERVER_IMAP_SSL",
    "MCP_EMAIL_SERVER_IMAP_VERIFY_SSL",
    "MCP_EMAIL_SERVER_SMTP_HOST",
    "MCP_EMAIL_SERVER_SMTP_PORT",
    "MCP_EMAIL_SERVER_SMTP_SSL",
    "MCP_EMAIL_SERVER_SMTP_START_SSL",
    "MCP_EMAIL_SERVER_SMTP_VERIFY_SSL",
    "MCP_EMAIL_SERVER_SAVE_TO_SENT",
    "MCP_EMAIL_SERVER_SENT_FOLDER_NAME",
    "MCP_EMAIL_SERVER_REDIRECT_TO",
    "MCP_EMAIL_SERVER_PASSWORD",
    "MCP_EMAIL_SERVER_IMAP_PASSWORD",
    "MCP_EMAIL_SERVER_SMTP_PASSWORD",
)


def _check(name: str, ok: bool, **fields: Any) -> dict[str, Any]:
    """Build a single diag check entry."""
    result: dict[str, Any] = {"name": name, "ok": ok}
    result.update(fields)
    return result


def _env_overview() -> dict[str, Any]:
    """Snapshot of recognized env vars with secrets masked.

    Never returns the raw value of password-style variables — only a short
    fingerprint ``<set:N>`` showing the length, so we can diagnose typos
    without exposing the secret.
    """
    overview: dict[str, str] = {}
    for var in _REPORTED_ENV_VARS:
        value = os.environ.get(var)
        if value is None:
            overview[var] = "<unset>"
        elif var in _SECRET_ENV_VARS:
            overview[var] = f"<set:{len(value)}>"
        else:
            overview[var] = value
    return overview


async def _tcp_check(host: str, port: int, timeout: float = 10.0) -> dict[str, Any]:
    """Async TCP connect probe. Closes immediately on success."""
    loop = asyncio.get_event_loop()

    def _connect() -> tuple[bool, str]:
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                peer = sock.getpeername()
                return True, f"connected to {peer[0]}:{peer[1]}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    ok, detail = await loop.run_in_executor(None, _connect)
    return {"ok": ok, "detail": detail}


async def _dns_check(host: str) -> dict[str, Any]:
    """Resolve hostname via getaddrinfo."""
    loop = asyncio.get_event_loop()

    def _resolve() -> tuple[bool, str]:
        try:
            infos = socket.getaddrinfo(host, None)
            addrs = sorted({info[4][0] for info in infos})
            return True, f"resolved {len(addrs)} addr(s): {', '.join(addrs[:5])}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    ok, detail = await loop.run_in_executor(None, _resolve)
    return {"ok": ok, "detail": detail}


async def _imap_login_check(account: EmailSettings) -> dict[str, Any]:
    """Open IMAP, login, SELECT INBOX, logout. Returns mailbox-existence info on success."""
    # Defer to the existing EmailClient instead of duplicating connection logic.
    from mcp_email_server.emails.classic import EmailClient

    client = EmailClient(account.incoming)
    imap = client._imap_connect()
    try:
        await asyncio.wait_for(imap._client_task, timeout=30.0)
        await asyncio.wait_for(imap.wait_hello_from_server(), timeout=10.0)
        await asyncio.wait_for(
            imap.login(account.incoming.user_name, account.incoming.password.get_secret_value()),
            timeout=30.0,
        )
        select_result = await asyncio.wait_for(imap.select('"INBOX"'), timeout=30.0)
        status = select_result[0] if isinstance(select_result, tuple) else str(select_result)
        return {"ok": str(status).upper() == "OK", "detail": f"SELECT INBOX → {status}"}
    finally:
        try:
            await asyncio.wait_for(imap.logout(), timeout=10.0)
        except Exception as e:
            logger.debug(f"IMAP logout (diag) failed: {e}")


async def _smtp_login_check(account: EmailSettings) -> dict[str, Any]:
    """Open SMTP, login, NOOP, quit."""
    import aiosmtplib

    from mcp_email_server.emails.classic import _create_ssl_context

    outgoing = account.outgoing
    async with aiosmtplib.SMTP(
        hostname=outgoing.host,
        port=outgoing.port,
        start_tls=outgoing.start_ssl,
        use_tls=outgoing.use_ssl,
        tls_context=_create_ssl_context(outgoing.verify_ssl),
    ) as smtp:
        await smtp.login(outgoing.user_name, outgoing.password.get_secret_value())
        await smtp.noop()
    return {"ok": True, "detail": "login + NOOP succeeded"}


def register_scher_tools(mcp: FastMCP) -> None:  # noqa: C901
    """Register Scher Extensions on an existing FastMCP server.

    Called from ``mcp_email_server.app`` after the upstream tools are bound.
    """

    @mcp.tool(
        description=(
            "Mark one or more emails as read by setting the IMAP \\Seen flag. "
            "Does NOT move or delete — purely a flag operation. Useful for "
            "triage paths that handle messages without relocating them."
        )
    )
    async def mark_seen(
        account_name: Annotated[str, Field(description="The name of the email account.")],
        email_ids: Annotated[
            list[str],
            Field(description="List of email_id (UID) to mark as read."),
        ],
        mailbox: Annotated[str, Field(default="INBOX", description="The mailbox containing the emails.")] = "INBOX",
    ) -> dict[str, Any]:
        handler = dispatch_handler(account_name)
        succeeded, failed = await handler.mark_seen(email_ids, mailbox)
        return {
            "marked_seen": succeeded,
            "failed": failed,
            "mailbox": mailbox,
        }

    @mcp.tool(
        description=(
            "Remove the IMAP \\Seen flag from one or more emails (mark them "
            "unread again). Mirrors mark_seen for symmetry."
        )
    )
    async def mark_unseen(
        account_name: Annotated[str, Field(description="The name of the email account.")],
        email_ids: Annotated[
            list[str],
            Field(description="List of email_id (UID) to mark as unread."),
        ],
        mailbox: Annotated[str, Field(default="INBOX", description="The mailbox containing the emails.")] = "INBOX",
    ) -> dict[str, Any]:
        handler = dispatch_handler(account_name)
        succeeded, failed = await handler.mark_unseen(email_ids, mailbox)
        return {
            "marked_unseen": succeeded,
            "failed": failed,
            "mailbox": mailbox,
        }

    @mcp.tool(
        description=(
            "Idempotently ensure an IMAP folder exists. Tries CREATE; an "
            "already-existing folder is treated as success. Verified via LIST. "
            "Use before move_emails to make sure the destination is there."
        )
    )
    async def ensure_folder(
        account_name: Annotated[str, Field(description="The name of the email account.")],
        folder: Annotated[
            str,
            Field(description="The IMAP folder name to ensure (e.g. 'INBOX/Archive')."),
        ],
    ) -> dict[str, Any]:
        handler = dispatch_handler(account_name)
        return await handler.ensure_folder(folder)

    @mcp.tool(
        description=(
            "Run a connectivity self-test for the given account: env snapshot "
            "(passwords masked), DNS, TCP connect, IMAP login + SELECT INBOX, "
            "SMTP login. Returns a list of per-check results."
        )
    )
    async def diag(
        account_name: Annotated[
            str,
            Field(description="The name of the email account to diagnose."),
        ],
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        # 0. Env overview — always safe to report (secrets masked).
        env = _env_overview()
        checks.append(_check("env_overview", True, env=env))

        # 1. Account lookup. Stop early if not found, since downstream checks
        #    all need the account.
        settings = get_settings()
        account = settings.get_account(account_name)
        if account is None:
            available = [a.account_name for a in settings.get_accounts()]
            checks.append(
                _check(
                    "account_lookup",
                    False,
                    error=f"account {account_name!r} not found",
                    available_accounts=available,
                )
            )
            return {"account": account_name, "checks": checks}

        if not isinstance(account, EmailSettings):
            checks.append(
                _check(
                    "account_lookup",
                    False,
                    error=f"account {account_name!r} is not an EmailSettings (got {type(account).__name__})",
                )
            )
            return {"account": account_name, "checks": checks}

        checks.append(
            _check(
                "account_lookup",
                True,
                account_name=account.account_name,
                email_address=account.email_address,
                imap_host=account.incoming.host,
                imap_port=account.incoming.port,
                smtp_host=account.outgoing.host,
                smtp_port=account.outgoing.port,
            )
        )

        # 2. DNS resolution.
        dns_imap = await _dns_check(account.incoming.host)
        checks.append(_check("dns_imap", dns_imap["ok"], host=account.incoming.host, detail=dns_imap["detail"]))
        dns_smtp = await _dns_check(account.outgoing.host)
        checks.append(_check("dns_smtp", dns_smtp["ok"], host=account.outgoing.host, detail=dns_smtp["detail"]))

        # 3. TCP connect (10s timeout).
        tcp_imap = await _tcp_check(account.incoming.host, account.incoming.port)
        checks.append(
            _check(
                "tcp_imap",
                tcp_imap["ok"],
                host=account.incoming.host,
                port=account.incoming.port,
                detail=tcp_imap["detail"],
            )
        )
        tcp_smtp = await _tcp_check(account.outgoing.host, account.outgoing.port)
        checks.append(
            _check(
                "tcp_smtp",
                tcp_smtp["ok"],
                host=account.outgoing.host,
                port=account.outgoing.port,
                detail=tcp_smtp["detail"],
            )
        )

        # 4. IMAP login + SELECT (skip if TCP failed — no point in piling errors).
        if tcp_imap["ok"]:
            try:
                imap_login = await _imap_login_check(account)
                checks.append(_check("imap_login", imap_login["ok"], detail=imap_login["detail"]))
            except Exception as e:
                checks.append(_check("imap_login", False, error=f"{type(e).__name__}: {e}"))
        else:
            checks.append(_check("imap_login", False, error="skipped: tcp_imap failed"))

        # 5. SMTP login.
        if tcp_smtp["ok"]:
            try:
                smtp_login = await _smtp_login_check(account)
                checks.append(_check("smtp_login", smtp_login["ok"], detail=smtp_login["detail"]))
            except Exception as e:
                checks.append(_check("smtp_login", False, error=f"{type(e).__name__}: {e}"))
        else:
            checks.append(_check("smtp_login", False, error="skipped: tcp_smtp failed"))

        return {"account": account_name, "checks": checks}

    logger.info("Scher Extensions registered: mark_seen, mark_unseen, ensure_folder, diag")
