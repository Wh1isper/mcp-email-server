from __future__ import annotations

from dataclasses import dataclass

from mcp_email_server import config as config_module
from mcp_email_server.application.management import BindingRole
from mcp_email_server.application.metadata import RuntimeMode
from mcp_email_server.bootstrap import process_bootstrap
from mcp_email_server.config import EmailSettings, ProviderSettings, Settings, get_settings
from mcp_email_server.managed import ManagedCatalog, ManagedCatalogError


@dataclass(frozen=True)
class LocalAccountResolution:
    """Current selected authority plus one optionally credentialed account."""

    mode: RuntimeMode
    settings: Settings
    account: EmailSettings


def resolve_local_account(
    account_name: str,
    *,
    roles: tuple[BindingRole, ...] = (),
    expected_mode: RuntimeMode | None = None,
) -> LocalAccountResolution:
    """Resolve one account and atomically pair managed policy with selected roles."""
    bootstrap = process_bootstrap(config_module.CONFIG_PATH)
    mode = bootstrap.mode
    if expected_mode is not None and mode != expected_mode:
        raise RuntimeError("Configuration mode changed; restart required")

    if mode == "managed":
        if bootstrap.db_path is None:
            raise RuntimeError("Managed mode requires a database path")
        catalog = ManagedCatalog(bootstrap.db_path)
        try:
            resolution = catalog.resolve_account(account_name, roles=roles)
        except ManagedCatalogError as exc:
            if "not found or is disabled" in str(exc):
                raise ValueError(f"Account {account_name} was not found") from None
            raise
        policy = resolution.policy
        settings = Settings.model_construct(
            emails=[resolution.account],
            providers=[],
            db_location=catalog.path.as_posix(),
            enable_attachment_download=policy.enable_attachment_download,
            allowed_recipients=list(policy.allowed_recipients),
            allowed_senders=list(policy.allowed_senders),
            report_blocked_mutations=policy.report_blocked_mutations,
            credential_storage="keyring",
        )
        return LocalAccountResolution(mode=mode, settings=settings, account=resolution.account)

    settings = get_settings()
    account = settings.get_account(account_name)
    if isinstance(account, ProviderSettings):
        raise NotImplementedError
    if not isinstance(account, EmailSettings):
        raise ValueError(f"Account {account_name} was not found")  # noqa: TRY004
    return LocalAccountResolution(mode=mode, settings=settings, account=account)
