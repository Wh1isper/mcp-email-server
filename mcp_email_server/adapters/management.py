from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

from pydantic import ValidationError

from mcp_email_server import keyring_store
from mcp_email_server.application.management import (
    BindingRole,
    BootstrapSnapshot,
    EndpointSummary,
    IndexHealth,
    LegacyAccountSnapshot,
    LegacySourceSnapshot,
    ManagedCatalogPort,
    ManagementError,
    ManagementMode,
    RevisionConflictError,
    SecretSourceClass,
)
from mcp_email_server.bootstrap import (
    BootstrapError,
    BootstrapRevisionError,
    configured_path,
    process_bootstrap,
    read_bootstrap,
    write_bootstrap,
)
from mcp_email_server.config import EmailSettings, ProviderSettings, normalize_address_list, normalize_pattern_list
from mcp_email_server.emails.classic import ClassicEmailHandler
from mcp_email_server.managed import ManagedCatalog, ManagedCatalogError


class LocalManagementBackend:
    """Compose bootstrap, SQLite/keyring, and provider management adapters."""

    def read_bootstrap(self) -> BootstrapSnapshot:
        try:
            bootstrap = read_bootstrap()
        except BootstrapError as exc:
            raise ManagementError(str(exc)) from exc
        running = process_bootstrap(bootstrap.path)
        return BootstrapSnapshot(
            mode=bootstrap.mode,
            db_path=bootstrap.db_path,
            revision=bootstrap.revision,
            running_mode=running.mode,
            running_db_path=running.db_path,
        )

    def initialize_catalog(self, path: Path) -> ManagedCatalogPort:
        try:
            return ManagedCatalog.initialize(path)
        except (ManagedCatalogError, OSError) as exc:
            if isinstance(exc, ManagementError):
                raise
            raise ManagementError("Managed catalog could not be initialized") from exc

    def open_catalog(self, path: Path) -> ManagedCatalogPort:
        return ManagedCatalog(path)

    def write_selection(self, mode: ManagementMode, db_path: Path | None, *, expected_revision: int) -> None:
        try:
            write_bootstrap(
                mode=mode,
                db_path=db_path,
                expected_revision=expected_revision,
            )
        except BootstrapRevisionError as exc:
            raise RevisionConflictError("bootstrap") from exc
        except (BootstrapError, OSError) as exc:
            raise ManagementError(str(exc)) from exc

    @staticmethod
    def _read_legacy_raw() -> dict[str, object]:
        path = configured_path()
        try:
            raw = tomllib.loads(path.read_text())
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ManagementError("Stored legacy configuration could not be read") from exc
        if not isinstance(raw, dict):
            raise ManagementError("Stored legacy configuration must contain a TOML table")
        return raw

    @staticmethod
    def _parse_legacy_accounts(raw: dict[str, object]) -> tuple[EmailSettings, ...]:
        raw_accounts = raw.get("emails", [])
        if not isinstance(raw_accounts, list):
            raise ManagementError("Stored legacy email accounts are invalid")
        try:
            accounts = tuple(EmailSettings.model_validate(item) for item in raw_accounts)
        except ValidationError as exc:
            raise ManagementError("Stored legacy email accounts are invalid") from exc
        names = [account.account_name for account in accounts]
        if len(names) != len(set(names)):
            raise ManagementError("Stored legacy email account names must be unique")
        return accounts

    @staticmethod
    def _legacy_secret_source(value: str) -> SecretSourceClass:
        return "keyring" if value == keyring_store.SENTINEL else "plaintext"

    @classmethod
    def _legacy_account_snapshot(cls, account: EmailSettings) -> LegacyAccountSnapshot:
        return LegacyAccountSnapshot(
            name=account.account_name,
            full_name=account.full_name,
            email_address=account.email_address,
            incoming=EndpointSummary(
                host=account.incoming.host,
                port=account.incoming.port,
                user_name=account.incoming.user_name,
                use_ssl=account.incoming.use_ssl,
                start_ssl=account.incoming.start_ssl,
                verify_ssl=account.incoming.verify_ssl,
            ),
            incoming_secret_source=cls._legacy_secret_source(account.incoming.password.get_secret_value()),
            outgoing=EndpointSummary(
                host=account.outgoing.host,
                port=account.outgoing.port,
                user_name=account.outgoing.user_name,
                use_ssl=account.outgoing.use_ssl,
                start_ssl=account.outgoing.start_ssl,
                verify_ssl=account.outgoing.verify_ssl,
            )
            if account.outgoing is not None
            else None,
            outgoing_secret_source=(
                cls._legacy_secret_source(account.outgoing.password.get_secret_value())
                if account.outgoing is not None
                else None
            ),
            save_to_sent=account.save_to_sent,
            sent_folder_name=account.sent_folder_name,
        )

    def load_legacy_source(self) -> LegacySourceSnapshot:
        raw = self._read_legacy_raw()
        accounts = self._parse_legacy_accounts(raw)
        raw_providers = raw.get("providers", [])
        if not isinstance(raw_providers, list):
            raise ManagementError("Stored legacy provider accounts are invalid")
        try:
            providers = tuple(ProviderSettings.model_validate(item) for item in raw_providers)
        except ValidationError as exc:
            raise ManagementError("Stored legacy provider accounts are invalid") from exc

        recipients = raw.get("allowed_recipients", [])
        senders = raw.get("allowed_senders", [])
        attachment_download = raw.get("enable_attachment_download", False)
        report_blocked = raw.get("report_blocked_mutations", False)
        if (
            not isinstance(recipients, list)
            or not all(isinstance(item, str) for item in recipients)
            or not isinstance(senders, list)
            or not all(isinstance(item, str) for item in senders)
            or not isinstance(attachment_download, bool)
            or not isinstance(report_blocked, bool)
        ):
            raise ManagementError("Stored legacy policy is invalid")

        return LegacySourceSnapshot(
            accounts=tuple(self._legacy_account_snapshot(account) for account in accounts),
            unsupported_provider_names=tuple(sorted(provider.account_name for provider in providers)),
            enable_attachment_download=attachment_download,
            allowed_recipients=tuple(normalize_address_list(recipients)),
            allowed_senders=tuple(normalize_pattern_list(senders)),
            report_blocked_mutations=report_blocked,
        )

    def resolve_legacy_secret(
        self,
        account_name: str,
        role: BindingRole,
        expected_account: LegacyAccountSnapshot,
    ) -> str:
        accounts = self._parse_legacy_accounts(self._read_legacy_raw())
        account = next((item for item in accounts if item.account_name == account_name), None)
        if account is None or self._legacy_account_snapshot(account) != expected_account:
            raise ManagementError("Stored legacy source changed during import; preview and retry")
        endpoint = account.incoming if role == "incoming" else account.outgoing
        if endpoint is None:
            raise ManagementError("Stored legacy account has no credential for that role")
        value = endpoint.password.get_secret_value()
        if value == keyring_store.SENTINEL:
            try:
                value = keyring_store.get_secret(account_name, role)
            except Exception as exc:
                raise ManagementError("Stored legacy credential backend is unavailable") from exc
            if value is None:
                raise ManagementError("Stored legacy credential is unavailable")
        if not value:
            raise ManagementError("Stored legacy credential is empty")
        return value

    def index_health(self, catalog: ManagedCatalogPort) -> IndexHealth:
        try:
            return catalog.index_health()
        except ManagementError:
            return IndexHealth(
                status="unavailable",
                indexed_accounts=0,
                pending_operations=0,
                problems=("index_unavailable",),
            )

    async def test_connection(self, catalog: ManagedCatalogPort, name: str, role: BindingRole) -> None:
        try:
            account = catalog.load_account(name, roles=(role,))
            handler = ClassicEmailHandler(account)
            if role == "incoming":
                await handler.list_mailboxes()
                return
            outgoing = account.outgoing
            if handler.outgoing_client is None or outgoing is None:
                raise ManagementError("Managed account has no outgoing endpoint")  # noqa: TRY301

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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, ManagementError):
                raise
            raise ManagementError(f"{role} connectivity test failed: {type(exc).__name__}") from None
