import enum
import os

import typer
from mcp.server.transport_security import TransportSecuritySettings

from mcp_email_server import keyring_store
from mcp_email_server.app import mcp
from mcp_email_server.config import Settings, delete_settings

app = typer.Typer()


class CredentialStorageTarget(str, enum.Enum):
    keyring = "keyring"
    plaintext = "plaintext"


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


@app.command()
def stdio():
    mcp.run(transport="stdio")


@app.command()
def sse(
    host: str = "localhost",
    port: int = 9557,
):
    _configure_http_transport(host, port)
    mcp.run(transport="sse")


@app.command()
def streamable_http(
    host: str = os.environ.get("MCP_HOST", "localhost"),
    port: int = int(os.environ.get("MCP_PORT", 9557)),
):
    _configure_http_transport(host, port)
    mcp.run(transport="streamable-http")


@app.command()
def ui():
    from mcp_email_server.ui import main as ui_main

    ui_main()


@app.command()
def reset():
    delete_settings()
    typer.echo("✅ Config reset")


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
    typer.echo(f"✅ Migrated {total} account(s) to '{target}' storage")
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
