import asyncio
import enum
import importlib.metadata
import os
import sys
from pathlib import Path
from typing import Annotated, Never

import typer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import SecretStr

from mcp_email_server import keyring_store
from mcp_email_server.app import mcp
from mcp_email_server.application.management import (
    CreateAccountCommand,
    EndpointPatch,
    EndpointSummary,
    LegacyImportPlan,
    ManagedPolicy,
    ManagementError,
    UpdateAccountCommand,
)
from mcp_email_server.bootstrap import (
    BootstrapError,
    ManagedModeWriteError,
    assert_legacy_writable,
    freeze_process_bootstrap,
)
from mcp_email_server.config import Settings, delete_settings, get_settings
from mcp_email_server.runtime import get_application_runtime

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


def _fail_cli(exc: Exception) -> Never:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1) from exc


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
) -> None:
    """Create a STAGING managed catalog without selecting managed mode."""
    try:
        get_application_runtime().management.lifecycle.initialize(database)
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo("Created STAGING managed catalog. Add an account, test it, then activate it.")


@config_app.command("status")
def config_status() -> None:
    """Show selected mode and bounded managed catalog status."""
    try:
        status = get_application_runtime().management.lifecycle.status()
        typer.echo(f"mode={status.mode}")
        typer.echo(f"bootstrap_revision={status.bootstrap_revision}")
        if status.catalog_problem is not None:
            typer.echo("catalog_status=unavailable")
            typer.echo(f"catalog_problem={status.catalog_problem}")
        elif status.selected_catalog is not None:
            typer.echo("catalog_status=available")
        if status.report is not None:
            typer.echo(f"lifecycle={status.report.lifecycle}")
            typer.echo(f"accounts={status.report.account_count}")
            typer.echo(f"enabled_accounts={status.report.enabled_account_count}")
    except ManagementError as exc:
        _fail_cli(exc)


@config_app.command("doctor")
def config_doctor() -> None:
    """Report bounded catalog, binding, and cleanup health without locators."""
    try:
        report = get_application_runtime().management.lifecycle.doctor()
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo(f"lifecycle={report.lifecycle}")
    typer.echo(f"schema_version={report.schema_version}")
    typer.echo(f"accounts={report.account_count}")
    typer.echo(f"enabled_accounts={report.enabled_account_count}")
    typer.echo(f"pending_bindings={report.pending_bindings}")
    typer.echo(f"cleanup_required_bindings={report.cleanup_required_bindings}")
    typer.echo(f"repair_required_bindings={report.repair_required_bindings}")
    typer.echo("problems=" + (",".join(report.problems) if report.problems else "none"))


@config_app.command("index-health")
def config_index_health() -> None:
    """Show bounded rebuildable metadata projection health."""
    try:
        health = get_application_runtime().management.index_health.get()
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo(f"status={health.status}")
    typer.echo(f"indexed_accounts={health.indexed_accounts}")
    typer.echo(f"pending_operations={health.pending_operations}")
    typer.echo("problems=" + (",".join(health.problems) if health.problems else "none"))


@config_app.command("policy")
def config_policy() -> None:
    """Show the canonical managed catalog policy and revision."""
    try:
        policy = get_application_runtime().management.policy.get()
    except ManagementError as exc:
        _fail_cli(exc)
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
    typer.echo(f"Updated managed policy at revision {policy.revision}.")


@config_app.command("cleanup-credentials")
def config_cleanup_credentials(
    limit: int = typer.Option(100, "--limit", min=1, max=100),
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
) -> None:
    """Preview or interactively apply stored TOML accounts without environment overlays."""
    try:
        service = get_application_runtime().management.legacy_import
        plan = service.preview()
    except ManagementError as exc:
        _fail_cli(exc)
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
def config_activate() -> None:
    """Validate the complete STAGING snapshot and mark it ACTIVE."""
    try:
        lifecycle = get_application_runtime().management.lifecycle
        lifecycle.activate(expected_revision=lifecycle.doctor().catalog_revision)
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo("Managed catalog is ACTIVE. Select managed mode separately when ready.")


@config_app.command("select")
def config_select(mode: ConfigMode) -> None:
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
    typer.echo(f"Selected {mode.value} mode. Restart all MCP server processes for the change to take effect.")


@account_app.command("add")
def account_add(
    name: str,
    email_address: str = typer.Option(..., "--email"),
    full_name: str = typer.Option(..., "--full-name"),
    imap_host: str = typer.Option(..., "--imap-host"),
    imap_port: int = typer.Option(993, "--imap-port"),
    imap_user: str | None = typer.Option(None, "--imap-user"),
    imap_ssl: bool = typer.Option(True, "--imap-ssl/--no-imap-ssl"),
    imap_starttls: bool = typer.Option(False, "--imap-starttls/--no-imap-starttls"),
    imap_verify_ssl: bool = typer.Option(True, "--imap-verify-ssl/--no-imap-verify-ssl"),
    smtp_host: str | None = typer.Option(None, "--smtp-host"),
    smtp_port: int = typer.Option(465, "--smtp-port"),
    smtp_user: str | None = typer.Option(None, "--smtp-user"),
    smtp_ssl: bool = typer.Option(True, "--smtp-ssl/--no-smtp-ssl"),
    smtp_starttls: bool = typer.Option(False, "--smtp-starttls/--no-smtp-starttls"),
    smtp_verify_ssl: bool = typer.Option(True, "--smtp-verify-ssl/--no-smtp-verify-ssl"),
    password_stdin: bool = typer.Option(False, "--password-stdin", help="Read secrets as lines from stdin."),
    save_to_sent: bool = typer.Option(True, "--save-to-sent/--no-save-to-sent"),
    sent_folder: str | None = typer.Option(None, "--sent-folder"),
) -> None:
    """Add one managed account; secrets are prompted or read from stdin, never argv."""
    try:
        incoming_password = _read_secret("Incoming password", from_stdin=password_stdin)
        outgoing_password = (
            _read_secret("Outgoing password", from_stdin=password_stdin) if smtp_host is not None else None
        )
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
            if smtp_host is not None and outgoing_password is not None
            else None
        )
        management = get_application_runtime().management
        result = management.accounts.create(
            CreateAccountCommand(
                expected_catalog_revision=management.lifecycle.doctor().catalog_revision,
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
    states = [f"incoming={result.incoming.status}"]
    if result.outgoing is not None:
        states.append(f"outgoing={result.outgoing.status}")
    typer.echo(f"Added managed account '{name}' ({', '.join(states)}).")


@account_app.command("set-secret")
def account_set_secret(
    name: str,
    role: ConnectionRole,
    password_stdin: bool = typer.Option(False, "--password-stdin", help="Read the secret from stdin."),
) -> None:
    """Install or rotate one managed credential through an immutable candidate."""
    try:
        secret = _read_secret(f"{role.value.title()} password", from_stdin=password_stdin)
        management = get_application_runtime().management
        expected_revision = management.accounts.show(name).revision
        result = management.credentials.set(
            name,
            role.value,
            secret,
            expected_revision=expected_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo(
        f"Credential result for '{name}' role={role.value}: {result.status}"
        + (f" cleanup_required={result.cleanup_required}" if result.cleanup_required else "")
    )


@account_app.command("list")
def account_list() -> None:
    """List non-secret managed account summaries."""
    try:
        accounts = get_application_runtime().management.accounts.list()
    except ManagementError as exc:
        _fail_cli(exc)
    for account in accounts:
        typer.echo(
            f"{account.name}\temail={account.email_address}\tenabled={str(account.enabled).lower()}\t"
            f"incoming={account.incoming_binding}\toutgoing={account.outgoing_binding or 'NONE'}"
        )


@account_app.command("show")
def account_show(name: str) -> None:
    """Show one non-secret managed account summary."""
    try:
        account = get_application_runtime().management.accounts.show(name)
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo(f"name={account.name}")
    typer.echo(f"full_name={account.full_name}")
    typer.echo(f"email={account.email_address}")
    typer.echo(f"enabled={str(account.enabled).lower()}")
    typer.echo(f"revision={account.revision}")
    typer.echo(f"incoming_host={account.incoming.host}")
    typer.echo(f"incoming_port={account.incoming.port}")
    typer.echo(f"incoming_binding={account.incoming_binding}")
    if account.outgoing is not None:
        typer.echo(f"outgoing_host={account.outgoing.host}")
        typer.echo(f"outgoing_port={account.outgoing.port}")
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
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo(f"Updated managed account at revision {revision}.")


@account_app.command("disable")
def account_disable(
    name: str,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
) -> None:
    """Disable an account using its last observed revision."""
    try:
        revision = get_application_runtime().management.accounts.disable(
            name,
            expected_revision=expected_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo(f"Disabled managed account '{name}' at revision {revision}.")


@account_app.command("enable")
def account_enable(
    name: str,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
) -> None:
    """Re-enable a complete account after validating its active secrets."""
    try:
        revision = get_application_runtime().management.accounts.enable(
            name,
            expected_revision=expected_revision,
        )
    except ManagementError as exc:
        _fail_cli(exc)
    typer.echo(f"Enabled managed account '{name}' at revision {revision}.")


@account_app.command("remove")
def account_remove(
    name: str,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    confirm: str = typer.Option(..., "--confirm", help="Repeat the exact account name."),
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
    typer.echo(f"state={result.status}")
    typer.echo(f"revision={result.revision}")
    typer.echo(f"cleanup_required={result.cleanup_required}")


@account_app.command("remove-secret")
def account_remove_secret(
    name: str,
    role: ConnectionRole,
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
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
    typer.echo(f"state={result.status}")
    typer.echo(f"revision={result.revision}")
    typer.echo(f"cleanup_required={result.cleanup_required}")


@account_app.command("test")
def account_test(
    name: str,
    role: Annotated[ConnectionRole, typer.Argument()] = ConnectionRole.incoming,
) -> None:
    """Test managed IMAP or SMTP connectivity outside SQLite transactions."""
    try:
        result = asyncio.run(get_application_runtime().management.connectivity.execute(name, role.value))
    except (ManagementError, ValueError) as exc:
        _fail_cli(exc)
    if result.status != "ok":
        _fail_cli(ManagementError(result.message))
    typer.echo(f"{role.value.title()} connectivity test passed for '{name}'.")


def _validate_managed_runtime() -> None:
    """Fail before opening a transport when selected managed authority is unusable."""
    try:
        if freeze_process_bootstrap().mode == "managed":
            get_settings(reload=True)
    except (BootstrapError, ManagementError, ValueError) as exc:
        _fail_cli(exc)


@app.command()
def stdio():
    _validate_managed_runtime()
    mcp.run(transport="stdio")


@app.command()
def sse(
    host: str = "localhost",
    port: int = 9557,
):
    _validate_managed_runtime()
    _configure_http_transport(host, port)
    mcp.run(transport="sse")


@app.command()
def streamable_http(
    host: str = os.environ.get("MCP_HOST", "localhost"),
    port: int = int(os.environ.get("MCP_PORT", 9557)),
):
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
def reset():
    try:
        assert_legacy_writable("reset legacy settings")
        delete_settings()
    except ManagedModeWriteError as exc:
        _fail_cli(exc)
    typer.echo("Config reset")


def _purge_keyring_after_plaintext_migration(settings: Settings) -> tuple[list[str], list[str]]:
    """Delete and verify keyring entries referenced by the pre-migration file.

    Restricting cleanup to loaded sentinels keeps migration of an already-plaintext
    file an idempotent no-op. Every referenced entry is classified as confirmed
    deleted, confirmed present, or unverifiable after the deletion attempt.
    """
    remaining: list[str] = []
    unverifiable: list[str] = []
    for account_name, role in sorted(settings.loaded_keyring_references):
        entry = f"{account_name}:{role}"
        status = keyring_store.delete_secret_checked(account_name, role)
        if status == "present":
            remaining.append(entry)
        elif status == "unverifiable":
            unverifiable.append(entry)
    return remaining, unverifiable


@app.command(name="migrate-credentials")
def migrate_credentials(
    to: CredentialStorageTarget = typer.Option(  # noqa: B008 (standard typer idiom)
        CredentialStorageTarget.keyring, "--to", help="Target credential storage mode."
    ),
) -> None:
    """Move all stored credentials to the OS keyring or to the plaintext config file.

    Loads the config bypassing env-composited state (env-var accounts, allowlist/bool
    overrides), so migration transforms the stored config, not the env-overridden view.
    """
    target = to.value

    try:
        assert_legacy_writable("migrate legacy credentials")
    except ManagedModeWriteError as exc:
        _fail_cli(exc)

    env_override = os.environ.get("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE")
    if env_override is not None and env_override != target:
        typer.echo(
            f"Warning: MCP_EMAIL_SERVER_CREDENTIAL_STORAGE={env_override!r} is set and differs "
            f"from --to {target!r}. This migration will still write '{target}', but future runs "
            "will keep obeying the environment variable until it's unset.",
            err=True,
        )

    try:
        settings = Settings.load_for_migration()
    except Exception as e:
        typer.echo(f"Error: could not load the current configuration: {e}", err=True)
        raise typer.Exit(code=1) from e

    settings.credential_storage = target
    settings._credential_storage_override = target

    try:
        settings.store()
    except Exception as e:
        typer.echo(f"Error: migration to '{target}' failed: {e}", err=True)
        raise typer.Exit(code=1) from e

    if target == "plaintext":
        remaining, unverifiable = _purge_keyring_after_plaintext_migration(settings)
    else:
        remaining, unverifiable = [], []

    total = len(settings.emails) + len(settings.providers)
    typer.echo(f"Migrated {total} account(s) to '{target}' storage")
    if remaining:
        typer.echo(
            "Warning: the plaintext copy was written, but these keyring entries are still present "
            f"and may hold live secrets: {', '.join(remaining)}. Remove them manually — on macOS: "
            f"`security delete-generic-password -s {keyring_store.SERVICE}`.",
            err=True,
        )
    if unverifiable:
        typer.echo(
            "Warning: the plaintext copy was written, but removal of these keyring entries could "
            f"not be verified: {', '.join(unverifiable)}. Check the active keyring manually — on "
            f"macOS: `security delete-generic-password -s {keyring_store.SERVICE}`.",
            err=True,
        )


if __name__ == "__main__":
    app(["stdio"])
