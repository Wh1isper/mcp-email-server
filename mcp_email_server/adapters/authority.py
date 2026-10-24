from __future__ import annotations

from dataclasses import dataclass

from mcp_email_server import config as config_module
from mcp_email_server import keyring_store
from mcp_email_server.application.management import BindingRole
from mcp_email_server.application.metadata import RuntimeMode
from mcp_email_server.bootstrap import process_bootstrap
from mcp_email_server.config import EmailServer, EmailSettings, ProviderSettings, Settings, get_settings
from mcp_email_server.managed import ManagedCatalog, ManagedCatalogError


@dataclass(frozen=True)
class LocalAccountResolution:
    """Current selected authority plus one optionally credentialed account."""

    mode: RuntimeMode
    settings: Settings
    account: EmailSettings
    authority_revision: tuple[str, int, int] | None = None


class _UnresolvedLegacySettings(Settings):
    """Fresh legacy policy/endpoint view without opening unrelated keyring entries."""

    def _resolve_keyring_sentinels(
        self,
        *,
        migration_load: bool,
        pending: list[tuple[str, str, EmailServer | ProviderSettings]] | None = None,
    ) -> None:
        pass


def resolve_local_account(  # noqa: C901 - managed/legacy selected-role resolution
    account_name: str,
    *,
    roles: tuple[BindingRole, ...] = (),
    expected_mode: RuntimeMode | None = None,
    fresh_legacy: bool = False,
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
            enable_attachment_content=policy.enable_attachment_content,
            allowed_recipients=list(policy.allowed_recipients),
            allowed_senders=list(policy.allowed_senders),
            allowed_from_addresses=list(policy.allowed_from_addresses),
            allowed_mutations=list(policy.allowed_mutations),
            report_blocked_mutations=policy.report_blocked_mutations,
            credential_storage="keyring",
        )
        return LocalAccountResolution(
            mode=mode,
            settings=settings,
            account=resolution.account,
            authority_revision=resolution.authority_revision,
        )

    settings = _UnresolvedLegacySettings() if fresh_legacy else get_settings()
    account = settings.get_account(account_name)
    if isinstance(account, ProviderSettings):
        raise NotImplementedError
    if not isinstance(account, EmailSettings):
        raise ValueError(f"Account {account_name} was not found")  # noqa: TRY004
    if fresh_legacy and roles:
        account = account.model_copy(deep=True)
        pending: list[tuple[str, str, EmailServer | ProviderSettings]] = []
        for role in roles:
            endpoint = account.incoming if role == "incoming" else account.outgoing
            if endpoint is None:
                raise ValueError("Selected account role is unavailable")
            if endpoint.password.get_secret_value() == keyring_store.SENTINEL:
                pending.append((account_name, role, endpoint))
        Settings._resolve_keyring_sentinels(settings, migration_load=False, pending=pending)
    return LocalAccountResolution(mode=mode, settings=settings, account=account)
