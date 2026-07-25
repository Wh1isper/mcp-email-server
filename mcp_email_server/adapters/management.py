from __future__ import annotations

import asyncio
import os
import tomllib
from pathlib import Path

from aiosmtplib.errors import SMTPAuthenticationError
from pydantic import ValidationError

from mcp_email_server import keyring_store
from mcp_email_server.application.limits import APPLICATION_LIMITS, validate_controlled_string
from mcp_email_server.application.management import (
    BindingRole,
    BootstrapSnapshot,
    ConnectivityCheckError,
    EndpointSummary,
    IndexHealth,
    LegacyAccountSnapshot,
    LegacyCredentialMigrationResult,
    LegacyCredentialStorage,
    LegacySourceSnapshot,
    ManagedCatalogPort,
    ManagementError,
    ManagementMode,
    RevisionConflictError,
    SecretSourceClass,
    validate_endpoint,
)
from mcp_email_server.bootstrap import (
    BootstrapError,
    BootstrapRevisionError,
    ManagedModeWriteError,
    _write_bootstrap_locked,
    bootstrap_file_lock,
    configured_path,
    freeze_process_bootstrap,
    process_bootstrap,
    read_bootstrap,
    write_bootstrap,
)
from mcp_email_server.config import (
    EmailSettings,
    LegacyCredentialMigrationLoadError,
    LegacyCredentialMigrationStoreError,
    ProviderSettings,
    Settings,
    compose_legacy_policy_environment,
    delete_settings,
    get_settings,
)
from mcp_email_server.emails.classic import ClassicEmailHandler, ImapAuthenticationError
from mcp_email_server.managed import ManagedCatalog, ManagedCatalogError

_PREVIEW_REDACTED = "preview-redacted"


class LocalManagementBackend:
    """Compose bootstrap, SQLite/keyring, and provider management adapters."""

    def read_bootstrap(self) -> BootstrapSnapshot:
        try:
            bootstrap = read_bootstrap()
            running = process_bootstrap(bootstrap.path)
        except BootstrapError as exc:
            raise ManagementError(
                "Bootstrap configuration could not be read",
                reason="bootstrap_unavailable",
            ) from exc
        return BootstrapSnapshot(
            mode=bootstrap.mode,
            db_path=bootstrap.db_path,
            revision=bootstrap.revision,
            exists=bootstrap.exists,
            running_mode=running.mode,
            running_db_path=running.db_path,
        )

    def default_catalog_path(self) -> Path:
        return configured_path().with_name("managed.sqlite3")

    def initialize_catalog(self, path: Path) -> ManagedCatalogPort:
        try:
            return ManagedCatalog.initialize(path)
        except (ManagedCatalogError, OSError) as exc:
            if isinstance(exc, ManagementError):
                raise
            raise ManagementError("Managed catalog could not be initialized") from exc

    def open_catalog(self, path: Path) -> ManagedCatalogPort:
        return ManagedCatalog(path)

    def write_selection(
        self,
        mode: ManagementMode,
        db_path: Path | None,
        *,
        expected_revision: int,
        expected_exists: bool | None = None,
        expected_source: LegacySourceSnapshot | None = None,
    ) -> None:
        try:
            if expected_source is None:
                write_bootstrap(
                    mode=mode,
                    db_path=db_path,
                    expected_revision=expected_revision,
                    expected_exists=expected_exists,
                )
                return
            source_path = configured_path()
            with bootstrap_file_lock(source_path):
                if self.load_legacy_source() != expected_source:
                    raise ManagementError(
                        "Legacy source changed; reload and retry",
                        reason="import_preview_stale",
                    )
                _write_bootstrap_locked(
                    mode=mode,
                    db_path=db_path,
                    path=source_path,
                    expected_revision=expected_revision,
                    expected_exists=expected_exists,
                )
        except BootstrapRevisionError as exc:
            raise RevisionConflictError("bootstrap") from exc
        except (BootstrapError, OSError) as exc:
            raise ManagementError("Bootstrap selection could not be updated") from exc

    def guarded_import_cutover(
        self,
        *,
        target_path: Path,
        expected_bootstrap_revision: int,
        expected_source: LegacySourceSnapshot,
        expected_resolved_secrets: tuple[tuple[str, BindingRole, str], ...],
        expected_catalog_revision: int,
        expected_account_revisions: tuple[tuple[str, int, bool], ...],
    ) -> None:
        source_path = configured_path()
        try:
            with bootstrap_file_lock(source_path):
                bootstrap = read_bootstrap(source_path)
                if (
                    bootstrap.revision != expected_bootstrap_revision
                    or bootstrap.mode != "legacy"
                    or bootstrap.db_path != target_path
                ):
                    raise RevisionConflictError("bootstrap")
                current_source = self.load_legacy_source()
                if current_source != expected_source:
                    raise ManagementError(
                        "Legacy import preview is stale; preview and retry",
                        reason="import_preview_stale",
                    )
                # Resolve private legacy values while the shared source lock
                # excludes supported legacy writers, but before taking the SQLite
                # writer fence: catalog transactions never span keyring access.
                source_accounts = {account.name: account for account in current_source.accounts}
                for account_name, role, expected_secret in expected_resolved_secrets:
                    account = source_accounts.get(account_name)
                    if account is None or self.resolve_legacy_secret(account_name, role, account) != expected_secret:
                        raise ManagementError(
                            "Legacy import credential source changed; preview and retry",
                            reason="import_preview_stale",
                        )
                catalog = ManagedCatalog(target_path)
                with catalog.import_cutover_guard(
                    expected_catalog_revision=expected_catalog_revision,
                    expected_account_revisions=expected_account_revisions,
                ):
                    if self.load_legacy_source() != expected_source:
                        raise ManagementError(
                            "Legacy import preview is stale; preview and retry",
                            reason="import_preview_stale",
                        )
                    _write_bootstrap_locked(
                        mode="managed",
                        db_path=target_path,
                        path=source_path,
                        expected_revision=expected_bootstrap_revision,
                        expected_exists=None,
                    )
        except BootstrapRevisionError as exc:
            raise RevisionConflictError("bootstrap") from exc
        except (BootstrapError, OSError) as exc:
            raise ManagementError("Bootstrap selection could not be updated") from exc

    @staticmethod
    def _read_legacy_raw() -> dict[str, object]:
        path = configured_path()
        if not path.exists() and not path.is_symlink():
            return {}
        try:
            with path.open("rb") as stream:
                encoded = stream.read(APPLICATION_LIMITS.serialized_response_bytes + 1)
            if len(encoded) > APPLICATION_LIMITS.serialized_response_bytes:
                raise ManagementError("Stored legacy configuration exceeds the size limit")
            raw = tomllib.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ManagementError("Stored legacy configuration could not be read") from exc
        if not isinstance(raw, dict):
            raise ManagementError("Stored legacy configuration must contain a TOML table")
        return raw

    @staticmethod
    def _legacy_secret_source(value: object) -> SecretSourceClass:
        if not isinstance(value, str):
            raise ManagementError("Stored legacy credential marker is invalid")
        return "keyring" if value == keyring_store.SENTINEL else "plaintext"

    @staticmethod
    def _bool_environment(name: str, default: bool) -> bool:
        value = os.getenv(name)
        return default if value is None else value.lower() in {"true", "1", "yes", "on"}

    @classmethod
    def _legacy_account_snapshot(
        cls,
        account: EmailSettings,
        *,
        incoming_source: SecretSourceClass,
        outgoing_source: SecretSourceClass | None,
    ) -> LegacyAccountSnapshot:
        snapshot = LegacyAccountSnapshot(
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
            incoming_secret_source=incoming_source,
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
            outgoing_secret_source=outgoing_source,
            save_to_sent=account.save_to_sent,
            sent_folder_name=account.sent_folder_name,
        )
        cls._validate_legacy_account_snapshot(snapshot)
        return snapshot

    @staticmethod
    def _validate_legacy_account_snapshot(account: LegacyAccountSnapshot) -> None:
        validate_controlled_string(
            account.name,
            field_name="legacy account name",
            maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
        )
        validate_controlled_string(
            account.full_name,
            field_name="legacy full name",
            maximum_bytes=APPLICATION_LIMITS.query_bytes,
        )
        validate_controlled_string(
            account.email_address,
            field_name="legacy email address",
            maximum_bytes=APPLICATION_LIMITS.address_bytes,
        )
        validate_endpoint(account.incoming, role="incoming")
        if account.outgoing is not None:
            validate_endpoint(account.outgoing, role="outgoing")
        if account.sent_folder_name is not None:
            validate_controlled_string(
                account.sent_folder_name,
                field_name="legacy sent folder",
                maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
            )

    @classmethod
    def _stored_legacy_account_snapshots(cls, raw: dict[str, object]) -> tuple[LegacyAccountSnapshot, ...]:
        raw_accounts = raw.get("emails", [])
        if not isinstance(raw_accounts, list) or len(raw_accounts) > APPLICATION_LIMITS.configured_accounts:
            raise ManagementError("Stored legacy email accounts are invalid or exceed the account limit")
        snapshots: list[LegacyAccountSnapshot] = []
        try:
            for item in raw_accounts:
                if not isinstance(item, dict):
                    raise ManagementError("Stored legacy email accounts are invalid")
                incoming = item.get("incoming")
                outgoing = item.get("outgoing")
                if not isinstance(incoming, dict) or (outgoing is not None and not isinstance(outgoing, dict)):
                    raise ManagementError("Stored legacy email accounts are invalid")
                incoming_source = cls._legacy_secret_source(incoming.get("password"))
                outgoing_source = cls._legacy_secret_source(outgoing.get("password")) if outgoing is not None else None
                redacted = dict(item)
                redacted["incoming"] = {**incoming, "password": _PREVIEW_REDACTED}
                if outgoing is not None:
                    redacted["outgoing"] = {**outgoing, "password": _PREVIEW_REDACTED}
                account = EmailSettings.model_validate(redacted)
                snapshots.append(
                    cls._legacy_account_snapshot(
                        account,
                        incoming_source=incoming_source,
                        outgoing_source=outgoing_source,
                    )
                )
        except (ValidationError, ValueError) as exc:
            raise ManagementError("Stored legacy email accounts are invalid") from exc
        names = [account.name for account in snapshots]
        if len(names) != len(set(names)):
            raise ManagementError("Stored legacy email account names must be unique")
        return tuple(snapshots)

    @staticmethod
    def _environment_key_exists(name: str) -> bool:
        # Iterating os.environ decodes names only. Mapping membership may delegate
        # to __getitem__ and materialize a secret value on some implementations.
        return any(candidate == name for candidate in os.environ)

    @classmethod
    def _environment_account_snapshot(cls) -> LegacyAccountSnapshot | None:
        email_address = os.getenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS")
        if not email_address or not cls._environment_key_exists("MCP_EMAIL_SERVER_PASSWORD"):
            return None
        imap_host = os.getenv("MCP_EMAIL_SERVER_IMAP_HOST")
        if not imap_host:
            return None
        account_name = os.getenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "default")
        full_name = os.getenv("MCP_EMAIL_SERVER_FULL_NAME", email_address.split("@")[0])
        user_name = os.getenv("MCP_EMAIL_SERVER_USER_NAME", email_address)
        smtp_host = os.getenv("MCP_EMAIL_SERVER_SMTP_HOST")
        try:
            account = EmailSettings.init(
                account_name=account_name,
                full_name=full_name,
                email_address=email_address,
                user_name=user_name,
                password=_PREVIEW_REDACTED,
                imap_host=imap_host,
                imap_port=int(os.getenv("MCP_EMAIL_SERVER_IMAP_PORT", "993")),
                imap_ssl=cls._bool_environment("MCP_EMAIL_SERVER_IMAP_SSL", True),
                imap_start_ssl=cls._bool_environment("MCP_EMAIL_SERVER_IMAP_START_SSL", False),
                imap_verify_ssl=cls._bool_environment("MCP_EMAIL_SERVER_IMAP_VERIFY_SSL", True),
                smtp_host=smtp_host,
                smtp_port=int(os.getenv("MCP_EMAIL_SERVER_SMTP_PORT", "465")),
                smtp_ssl=cls._bool_environment("MCP_EMAIL_SERVER_SMTP_SSL", True),
                smtp_start_ssl=cls._bool_environment("MCP_EMAIL_SERVER_SMTP_START_SSL", False),
                smtp_verify_ssl=cls._bool_environment("MCP_EMAIL_SERVER_SMTP_VERIFY_SSL", True),
                smtp_user_name=os.getenv("MCP_EMAIL_SERVER_SMTP_USER_NAME", user_name),
                smtp_password=_PREVIEW_REDACTED,
                imap_user_name=os.getenv("MCP_EMAIL_SERVER_IMAP_USER_NAME", user_name),
                imap_password=_PREVIEW_REDACTED,
                save_to_sent=cls._bool_environment("MCP_EMAIL_SERVER_SAVE_TO_SENT", True),
                sent_folder_name=os.getenv("MCP_EMAIL_SERVER_SENT_FOLDER_NAME"),
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise ManagementError("Effective legacy environment account is invalid") from exc
        return cls._legacy_account_snapshot(
            account,
            incoming_source="environment",
            outgoing_source="environment" if account.outgoing is not None else None,
        )

    def _effective_legacy_accounts(self, raw: dict[str, object]) -> tuple[LegacyAccountSnapshot, ...]:
        accounts = list(self._stored_legacy_account_snapshots(raw))
        environment_account = self._environment_account_snapshot()
        if environment_account is None:
            return tuple(accounts)
        for index, account in enumerate(accounts):
            if account.name == environment_account.name:
                accounts[index] = environment_account
                return tuple(accounts)
        return (environment_account, *accounts)

    def load_legacy_source(self) -> LegacySourceSnapshot:  # noqa: C901 - explicit bounded composition
        raw = self._read_legacy_raw()
        accounts = self._effective_legacy_accounts(raw)
        raw_providers = raw.get("providers", [])
        if (
            not isinstance(raw_providers, list)
            or len(accounts) + len(raw_providers) > APPLICATION_LIMITS.configured_accounts
        ):
            raise ManagementError("Stored legacy provider accounts are invalid or exceed the account limit")
        provider_names: list[str] = []
        try:
            for item in raw_providers:
                if not isinstance(item, dict):
                    raise ManagementError("Stored legacy provider accounts are invalid")
                provider = ProviderSettings.model_validate({**item, "api_key": _PREVIEW_REDACTED})
                provider_names.append(
                    validate_controlled_string(
                        provider.account_name,
                        field_name="legacy provider account name",
                        maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
                    )
                )
        except (ValidationError, ValueError) as exc:
            raise ManagementError("Stored legacy provider accounts are invalid") from exc

        recipients = raw.get("allowed_recipients", [])
        senders = raw.get("allowed_senders", [])
        attachment_download = raw.get("enable_attachment_download", False)
        report_blocked = raw.get("report_blocked_mutations", False)
        if (
            not isinstance(recipients, list)
            or len(recipients) > APPLICATION_LIMITS.policy_entries
            or not all(isinstance(item, str) for item in recipients)
            or not isinstance(senders, list)
            or len(senders) > APPLICATION_LIMITS.policy_entries
            or not all(isinstance(item, str) for item in senders)
            or not isinstance(attachment_download, bool)
            or not isinstance(report_blocked, bool)
        ):
            raise ManagementError("Stored legacy policy is invalid")

        try:
            attachment_download, normalized_recipients, normalized_senders, report_blocked = (
                compose_legacy_policy_environment(
                    enable_attachment_download=attachment_download,
                    allowed_recipients=recipients,
                    allowed_senders=senders,
                    report_blocked_mutations=report_blocked,
                )
            )
        except ValueError as exc:
            raise ManagementError("Effective legacy policy environment is invalid") from exc
        if (
            len(normalized_recipients) > APPLICATION_LIMITS.policy_entries
            or len(normalized_senders) > APPLICATION_LIMITS.policy_entries
        ):
            raise ManagementError("Effective legacy policy exceeds the entry limit")
        try:
            for recipient in normalized_recipients:
                validate_controlled_string(
                    recipient,
                    field_name="legacy allowed recipient",
                    maximum_bytes=APPLICATION_LIMITS.address_bytes,
                )
            for sender in normalized_senders:
                validate_controlled_string(
                    sender,
                    field_name="legacy allowed sender",
                    maximum_bytes=APPLICATION_LIMITS.address_bytes,
                )
        except ValueError as exc:
            raise ManagementError("Effective legacy policy contains an invalid entry") from exc
        return LegacySourceSnapshot(
            accounts=accounts,
            unsupported_provider_names=tuple(sorted(provider_names)),
            enable_attachment_download=attachment_download,
            allowed_recipients=tuple(normalized_recipients),
            allowed_senders=tuple(normalized_senders),
            report_blocked_mutations=report_blocked,
        )

    def resolve_legacy_secret(  # noqa: C901 - explicit source-specific JIT resolution
        self,
        account_name: str,
        role: BindingRole,
        expected_account: LegacyAccountSnapshot,
    ) -> str:
        raw = self._read_legacy_raw()
        accounts = self._effective_legacy_accounts(raw)
        match = next((item for item in accounts if item.name == account_name), None)
        if match is None or match != expected_account:
            raise ManagementError(
                "Effective legacy source changed during import; preview and retry",
                reason="import_preview_stale",
            )
        source = match.incoming_secret_source if role == "incoming" else match.outgoing_secret_source
        if source is None:
            raise ManagementError("Stored legacy account has no credential for that role")
        if source == "environment":
            base = os.getenv("MCP_EMAIL_SERVER_PASSWORD")
            override = os.getenv(
                "MCP_EMAIL_SERVER_IMAP_PASSWORD" if role == "incoming" else "MCP_EMAIL_SERVER_SMTP_PASSWORD"
            )
            # Match EmailSettings.init(): an absent or explicitly empty
            # role-specific value falls back to the required base password.
            value = override or base
        else:
            raw_accounts = raw.get("emails", [])
            if not isinstance(raw_accounts, list):
                raise ManagementError("Stored legacy email accounts are invalid")
            raw_account = next(
                (item for item in raw_accounts if isinstance(item, dict) and item.get("account_name") == account_name),
                None,
            )
            if raw_account is None:
                raise ManagementError(
                    "Effective legacy source changed during import; preview and retry",
                    reason="import_preview_stale",
                )
            try:
                account = EmailSettings.model_validate(raw_account)
            except ValidationError as exc:
                raise ManagementError("Stored legacy email account is invalid") from exc
            endpoint = account.incoming if role == "incoming" else account.outgoing
            if endpoint is None:
                raise ManagementError("Stored legacy account has no credential for that role")
            value = endpoint.password.get_secret_value()
        if value == keyring_store.SENTINEL:
            try:
                value = keyring_store.get_secret(account_name, role)
            except Exception:
                raise ManagementError(
                    "Stored legacy credential backend is unavailable",
                    reason="credential_store_unavailable",
                ) from None
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

    def freeze_runtime_authority(self) -> ManagementMode:
        try:
            return freeze_process_bootstrap().mode
        except BootstrapError as exc:
            raise ManagementError(str(exc)) from exc

    def validate_managed_runtime(self) -> None:
        try:
            get_settings(reload=True)
        except (BootstrapError, ManagementError, ValueError) as exc:
            raise ManagementError(str(exc)) from exc

    def reset_legacy_settings(self) -> None:
        try:
            delete_settings()
        except (BootstrapError, ManagedModeWriteError, OSError) as exc:
            raise ManagementError(str(exc)) from exc

    def migrate_legacy_credentials(self, target: LegacyCredentialStorage) -> LegacyCredentialMigrationResult:
        try:
            settings, remaining, unverifiable = Settings.migrate_credentials(target)
        except ManagedModeWriteError as exc:
            raise ManagementError(str(exc)) from exc
        except BootstrapError as exc:
            raise ManagementError(
                "Legacy credential migration is unavailable because bootstrap authority is invalid or busy",
                reason="bootstrap_unavailable",
            ) from exc
        except OSError as exc:
            raise ManagementError(
                "Legacy credential migration storage is unavailable",
                reason="storage_unavailable",
            ) from exc
        except LegacyCredentialMigrationLoadError as exc:
            raise ManagementError("could not load the current configuration") from exc
        except LegacyCredentialMigrationStoreError as exc:
            raise ManagementError(f"migration to '{target}' failed") from exc

        return LegacyCredentialMigrationResult(
            account_count=len(settings.emails) + len(settings.providers),
            remaining_entries=remaining,
            unverifiable_entries=unverifiable,
            keyring_service=keyring_store.SERVICE,
        )

    async def test_connection(self, catalog: ManagedCatalogPort, name: str, role: BindingRole) -> None:
        try:
            if role == "outgoing" and catalog.show_account(name).outgoing is None:
                raise ConnectivityCheckError(  # noqa: TRY301
                    "endpoint_unavailable",
                    "Outgoing endpoint is unavailable; configure SMTP before testing",
                )
            account = catalog.load_account(name, roles=(role,))
            handler = ClassicEmailHandler(account)
            if role == "incoming":
                await handler.list_mailboxes()
                return
            outgoing = account.outgoing
            if handler.outgoing_client is None or outgoing is None:
                raise ConnectivityCheckError(  # noqa: TRY301
                    "endpoint_unavailable",
                    "Outgoing endpoint is unavailable; configure SMTP before testing",
                )

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
        except ConnectivityCheckError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise ConnectivityCheckError(
                    "timeout",
                    "Connection test timed out; verify the endpoint and network, then retry",
                ) from None
            if isinstance(exc, ManagedCatalogError):
                raise ConnectivityCheckError(
                    "credential_unavailable",
                    "Credential is unavailable; save the account password again",
                ) from None
            if isinstance(exc, ImapAuthenticationError | SMTPAuthenticationError):
                raise ConnectivityCheckError(
                    "authentication_or_provider_rejected",
                    "Authentication or provider policy rejected the connection",
                ) from None
            raise ConnectivityCheckError(
                "tls_or_connection_failed",
                "TLS or network connection failed; verify endpoint and TLS settings",
            ) from None
