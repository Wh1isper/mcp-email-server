import asyncio
import enum
import os
import sys
from pathlib import Path
from typing import Never

import typer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import SecretStr

from mcp_email_server import keyring_store
from mcp_email_server.app import mcp
from mcp_email_server.bootstrap import (
    BootstrapError,
    ManagedModeWriteError,
    assert_legacy_writable,
    freeze_process_bootstrap,
    read_bootstrap,
    write_bootstrap,
)
from mcp_email_server.config import EmailServer, Settings, delete_settings, get_settings
from mcp_email_server.emails.classic import ClassicEmailHandler
from mcp_email_server.managed import ManagedCatalog, ManagedCatalogError

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


class ConnectionRole(enum.StrEnum):
    incoming = "incoming"
    outgoing = "outgoing"


LOOPBACK_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
WILDCARD_IPV4_BIND_HOST = "0.0.0.0"  # noqa: S104
WILDCARD_BIND_HOSTS = {WILDCARD_IPV4_BIND_HOST, "::", ""}
FALSE_VALUES = {"0", "false", "no", "off"}


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


def _managed_catalog() -> ManagedCatalog:
    bootstrap = read_bootstrap()
    if bootstrap.db_path is None:
        raise BootstrapError("No managed database is configured. Run `mcp-email-server config init --database PATH`.")
    return ManagedCatalog(bootstrap.db_path)


def _fail_cli(exc: Exception) -> Never:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1) from exc


def _read_secret(label: str, *, from_stdin: bool) -> str:
    if from_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    else:
        value = typer.prompt(label, hide_input=True, confirmation_prompt=False)
    if not value:
        raise ManagedCatalogError(f"{label} must not be empty")
    return value


@config_app.command("init")
def config_init(
    database: Path = typer.Option(..., "--database", help="Path for the new managed SQLite catalog."),  # noqa: B008
) -> None:
    """Create a STAGING managed catalog without selecting managed mode."""
    try:
        bootstrap = read_bootstrap()
        if bootstrap.mode == "managed":
            raise BootstrapError(  # noqa: TRY301
                "Cannot initialize a replacement catalog while managed mode is selected. "
                "Select legacy, restart, and retry."
            )
        catalog = ManagedCatalog.initialize(database)
        write_bootstrap(mode=bootstrap.mode, db_path=catalog.path)
    except (BootstrapError, ManagedCatalogError, OSError) as exc:
        _fail_cli(exc)
    typer.echo("Created STAGING managed catalog. Add an account, test it, then activate it.")


@config_app.command("status")
def config_status() -> None:
    """Show selected mode and bounded managed catalog status."""
    try:
        bootstrap = read_bootstrap()
        typer.echo(f"mode={bootstrap.mode}")
        if bootstrap.db_path is not None:
            report = ManagedCatalog(bootstrap.db_path).doctor()
            typer.echo(f"lifecycle={report.lifecycle}")
            typer.echo(f"accounts={report.account_count}")
            typer.echo(f"enabled_accounts={report.enabled_account_count}")
    except (BootstrapError, ManagedCatalogError) as exc:
        _fail_cli(exc)


@config_app.command("doctor")
def config_doctor() -> None:
    """Report bounded catalog, binding, and cleanup health without locators."""
    try:
        report = _managed_catalog().doctor()
    except (BootstrapError, ManagedCatalogError) as exc:
        _fail_cli(exc)
    typer.echo(f"lifecycle={report.lifecycle}")
    typer.echo(f"schema_version={report.schema_version}")
    typer.echo(f"accounts={report.account_count}")
    typer.echo(f"enabled_accounts={report.enabled_account_count}")
    typer.echo(f"pending_bindings={report.pending_bindings}")
    typer.echo(f"cleanup_required_bindings={report.cleanup_required_bindings}")
    typer.echo("problems=" + (",".join(report.problems) if report.problems else "none"))


@config_app.command("activate")
def config_activate() -> None:
    """Validate the complete STAGING snapshot and mark it ACTIVE."""
    try:
        _managed_catalog().activate()
    except (BootstrapError, ManagedCatalogError) as exc:
        _fail_cli(exc)
    typer.echo("Managed catalog is ACTIVE. Select managed mode separately when ready.")


@config_app.command("select")
def config_select(mode: ConfigMode) -> None:
    """Atomically select legacy or an already ACTIVE managed catalog."""
    try:
        bootstrap = read_bootstrap()
        if mode is ConfigMode.managed:
            if bootstrap.db_path is None:
                raise BootstrapError("Selecting managed mode requires a configured database")  # noqa: TRY301
            catalog = ManagedCatalog(bootstrap.db_path)
            if catalog.lifecycle() != "ACTIVE":
                raise ManagedCatalogError("Managed catalog must be ACTIVE before selection")  # noqa: TRY301
            # Selection validates the full effective snapshot, including active
            # secret availability, rather than lifecycle metadata alone.
            catalog.load_settings(require_active=True)
        write_bootstrap(mode=mode.value, db_path=bootstrap.db_path)
    except (BootstrapError, ManagedCatalogError) as exc:
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
        incoming = EmailServer(
            user_name=imap_user or email_address,
            password=SecretStr(incoming_password),
            host=imap_host,
            port=imap_port,
            use_ssl=imap_ssl,
            start_ssl=imap_starttls,
            verify_ssl=imap_verify_ssl,
        )
        outgoing = (
            EmailServer(
                user_name=smtp_user or email_address,
                password=SecretStr(outgoing_password),
                host=smtp_host,
                port=smtp_port,
                use_ssl=smtp_ssl,
                start_ssl=smtp_starttls,
                verify_ssl=smtp_verify_ssl,
            )
            if smtp_host is not None and outgoing_password is not None
            else None
        )
        catalog = _managed_catalog()
        catalog.add_account(
            name=name,
            full_name=full_name,
            email_address=email_address,
            incoming=incoming,
            outgoing=outgoing,
            save_to_sent=save_to_sent,
            sent_folder_name=sent_folder,
        )
        catalog.set_secret(name, "incoming", incoming_password)
        if outgoing_password is not None:
            catalog.set_secret(name, "outgoing", outgoing_password)
    except (BootstrapError, ManagedCatalogError, ValueError) as exc:
        _fail_cli(exc)
    typer.echo(f"Added managed account '{name}'.")


@account_app.command("set-secret")
def account_set_secret(
    name: str,
    role: ConnectionRole,
    password_stdin: bool = typer.Option(False, "--password-stdin", help="Read the secret from stdin."),
) -> None:
    """Install or rotate one managed credential through an immutable candidate."""
    try:
        secret = _read_secret(f"{role.value.title()} password", from_stdin=password_stdin)
        _managed_catalog().set_secret(name, role.value, secret)
    except (BootstrapError, ManagedCatalogError) as exc:
        _fail_cli(exc)
    typer.echo(f"Updated {role.value} credential for '{name}'.")


@account_app.command("list")
def account_list() -> None:
    """List non-secret managed account summaries."""
    try:
        accounts = _managed_catalog().list_accounts()
    except (BootstrapError, ManagedCatalogError) as exc:
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
        account = next((item for item in _managed_catalog().list_accounts() if item.name == name), None)
        if account is None:
            raise ManagedCatalogError("Managed account was not found")  # noqa: TRY301
    except (BootstrapError, ManagedCatalogError) as exc:
        _fail_cli(exc)
    typer.echo(f"name={account.name}")
    typer.echo(f"email={account.email_address}")
    typer.echo(f"enabled={str(account.enabled).lower()}")
    typer.echo(f"revision={account.revision}")
    typer.echo(f"incoming_binding={account.incoming_binding}")
    typer.echo(f"outgoing_binding={account.outgoing_binding or 'NONE'}")


@account_app.command("disable")
def account_disable(name: str) -> None:
    """Disable an account before any subsequent provider access."""
    try:
        _managed_catalog().disable_account(name)
    except (BootstrapError, ManagedCatalogError) as exc:
        _fail_cli(exc)
    typer.echo(f"Disabled managed account '{name}'.")


async def _test_account_connection(catalog: ManagedCatalog, name: str, role: ConnectionRole) -> None:
    account = catalog.load_account(name)
    handler = ClassicEmailHandler(account)
    if role is ConnectionRole.incoming:
        await handler.list_mailboxes()
        return
    outgoing = account.outgoing
    if handler.outgoing_client is None or outgoing is None:
        raise ManagedCatalogError("Managed account has no outgoing endpoint")

    import aiosmtplib

    client = handler.outgoing_client
    async with aiosmtplib.SMTP(
        hostname=outgoing.host,
        port=outgoing.port,
        start_tls=client.smtp_start_tls,
        use_tls=client.smtp_use_tls,
        tls_context=client._get_smtp_ssl_context(),
    ) as smtp:
        await smtp.login(outgoing.user_name, outgoing.password.get_secret_value())


@account_app.command("test")
def account_test(name: str, role: ConnectionRole = ConnectionRole.incoming) -> None:
    """Test managed IMAP or SMTP connectivity outside SQLite transactions."""
    try:
        asyncio.run(_test_account_connection(_managed_catalog(), name, role))
    except Exception as exc:
        _fail_cli(ManagedCatalogError(f"{role.value} connectivity test failed: {type(exc).__name__}"))
    typer.echo(f"{role.value.title()} connectivity test passed for '{name}'.")


def _validate_managed_runtime() -> None:
    """Fail before opening a transport when selected managed authority is unusable."""
    try:
        if freeze_process_bootstrap().mode == "managed":
            get_settings(reload=True)
    except (BootstrapError, ManagedCatalogError, ValueError) as exc:
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
def ui():
    try:
        assert_legacy_writable("open the legacy configuration UI")
    except ManagedModeWriteError as exc:
        _fail_cli(exc)
    from mcp_email_server.ui import main as ui_main

    ui_main()


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
