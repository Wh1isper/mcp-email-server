import asyncio
import enum
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Never

import click
import typer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import SecretStr

from mcp_email_server.app import mcp
from mcp_email_server.application.limits import APPLICATION_LIMITS, validate_controlled_string
from mcp_email_server.application.management import (
    CreateAccountCommand,
    DoctorReport,
    EndpointPatch,
    EndpointSummary,
    LegacyImportPlan,
    ManagedPolicy,
    ManagementError,
    RevisionConflictError,
    UpdateAccountCommand,
    validate_endpoint,
)
from mcp_email_server.runtime import get_application_runtime
from mcp_email_server.stdio import run_bounded_stdio

app = typer.Typer()
config_app = typer.Typer(help="Manage bootstrap mode and the managed catalog.")
account_app = typer.Typer(help="Manage accounts in the configured managed catalog.")
app.add_typer(config_app, name="config")
app.add_typer(account_app, name="account")


class CredentialStorageTarget(enum.StrEnum):
    keyring = "keyring"
    plaintext = "plaintext"


class ConfigMode(enum.StrEnum):
    legacy = "legacy"
    managed = "managed"


class CredentialRepairAction(enum.StrEnum):
    resume = "resume"
    rollback = "rollback"


class ConnectionRole(enum.StrEnum):
    incoming = "incoming"
    outgoing = "outgoing"


LOOPBACK_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
WILDCARD_IPV4_BIND_HOST = "0.0.0.0"  # noqa: S104
WILDCARD_BIND_HOSTS = {WILDCARD_IPV4_BIND_HOST, "::", ""}
FALSE_VALUES = {"0", "false", "no", "off"}
JsonOutput = Annotated[
    bool,
    typer.Option("--json", help="Emit one stable JSON result document for automation."),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(importlib.metadata.version("mcp-email-server"))
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed application version and exit.",
    ),
) -> None:
    """Run the email MCP server or its local management commands."""


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_dns_rebinding_protection_enabled() -> bool:
    value = os.environ.get("MCP_ENABLE_DNS_REBINDING_PROTECTION")
    if value is None:
        return True
    return value.strip().lower() not in FALSE_VALUES


def _normalize_host(host: str) -> str:
    if host == "::1":
        return "[::1]"
    return host


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _expand_allowed_hosts(allowed_hosts: list[str]) -> list[str]:
    expanded: list[str] = []
    for allowed_host in allowed_hosts:
        expanded.append(allowed_host)
        if (":" not in allowed_host and allowed_host != "*") or (
            allowed_host.startswith("[") and allowed_host.endswith("]")
        ):
            expanded.append(f"{allowed_host}:*")
    return _unique(expanded)


def _expand_allowed_origins(allowed_origins: list[str]) -> list[str]:
    expanded: list[str] = []
    for allowed_origin in allowed_origins:
        expanded.append(allowed_origin)
        scheme_separator = "://"
        if scheme_separator in allowed_origin and allowed_origin != "*":
            scheme, host = allowed_origin.split(scheme_separator, maxsplit=1)
            has_port = host.rsplit(":", maxsplit=1)[-1].isdigit() or host.endswith(":*")
            if (":" not in host or (host.startswith("[") and host.endswith("]"))) and not has_port:
                expanded.append(f"{scheme}{scheme_separator}{host}:*")
    return _unique(expanded)


def _default_allowed_hosts(host: str, port: int) -> list[str]:
    allowed_hosts = list(LOOPBACK_ALLOWED_HOSTS)
    normalized_host = _normalize_host(host)

    if normalized_host in {"127.0.0.1", "localhost", "[::1]"} or host in WILDCARD_BIND_HOSTS:
        return allowed_hosts

    allowed_hosts.extend([normalized_host, f"{normalized_host}:{port}", f"{normalized_host}:*"])
    return allowed_hosts


def _default_allowed_origins(host: str, port: int) -> list[str]:
    allowed_origins = list(LOOPBACK_ALLOWED_ORIGINS)
    normalized_host = _normalize_host(host)

    if normalized_host in {"127.0.0.1", "localhost", "[::1]"} or host in WILDCARD_BIND_HOSTS:
        return allowed_origins

    allowed_origins.extend([
        f"http://{normalized_host}",
        f"http://{normalized_host}:{port}",
        f"http://{normalized_host}:*",
        f"https://{normalized_host}",
        f"https://{normalized_host}:{port}",
        f"https://{normalized_host}:*",
    ])
    return allowed_origins


def _build_transport_security_settings(host: str, port: int) -> TransportSecuritySettings:
    allowed_hosts = _split_csv(os.environ.get("MCP_ALLOWED_HOSTS"))
    allowed_origins = _split_csv(os.environ.get("MCP_ALLOWED_ORIGINS"))

    if not _is_dns_rebinding_protection_enabled() or "*" in allowed_hosts or "*" in allowed_origins:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_expand_allowed_hosts(allowed_hosts) if allowed_hosts else _default_allowed_hosts(host, port),
        allowed_origins=_expand_allowed_origins(allowed_origins)
        if allowed_origins
        else _default_allowed_origins(host, port),
    )


def _configure_http_transport(host: str, port: int) -> None:
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.transport_security = _build_transport_security_settings(host, port)


def _json_requested() -> bool:
    context = click.get_current_context(silent=True)
    return bool(context is not None and context.params.get("json_output", False))


def _command_id() -> str:
    names: list[str] = []
    context = click.get_current_context(silent=True)
    while context is not None and context.parent is not None:
        if context.info_name:
            names.append(context.info_name)
        context = context.parent
    return ".".join(reversed(names))


def _echo_json(
    command: str,
    data: dict[str, object],
    *,
    warnings: list[dict[str, str]] | None = None,
) -> None:
    typer.echo(
        json.dumps(
            {
                "schema_version": 1,
                "ok": True,
                "command": command,
                "data": data,
                "warnings": warnings or [],
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    )


def _bounded_error_message(exc: Exception) -> str:
    raw = str(exc)
    encoded = raw.encode("utf-8")
    limit = APPLICATION_LIMITS.error_detail_bytes
    if len(encoded) <= limit:
        return raw
    suffix = "..."
    return encoded[: limit - len(suffix)].decode("utf-8", errors="ignore") + suffix


def _fail_cli(exc: Exception, *, error_code: str | None = None) -> Never:
    if _json_requested():
        details: dict[str, object] = {}
        if error_code is not None:
            code = error_code
        elif isinstance(exc, RevisionConflictError):
            code = "revision_conflict"
            details["aggregate"] = exc.aggregate
            if exc.name is not None:
                details["name"] = exc.name
        elif isinstance(exc, ValueError):
            code = "invalid_input"
        elif isinstance(exc, ManagementError):
            code = "management_error"
        else:
            code = "runtime_error"
        typer.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": False,
                    "command": _command_id(),
                    "error": {"code": code, "message": _bounded_error_message(exc), "details": details},
                    "warnings": [],
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(f"Error: {_bounded_error_message(exc)}", err=True)
    raise typer.Exit(code=1) from exc


def _doctor_data(report: object) -> dict[str, object]:
    if not isinstance(report, DoctorReport):
        raise TypeError("Expected DoctorReport")
    return {
        "lifecycle": report.lifecycle,
        "schema_version": report.schema_version,
        "catalog_revision": report.catalog_revision,
        "accounts": report.account_count,
        "enabled_accounts": report.enabled_account_count,
        "pending_bindings": report.pending_bindings,
        "cleanup_required_bindings": report.cleanup_required_bindings,
        "repair_required_bindings": report.repair_required_bindings,
        "problems": list(report.problems),
    }


def _endpoint_data(endpoint: EndpointSummary) -> dict[str, object]:
    return {
        "host": endpoint.host,
        "port": endpoint.port,
        "user_name": endpoint.user_name,
        "use_ssl": endpoint.use_ssl,
        "start_ssl": endpoint.start_ssl,
        "verify_ssl": endpoint.verify_ssl,
    }


def _policy_data(policy: ManagedPolicy) -> dict[str, object]:
    return {
        "revision": policy.revision,
        "enable_attachment_download": policy.enable_attachment_download,
        "allowed_recipients": list(policy.allowed_recipients),
        "allowed_senders": list(policy.allowed_senders),
        "report_blocked_mutations": policy.report_blocked_mutations,
    }


def _legacy_import_plan_data(plan: LegacyImportPlan) -> dict[str, object]:
    accounts: list[dict[str, object]] = []
    for item in plan.accounts:
        source = item.source
        accounts.append({
            "name": item.name,
            "action": item.action,
            "expected_target_revision": item.expected_target_revision,
            "missing_credentials": list(item.missing_credentials),
            "identity": {
                "full_name": source.full_name,
                "email_address": source.email_address,
                "save_to_sent": source.save_to_sent,
                "sent_folder_name": source.sent_folder_name,
            },
            "incoming": {
                **_endpoint_data(source.incoming),
                "secret_source": source.incoming_secret_source,
            },
            "outgoing": (
                {
                    **_endpoint_data(source.outgoing),
                    "secret_source": source.outgoing_secret_source,
                }
                if source.outgoing is not None
                else None
            ),
        })
    return {
        "source_fingerprint": plan.source_fingerprint,
        "target_revision": plan.target_revision,
        "target_policy_revision": plan.target_policy_revision,
        "has_conflicts": plan.has_conflicts,
        "accounts": accounts,
        "policy": {
            "action": plan.policy_action,
            "enable_attachment_download": plan.source_policy.enable_attachment_download,
            "allowed_recipients": list(plan.source_policy.allowed_recipients),
            "allowed_senders": list(plan.source_policy.allowed_senders),
            "report_blocked_mutations": plan.source_policy.report_blocked_mutations,
        },
        "unsupported_provider_names": list(plan.unsupported_provider_names),
    }


def _read_secret(label: str, *, from_stdin: bool) -> str:
    if from_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    else:
        value = typer.prompt(label, hide_input=True, confirmation_prompt=False)
    if not value:
        raise ManagementError(f"{label} must not be empty")
    return value


def _endpoint_patch(
    *,
    host: str | None,
    port: int | None,
    user_name: str | None,
    use_ssl: bool | None,
    start_ssl: bool | None,
    verify_ssl: bool | None,
) -> EndpointPatch | None:
    values = (host, port, user_name, use_ssl, start_ssl, verify_ssl)
    if all(value is None for value in values):
        return None
    return EndpointPatch(
        host=host,
        port=port,
        user_name=user_name,
        use_ssl=use_ssl,
        start_ssl=start_ssl,
        verify_ssl=verify_ssl,
    )


@config_app.command("init")
def config_init(
    database: Path = typer.Option(..., "--database", help="Path for the new managed SQLite catalog."),  # noqa: B008
    json_output: JsonOutput = False,
) -> None:
    """Create a STAGING managed catalog without selecting managed mode."""
    try:
        get_application_runtime().management.lifecycle.initialize(database)
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "config.init",
            {
                "lifecycle": "STAGING",
                "next_steps": ["account.add", "account.test", "config.activate", "config.select"],
            },
        )
        return
    typer.echo(
        "Created STAGING managed catalog. Next: add an account, test it, run `config activate`, "
        "then `config select managed` and restart MCP clients."
    )


@config_app.command("status")
def config_status(json_output: JsonOutput = False) -> None:
    """Show selected mode and bounded managed catalog status."""
    try:
        status = get_application_runtime().management.lifecycle.status()
        catalog_status = (
            "unavailable"
            if status.catalog_problem is not None
            else "available"
            if status.selected_catalog is not None
            else "not_configured"
        )
        if json_output:
            data: dict[str, object] = {
                "mode": status.mode,
                "bootstrap_revision": status.bootstrap_revision,
                "catalog_status": catalog_status,
                "restart_required": status.restart_required,
                "report": _doctor_data(status.report) if status.report is not None else None,
            }
            if status.catalog_problem is not None:
                data["catalog_problem"] = status.catalog_problem
            hints = (
                [
                    {
                        "code": "user_setup_required",
                        "message": "Ask the user to complete account setup in their own terminal or local UI.",
                    }
                ]
                if catalog_status == "not_configured"
                else []
            )
            data["hints"] = hints
            _echo_json("config.status", data)
            return
        typer.echo(f"mode={status.mode}")
        typer.echo(f"bootstrap_revision={status.bootstrap_revision}")
        if status.catalog_problem is not None:
            typer.echo("catalog_status=unavailable")
            typer.echo(f"catalog_problem={status.catalog_problem}")
        elif status.selected_catalog is not None:
            typer.echo("catalog_status=available")
        else:
            typer.echo("catalog_status=not_configured")
        typer.echo(f"restart_required={str(status.restart_required).lower()}")
        if status.report is not None:
            typer.echo(f"lifecycle={status.report.lifecycle}")
            typer.echo(f"accounts={status.report.account_count}")
            typer.echo(f"enabled_accounts={status.report.enabled_account_count}")
    except ManagementError as exc:
        _fail_cli(exc)


@config_app.command("doctor")
def config_doctor(json_output: JsonOutput = False) -> None:
    """Report bounded catalog, binding, and cleanup health without locators."""
    try:
        report = get_application_runtime().management.lifecycle.doctor()
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("config.doctor", _doctor_data(report))
        return
    typer.echo(f"lifecycle={report.lifecycle}")
    typer.echo(f"schema_version={report.schema_version}")
    typer.echo(f"catalog_revision={report.catalog_revision}")
    typer.echo(f"accounts={report.account_count}")
    typer.echo(f"enabled_accounts={report.enabled_account_count}")
    typer.echo(f"pending_bindings={report.pending_bindings}")
    typer.echo(f"cleanup_required_bindings={report.cleanup_required_bindings}")
    typer.echo(f"repair_required_bindings={report.repair_required_bindings}")
    typer.echo("problems=" + (",".join(report.problems) if report.problems else "none"))


@config_app.command("index-health")
def config_index_health(json_output: JsonOutput = False) -> None:
    """Show bounded rebuildable metadata projection health."""
    try:
        health = get_application_runtime().management.index_health.get()
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "config.index-health",
            {
                "status": health.status,
                "indexed_accounts": health.indexed_accounts,
                "pending_operations": health.pending_operations,
                "problems": list(health.problems),
            },
        )
        return
    typer.echo(f"status={health.status}")
    typer.echo(f"indexed_accounts={health.indexed_accounts}")
    typer.echo(f"pending_operations={health.pending_operations}")
    typer.echo("problems=" + (",".join(health.problems) if health.problems else "none"))


@config_app.command("policy")
def config_policy(json_output: JsonOutput = False) -> None:
    """Show the canonical managed catalog policy and revision."""
    try:
        policy = get_application_runtime().management.policy.get()
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("config.policy", _policy_data(policy))
        return
    typer.echo(f"revision={policy.revision}")
    typer.echo(f"enable_attachment_download={str(policy.enable_attachment_download).lower()}")
    typer.echo("allowed_recipients=" + (",".join(policy.allowed_recipients) or "none"))
    typer.echo("allowed_senders=" + (",".join(policy.allowed_senders) or "none"))
    typer.echo(f"report_blocked_mutations={str(policy.report_blocked_mutations).lower()}")


@config_app.command("update-policy")
def config_update_policy(
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    enable_attachment_download: bool | None = typer.Option(
        None,
        "--enable-attachment-download/--disable-attachment-download",
    ),
    allowed_recipients: str | None = typer.Option(
        None,
        "--allowed-recipients",
        help="Comma-separated addresses; pass an empty value to clear.",
    ),
    allowed_senders: str | None = typer.Option(
        None,
        "--allowed-senders",
        help="Comma-separated patterns; pass an empty value to clear.",
    ),
    report_blocked_mutations: bool | None = typer.Option(
        None,
        "--report-blocked-mutations/--no-report-blocked-mutations",
    ),
    json_output: JsonOutput = False,
) -> None:
    """Revision-update managed policy, preserving options that are omitted."""
    try:
        service = get_application_runtime().management.policy
        current = service.get()
        policy = service.update(
            ManagedPolicy(
                revision=expected_revision,
                enable_attachment_download=(
                    current.enable_attachment_download
                    if enable_attachment_download is None
                    else enable_attachment_download
                ),
                allowed_recipients=(
                    current.allowed_recipients if allowed_recipients is None else tuple(_split_csv(allowed_recipients))
                ),
                allowed_senders=(
                    current.allowed_senders if allowed_senders is None else tuple(_split_csv(allowed_senders))
                ),
                report_blocked_mutations=(
                    current.report_blocked_mutations if report_blocked_mutations is None else report_blocked_mutations
                ),
            )
        )
    except (ManagementError, ValueError) as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("config.update-policy", _policy_data(policy))
        return
    typer.echo(f"Updated managed policy at revision {policy.revision}.")


@config_app.command("cleanup-credentials")
def config_cleanup_credentials(
    limit: int = typer.Option(100, "--limit", min=1, max=100),
    json_output: JsonOutput = False,
) -> None:
    """Best-effort cleanup of bounded stale candidate locators."""
    try:
        management = get_application_runtime().management
        expected_revision = management.lifecycle.doctor().catalog_revision
        report = management.credentials.cleanup(
            limit=limit,
            expected_revision=expected_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "config.cleanup-credentials",
            {"examined": report.examined, "cleaned": report.cleaned, "remaining": report.remaining},
        )
        return
    typer.echo(f"examined={report.examined}")
    typer.echo(f"cleaned={report.cleaned}")
    typer.echo(f"remaining={report.remaining}")


def _echo_legacy_import_plan(plan: LegacyImportPlan) -> None:
    typer.echo(f"source_fingerprint={plan.source_fingerprint}")
    typer.echo(f"target_revision={plan.target_revision}")
    for item in plan.accounts:
        credential_work = ",".join(item.missing_credentials) if item.missing_credentials else "none"
        target_revision = item.expected_target_revision if item.expected_target_revision is not None else "absent"
        typer.echo(
            f"account={item.name} action={item.action} credentials={credential_work} target_revision={target_revision}"
        )
        source = item.source
        typer.echo(
            f"  identity={source.email_address} full_name={source.full_name!r} "
            f"save_to_sent={str(source.save_to_sent).lower()} sent_folder={source.sent_folder_name or 'default'}"
        )
        incoming = source.incoming
        typer.echo(
            f"  incoming={incoming.host}:{incoming.port} user={incoming.user_name} "
            f"ssl={str(incoming.use_ssl).lower()} starttls={str(incoming.start_ssl).lower()} "
            f"verify_ssl={str(incoming.verify_ssl).lower()} secret_source={source.incoming_secret_source}"
        )
        if source.outgoing is not None:
            outgoing = source.outgoing
            typer.echo(
                f"  outgoing={outgoing.host}:{outgoing.port} user={outgoing.user_name} "
                f"ssl={str(outgoing.use_ssl).lower()} starttls={str(outgoing.start_ssl).lower()} "
                f"verify_ssl={str(outgoing.verify_ssl).lower()} secret_source={source.outgoing_secret_source}"
            )
        else:
            typer.echo("  outgoing=none")
    policy = plan.source_policy
    typer.echo(f"policy={plan.policy_action} target_revision={plan.target_policy_revision}")
    typer.echo(f"  attachment_download={str(policy.enable_attachment_download).lower()}")
    typer.echo("  allowed_recipients=" + (",".join(policy.allowed_recipients) or "none"))
    typer.echo("  allowed_senders=" + (",".join(policy.allowed_senders) or "none"))
    typer.echo(f"  report_blocked_mutations={str(policy.report_blocked_mutations).lower()}")
    for provider_name in plan.unsupported_provider_names:
        typer.echo(f"provider={provider_name} action=unsupported")


@config_app.command("import-legacy")
def config_import_legacy(
    apply: bool = typer.Option(False, "--apply", help="Review, confirm, and apply the import to STAGING."),
    json_output: JsonOutput = False,
) -> None:
    """Preview or interactively apply stored TOML accounts without environment overlays."""
    if apply and json_output:
        _fail_cli(ManagementError("--json cannot be combined with interactive --apply; review and apply in text mode"))
    try:
        service = get_application_runtime().management.legacy_import
        plan = service.preview()
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("config.import-legacy", _legacy_import_plan_data(plan))
        return
    typer.echo("mode=" + ("apply-preview" if apply else "preview"))
    _echo_legacy_import_plan(plan)
    if not apply:
        return
    confirmation = typer.prompt("Type IMPORT to apply this exact preview")
    try:
        report = service.apply(
            preview_token=plan.preview_token,
            expected_revision=plan.target_revision,
            confirmation=confirmation,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo("created=" + (",".join(report.created) if report.created else "none"))
    typer.echo("resumed=" + (",".join(report.resumed) if report.resumed else "none"))


@config_app.command("activate")
def config_activate(json_output: JsonOutput = False) -> None:
    """Validate the complete STAGING snapshot and mark it ACTIVE."""
    try:
        lifecycle = get_application_runtime().management.lifecycle
        lifecycle.activate(expected_revision=lifecycle.doctor().catalog_revision)
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "config.activate",
            {"lifecycle": "ACTIVE", "next_steps": ["config.select", "restart_mcp_clients"]},
        )
        return
    typer.echo("Managed catalog is ACTIVE. Select managed mode separately when ready.")


@config_app.command("select")
def config_select(mode: ConfigMode, json_output: JsonOutput = False) -> None:
    """Atomically select legacy or an already ACTIVE managed catalog."""
    try:
        lifecycle = get_application_runtime().management.lifecycle
        status = lifecycle.status()
        expected_catalog_revision = status.report.catalog_revision if status.report is not None else None
        lifecycle.select(
            mode.value,
            expected_bootstrap_revision=status.bootstrap_revision,
            expected_catalog_revision=expected_catalog_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("config.select", {"mode": mode.value, "restart_required": True})
        return
    typer.echo(f"Selected {mode.value} mode. Restart all MCP server processes for the change to take effect.")


@account_app.command("add")
def account_add(
    name: str,
    email_address: str = typer.Option(..., "--email"),
    full_name: str = typer.Option(..., "--full-name"),
    imap_host: str = typer.Option(..., "--imap-host"),
    imap_port: int = typer.Option(993, "--imap-port", min=1, max=65535),
    imap_user: str | None = typer.Option(None, "--imap-user"),
    imap_ssl: bool = typer.Option(True, "--imap-ssl/--no-imap-ssl"),
    imap_starttls: bool = typer.Option(False, "--imap-starttls/--no-imap-starttls"),
    imap_verify_ssl: bool = typer.Option(True, "--imap-verify-ssl/--no-imap-verify-ssl"),
    smtp_host: str | None = typer.Option(None, "--smtp-host"),
    smtp_port: int = typer.Option(465, "--smtp-port", min=1, max=65535),
    smtp_user: str | None = typer.Option(None, "--smtp-user"),
    smtp_ssl: bool = typer.Option(True, "--smtp-ssl/--no-smtp-ssl"),
    smtp_starttls: bool = typer.Option(False, "--smtp-starttls/--no-smtp-starttls"),
    smtp_verify_ssl: bool = typer.Option(True, "--smtp-verify-ssl/--no-smtp-verify-ssl"),
    password_stdin: bool = typer.Option(False, "--password-stdin", help="Read secrets as lines from stdin."),
    save_to_sent: bool = typer.Option(True, "--save-to-sent/--no-save-to-sent"),
    sent_folder: str | None = typer.Option(None, "--sent-folder"),
    json_output: JsonOutput = False,
) -> None:
    """Add one managed account; secrets are prompted or read from stdin, never argv."""
    if json_output and not password_stdin:
        _fail_cli(ManagementError("--json requires --password-stdin so prompts cannot corrupt JSON output"))
    try:
        incoming = EndpointSummary(
            user_name=imap_user or email_address,
            host=imap_host,
            port=imap_port,
            use_ssl=imap_ssl,
            start_ssl=imap_starttls,
            verify_ssl=imap_verify_ssl,
        )
        outgoing = (
            EndpointSummary(
                user_name=smtp_user or email_address,
                host=smtp_host,
                port=smtp_port,
                use_ssl=smtp_ssl,
                start_ssl=smtp_starttls,
                verify_ssl=smtp_verify_ssl,
            )
            if smtp_host is not None
            else None
        )
        management = get_application_runtime().management
        expected_catalog_revision = management.lifecycle.doctor().catalog_revision
        validate_controlled_string(
            name,
            field_name="account name",
            maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
        )
        validate_controlled_string(
            full_name,
            field_name="full name",
            maximum_bytes=APPLICATION_LIMITS.query_bytes,
        )
        validate_controlled_string(
            email_address,
            field_name="email address",
            maximum_bytes=APPLICATION_LIMITS.address_bytes,
        )
        if sent_folder is not None:
            validate_controlled_string(
                sent_folder,
                field_name="sent folder",
                maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
            )
        validate_endpoint(incoming, role="incoming")
        if outgoing is not None:
            validate_endpoint(outgoing, role="outgoing")
        incoming_password = _read_secret("Incoming password", from_stdin=password_stdin)
        outgoing_password = (
            _read_secret("Outgoing password", from_stdin=password_stdin) if outgoing is not None else None
        )
        result = management.accounts.create(
            CreateAccountCommand(
                expected_catalog_revision=expected_catalog_revision,
                name=name,
                full_name=full_name,
                email_address=email_address,
                incoming=incoming,
                incoming_secret=SecretStr(incoming_password),
                outgoing=outgoing,
                outgoing_secret=SecretStr(outgoing_password) if outgoing_password is not None else None,
                save_to_sent=save_to_sent,
                sent_folder_name=sent_folder,
            )
        )
    except (ManagementError, ValueError) as exc:
        _fail_cli(exc)
    if json_output:
        bindings = {
            "incoming": {
                "status": result.incoming.status,
                "revision": result.incoming.revision,
                "cleanup_required": result.incoming.cleanup_required,
            },
            "outgoing": (
                {
                    "status": result.outgoing.status,
                    "revision": result.outgoing.revision,
                    "cleanup_required": result.outgoing.cleanup_required,
                }
                if result.outgoing is not None
                else None
            ),
        }
        _echo_json("account.add", {"name": name, "bindings": bindings})
        return
    states = [f"incoming={result.incoming.status}"]
    if result.outgoing is not None:
        states.append(f"outgoing={result.outgoing.status}")
    typer.echo(f"Added managed account '{name}' ({', '.join(states)}).")
    repairs = [
        role
        for role, binding in (("incoming", result.incoming), ("outgoing", result.outgoing))
        if binding is not None and binding.status == "pending_repair_required"
    ]
    for role in repairs:
        typer.echo(
            f"Next: inspect `account show {name}` and run `account repair-secret {name} {role} resume --expected-revision REVISION`."
        )


@account_app.command("set-secret")
def account_set_secret(
    name: str,
    role: ConnectionRole,
    password_stdin: bool = typer.Option(False, "--password-stdin", help="Read the secret from stdin."),
    json_output: JsonOutput = False,
) -> None:
    """Install or rotate one managed credential through an immutable candidate."""
    if json_output and not password_stdin:
        _fail_cli(ManagementError("--json requires --password-stdin so prompts cannot corrupt JSON output"))
    try:
        management = get_application_runtime().management
        account = management.accounts.show(name)
        if role is ConnectionRole.outgoing and account.outgoing is None:
            _fail_cli(ManagementError("Outgoing endpoint is unavailable; configure SMTP before setting its credential"))
        expected_revision = account.revision
        secret = _read_secret(f"{role.value.title()} password", from_stdin=password_stdin)
        result = management.credentials.set(
            name,
            role.value,
            secret,
            expected_revision=expected_revision,
        )
    except (ManagementError, ValueError) as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "account.set-secret",
            {
                "name": name,
                "role": role.value,
                "state": result.status,
                "revision": result.revision,
                "cleanup_required": result.cleanup_required,
            },
        )
        return
    typer.echo(
        f"Credential result for '{name}' role={role.value}: {result.status}"
        + (f" cleanup_required={result.cleanup_required}" if result.cleanup_required else "")
    )


@account_app.command("list")
def account_list(json_output: JsonOutput = False) -> None:
    """List non-secret managed account summaries."""
    try:
        accounts = get_application_runtime().management.accounts.list()
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "account.list",
            {
                "accounts": [
                    {
                        "name": account.name,
                        "email_address": account.email_address,
                        "enabled": account.enabled,
                        "revision": account.revision,
                        "has_outgoing": account.has_outgoing,
                        "incoming_binding": account.incoming_binding,
                        "outgoing_binding": account.outgoing_binding,
                    }
                    for account in accounts
                ]
            },
        )
        return
    if not accounts:
        typer.echo("No managed accounts configured.")
        return
    for account in accounts:
        typer.echo(
            f"{account.name}\temail={account.email_address}\tenabled={str(account.enabled).lower()}\t"
            f"incoming={account.incoming_binding}\toutgoing={account.outgoing_binding or 'NONE'}"
        )


@account_app.command("show")
def account_show(name: str, json_output: JsonOutput = False) -> None:
    """Show one non-secret managed account summary."""
    try:
        account = get_application_runtime().management.accounts.show(name)
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "account.show",
            {
                "name": account.name,
                "full_name": account.full_name,
                "email_address": account.email_address,
                "enabled": account.enabled,
                "revision": account.revision,
                "save_to_sent": account.save_to_sent,
                "sent_folder_name": account.sent_folder_name,
                "incoming": _endpoint_data(account.incoming),
                "outgoing": _endpoint_data(account.outgoing) if account.outgoing is not None else None,
                "incoming_binding": account.incoming_binding,
                "outgoing_binding": account.outgoing_binding,
            },
        )
        return
    typer.echo(f"name={account.name}")
    typer.echo(f"full_name={account.full_name}")
    typer.echo(f"email={account.email_address}")
    typer.echo(f"enabled={str(account.enabled).lower()}")
    typer.echo(f"revision={account.revision}")
    typer.echo(f"save_to_sent={str(account.save_to_sent).lower()}")
    typer.echo(f"sent_folder={account.sent_folder_name or 'default'}")
    typer.echo(f"incoming_host={account.incoming.host}")
    typer.echo(f"incoming_port={account.incoming.port}")
    typer.echo(f"incoming_user={account.incoming.user_name}")
    typer.echo(f"incoming_ssl={str(account.incoming.use_ssl).lower()}")
    typer.echo(f"incoming_starttls={str(account.incoming.start_ssl).lower()}")
    typer.echo(f"incoming_verify_ssl={str(account.incoming.verify_ssl).lower()}")
    typer.echo(f"incoming_binding={account.incoming_binding}")
    if account.outgoing is not None:
        typer.echo(f"outgoing_host={account.outgoing.host}")
        typer.echo(f"outgoing_port={account.outgoing.port}")
        typer.echo(f"outgoing_user={account.outgoing.user_name}")
        typer.echo(f"outgoing_ssl={str(account.outgoing.use_ssl).lower()}")
        typer.echo(f"outgoing_starttls={str(account.outgoing.start_ssl).lower()}")
        typer.echo(f"outgoing_verify_ssl={str(account.outgoing.verify_ssl).lower()}")
    typer.echo(f"outgoing_binding={account.outgoing_binding or 'NONE'}")


@account_app.command("update")
def account_update(
    name: str,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    new_name: str | None = typer.Option(None, "--name"),
    full_name: str | None = typer.Option(None, "--full-name"),
    email_address: str | None = typer.Option(None, "--email"),
    imap_host: str | None = typer.Option(None, "--imap-host"),
    imap_port: int | None = typer.Option(None, "--imap-port", min=1, max=65535),
    imap_user: str | None = typer.Option(None, "--imap-user"),
    imap_ssl: bool | None = typer.Option(None, "--imap-ssl/--no-imap-ssl"),
    imap_starttls: bool | None = typer.Option(None, "--imap-starttls/--no-imap-starttls"),
    imap_verify_ssl: bool | None = typer.Option(None, "--imap-verify-ssl/--no-imap-verify-ssl"),
    smtp_host: str | None = typer.Option(None, "--smtp-host"),
    smtp_port: int | None = typer.Option(None, "--smtp-port", min=1, max=65535),
    smtp_user: str | None = typer.Option(None, "--smtp-user"),
    smtp_ssl: bool | None = typer.Option(None, "--smtp-ssl/--no-smtp-ssl"),
    smtp_starttls: bool | None = typer.Option(None, "--smtp-starttls/--no-smtp-starttls"),
    smtp_verify_ssl: bool | None = typer.Option(None, "--smtp-verify-ssl/--no-smtp-verify-ssl"),
    remove_outgoing: bool = typer.Option(False, "--remove-outgoing"),
    save_to_sent: bool | None = typer.Option(None, "--save-to-sent/--no-save-to-sent"),
    sent_folder: str | None = typer.Option(None, "--sent-folder"),
    clear_sent_folder: bool = typer.Option(False, "--clear-sent-folder"),
    json_output: JsonOutput = False,
) -> None:
    """Update account fields and endpoints using an optimistic revision."""
    if sent_folder is not None and clear_sent_folder:
        _fail_cli(ManagementError("--sent-folder and --clear-sent-folder are mutually exclusive"))
    try:
        revision = get_application_runtime().management.accounts.update(
            UpdateAccountCommand(
                name=name,
                expected_revision=expected_revision,
                new_name=new_name,
                full_name=full_name,
                email_address=email_address,
                incoming=_endpoint_patch(
                    host=imap_host,
                    port=imap_port,
                    user_name=imap_user,
                    use_ssl=imap_ssl,
                    start_ssl=imap_starttls,
                    verify_ssl=imap_verify_ssl,
                ),
                outgoing=_endpoint_patch(
                    host=smtp_host,
                    port=smtp_port,
                    user_name=smtp_user,
                    use_ssl=smtp_ssl,
                    start_ssl=smtp_starttls,
                    verify_ssl=smtp_verify_ssl,
                ),
                remove_outgoing=remove_outgoing,
                save_to_sent=save_to_sent,
                sent_folder_name=sent_folder,
                update_sent_folder=sent_folder is not None or clear_sent_folder,
            )
        )
    except (ManagementError, ValueError) as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("account.update", {"name": new_name or name, "revision": revision})
        return
    typer.echo(f"Updated managed account at revision {revision}.")


@account_app.command("disable")
def account_disable(
    name: str,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    json_output: JsonOutput = False,
) -> None:
    """Disable an account using its last observed revision."""
    try:
        revision = get_application_runtime().management.accounts.disable(
            name,
            expected_revision=expected_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("account.disable", {"name": name, "enabled": False, "revision": revision})
        return
    typer.echo(f"Disabled managed account '{name}' at revision {revision}.")


@account_app.command("enable")
def account_enable(
    name: str,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    json_output: JsonOutput = False,
) -> None:
    """Re-enable a complete account after validating its active secrets."""
    try:
        revision = get_application_runtime().management.accounts.enable(
            name,
            expected_revision=expected_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("account.enable", {"name": name, "enabled": True, "revision": revision})
        return
    typer.echo(f"Enabled managed account '{name}' at revision {revision}.")


@account_app.command("remove")
def account_remove(
    name: str,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    confirm: str = typer.Option(..., "--confirm", help="Repeat the exact account name."),
    json_output: JsonOutput = False,
) -> None:
    """Soft-remove an account while retaining identity and cleanup state."""
    try:
        result = get_application_runtime().management.accounts.soft_remove(
            name,
            expected_revision=expected_revision,
            confirmation=confirm,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "account.remove",
            {
                "name": name,
                "revision": result.revision,
                "credentials_examined": result.credentials_examined,
                "credentials_cleaned": result.credentials_cleaned,
                "cleanup_required": result.cleanup_required,
            },
        )
        return
    typer.echo(
        f"Soft-removed managed account '{name}' at revision {result.revision}; "
        f"credentials cleaned={result.credentials_cleaned}, cleanup_required={result.cleanup_required}."
    )


@account_app.command("repair-secret")
def account_repair_secret(
    name: str,
    role: ConnectionRole,
    action: CredentialRepairAction,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    json_output: JsonOutput = False,
) -> None:
    """Explicitly resume or roll back one ambiguous credential candidate."""
    try:
        result = get_application_runtime().management.credentials.repair(
            name,
            role.value,
            action=action.value,
            expected_revision=expected_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "account.repair-secret",
            {
                "name": name,
                "role": role.value,
                "state": result.status,
                "revision": result.revision,
                "cleanup_required": result.cleanup_required,
            },
        )
        return
    typer.echo(f"state={result.status}")
    typer.echo(f"revision={result.revision}")
    typer.echo(f"cleanup_required={result.cleanup_required}")


@account_app.command("remove-secret")
def account_remove_secret(
    name: str,
    role: ConnectionRole,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    json_output: JsonOutput = False,
) -> None:
    """Detach and remove one credential from a disabled account."""
    try:
        result = get_application_runtime().management.credentials.remove(
            name,
            role.value,
            expected_revision=expected_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json(
            "account.remove-secret",
            {
                "name": name,
                "role": role.value,
                "state": result.status,
                "revision": result.revision,
                "cleanup_required": result.cleanup_required,
            },
        )
        return
    typer.echo(f"state={result.status}")
    typer.echo(f"revision={result.revision}")
    typer.echo(f"cleanup_required={result.cleanup_required}")


@account_app.command("test")
def account_test(
    name: str,
    role: Annotated[ConnectionRole, typer.Argument()] = ConnectionRole.incoming,
    json_output: JsonOutput = False,
) -> None:
    """Test managed IMAP or SMTP connectivity outside SQLite transactions."""
    try:
        result = asyncio.run(get_application_runtime().management.connectivity.execute(name, role.value))
    except (ManagementError, ValueError) as exc:
        _fail_cli(exc)
    if result.status != "ok":
        _fail_cli(ManagementError(result.message), error_code=result.category or "connectivity_failed")
    if json_output:
        _echo_json(
            "account.test",
            {"name": name, "role": role.value, "status": result.status, "message": result.message},
        )
        return
    typer.echo(f"{role.value.title()} connectivity test passed for '{name}'.")


def _validate_managed_runtime() -> None:
    """Fail before opening a transport when selected managed authority is unusable."""
    try:
        get_application_runtime().management.legacy_compatibility.validate_runtime()
    except ManagementError as exc:
        _fail_cli(exc)


@app.command()
def stdio() -> None:
    """Run the bounded local MCP stdio transport."""
    _validate_managed_runtime()
    run_bounded_stdio(mcp)


@app.command()
def sse(
    host: str = typer.Option("localhost", "--host", help="HTTP bind host."),
    port: int = typer.Option(9557, "--port", min=1, max=65535, help="HTTP bind port."),
) -> None:
    """Run the compatibility SSE transport with explicit HTTP trust boundaries."""
    _validate_managed_runtime()
    _configure_http_transport(host, port)
    mcp.run(transport="sse")


@app.command()
def streamable_http(
    host: str = typer.Option(os.environ.get("MCP_HOST", "localhost"), "--host", help="HTTP bind host."),
    port: int = typer.Option(
        int(os.environ.get("MCP_PORT", 9557)),
        "--port",
        min=1,
        max=65535,
        help="HTTP bind port.",
    ),
) -> None:
    """Run the compatibility Streamable HTTP transport with explicit trust boundaries."""
    _validate_managed_runtime()
    _configure_http_transport(host, port)
    mcp.run(transport="streamable-http")


@app.command()
def ui(
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the bootstrap URL in a browser."),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Loopback port; 0 selects an ephemeral port."),
) -> None:
    """Run the foreground loopback-only management UI."""
    from mcp_email_server.web_ui import run_local_ui

    try:
        run_local_ui(no_open=no_open, port=port)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail_cli(exc)


@app.command()
def reset(
    confirm: str = typer.Option(..., "--confirm", help="Type RESET to delete persistent legacy configuration."),
    json_output: JsonOutput = False,
) -> None:
    """Delete all persistent legacy accounts and best-effort clean their keyring entries."""
    if confirm != "RESET":
        _fail_cli(ManagementError("Reset confirmation must be exactly RESET"))
    try:
        get_application_runtime().management.legacy_compatibility.reset()
    except ManagementError as exc:
        _fail_cli(exc)
    if json_output:
        _echo_json("reset", {"reset": True, "scope": "persistent_legacy_configuration"})
        return
    typer.echo("Persistent legacy configuration reset")


@app.command(name="migrate-credentials")
def migrate_credentials(
    to: CredentialStorageTarget = typer.Option(  # noqa: B008 (standard typer idiom)
        CredentialStorageTarget.keyring, "--to", help="Target credential storage mode."
    ),
    json_output: JsonOutput = False,
) -> None:
    """Move all stored credentials to the OS keyring or to the plaintext config file.

    Loads the config bypassing env-composited state (env-var accounts, allowlist/bool
    overrides), so migration transforms the stored config, not the env-overridden view.
    """
    target = to.value

    warnings: list[dict[str, str]] = []
    env_override = os.environ.get("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE")
    if env_override is not None and env_override != target:
        warnings.append({
            "code": "environment_override_conflict",
            "message": "MCP_EMAIL_SERVER_CREDENTIAL_STORAGE differs from the migration target.",
        })
        if not json_output:
            typer.echo(
                f"Warning: MCP_EMAIL_SERVER_CREDENTIAL_STORAGE={env_override!r} is set and differs "
                f"from --to {target!r}. This migration will still write '{target}', but future runs "
                "will keep obeying the environment variable until it's unset.",
                err=True,
            )

    try:
        result = get_application_runtime().management.legacy_compatibility.migrate_credentials(target)
    except ManagementError as exc:
        _fail_cli(exc)

    if result.remaining_entries:
        warnings.append({
            "code": "keyring_entries_remaining",
            "message": "Some migrated keyring entries remain and require manual cleanup.",
        })
    if result.unverifiable_entries:
        warnings.append({
            "code": "keyring_cleanup_unverifiable",
            "message": "Removal of some migrated keyring entries could not be verified.",
        })
    if json_output:
        _echo_json(
            "migrate-credentials",
            {
                "target": target,
                "account_count": result.account_count,
                "remaining_entry_count": len(result.remaining_entries),
                "unverifiable_entry_count": len(result.unverifiable_entries),
                "cleanup_complete": not result.remaining_entries and not result.unverifiable_entries,
            },
            warnings=warnings,
        )
        return
    typer.echo(f"Migrated {result.account_count} account(s) to '{target}' storage")
    if result.remaining_entries:
        typer.echo(
            "Warning: the plaintext copy was written, but these keyring entries are still present "
            f"and may hold live secrets: {', '.join(result.remaining_entries)}. Remove them manually — on macOS: "
            f"`security delete-generic-password -s {result.keyring_service}`.",
            err=True,
        )
    if result.unverifiable_entries:
        typer.echo(
            "Warning: the plaintext copy was written, but removal of these keyring entries could "
            f"not be verified: {', '.join(result.unverifiable_entries)}. Check the active keyring manually — on "
            f"macOS: `security delete-generic-password -s {result.keyring_service}`.",
            err=True,
        )


if __name__ == "__main__":
    app(["stdio"])
