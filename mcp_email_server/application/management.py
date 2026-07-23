from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import SecretStr

from mcp_email_server.application.limits import APPLICATION_LIMITS, validate_controlled_string
from mcp_email_server.config import EmailServer, EmailSettings, normalize_address_list, normalize_pattern_list

BindingRole = Literal["incoming", "outgoing"]
SecretSourceClass = Literal["plaintext", "keyring"]
CredentialMutationStatus = Literal["active", "active_cleanup_required", "pending_repair_required"]
CredentialRemovalStatus = Literal["removed", "removed_cleanup_required"]
CredentialRepairStatus = Literal[
    "active",
    "active_cleanup_required",
    "rolled_back",
    "rolled_back_cleanup_required",
]
Lifecycle = Literal["STAGING", "ACTIVE"]
ManagementMode = Literal["legacy", "managed"]


class ManagementError(RuntimeError):
    """A management workflow failed without exposing sensitive internals."""


class RevisionConflictError(ManagementError):
    """An optimistic managed aggregate revision or claimed state changed."""

    def __init__(
        self,
        aggregate: Literal["bootstrap", "catalog", "account", "credential"],
        *,
        name: str | None = None,
        message: str | None = None,
    ) -> None:
        self.aggregate = aggregate
        self.name = name
        super().__init__(message or f"Managed {aggregate} revision changed; reload and retry")


@dataclass(frozen=True)
class EndpointSummary:
    host: str
    port: int
    use_ssl: bool
    start_ssl: bool
    verify_ssl: bool
    user_name: str


@dataclass(frozen=True)
class AccountSummary:
    name: str
    email_address: str
    enabled: bool
    revision: int
    has_outgoing: bool
    incoming_binding: str
    outgoing_binding: str | None


@dataclass(frozen=True)
class AccountDetails:
    name: str
    full_name: str
    email_address: str
    enabled: bool
    revision: int
    save_to_sent: bool
    sent_folder_name: str | None
    incoming: EndpointSummary
    outgoing: EndpointSummary | None
    incoming_binding: str
    outgoing_binding: str | None


@dataclass(frozen=True)
class CredentialMutationResult:
    status: CredentialMutationStatus
    revision: int
    cleanup_required: int = 0


@dataclass(frozen=True)
class AccountCreationResult:
    incoming: CredentialMutationResult
    outgoing: CredentialMutationResult | None


@dataclass(frozen=True)
class CredentialRepairResult:
    status: CredentialRepairStatus
    revision: int
    cleanup_required: int = 0


@dataclass(frozen=True)
class CredentialCleanupReport:
    examined: int
    cleaned: int
    remaining: int


@dataclass(frozen=True)
class CredentialRemovalResult:
    status: CredentialRemovalStatus
    revision: int
    cleanup_required: int = 0


@dataclass(frozen=True)
class ConnectivityResult:
    role: BindingRole
    status: Literal["ok", "failed"]
    message: str


@dataclass(frozen=True)
class IndexHealth:
    status: Literal["healthy", "degraded", "unavailable"]
    indexed_accounts: int
    pending_operations: int
    problems: tuple[str, ...]


@dataclass(frozen=True)
class ManagedPolicy:
    revision: int
    enable_attachment_download: bool
    allowed_recipients: tuple[str, ...]
    allowed_senders: tuple[str, ...]
    report_blocked_mutations: bool


@dataclass(frozen=True)
class DoctorReport:
    lifecycle: Lifecycle
    schema_version: int
    catalog_revision: int
    account_count: int
    enabled_account_count: int
    pending_bindings: int
    cleanup_required_bindings: int
    repair_required_bindings: int
    problems: tuple[str, ...]


@dataclass(frozen=True)
class BootstrapSnapshot:
    mode: ManagementMode
    db_path: Path | None
    revision: int = 0
    running_mode: ManagementMode | None = None
    running_db_path: Path | None = None


@dataclass(frozen=True)
class ManagementStatus:
    mode: ManagementMode
    selected_catalog: str | None
    bootstrap_revision: int
    restart_required: bool
    report: DoctorReport | None
    catalog_problem: str | None = None


@dataclass(frozen=True)
class AccountRemovalResult:
    revision: int
    credentials_examined: int
    credentials_cleaned: int
    cleanup_required: int


@dataclass(frozen=True)
class CreateAccountCommand:
    expected_catalog_revision: int
    name: str
    full_name: str
    email_address: str
    incoming: EndpointSummary
    incoming_secret: SecretStr = field(repr=False, compare=False)
    outgoing: EndpointSummary | None = None
    outgoing_secret: SecretStr | None = field(default=None, repr=False, compare=False)
    save_to_sent: bool = True
    sent_folder_name: str | None = None


@dataclass(frozen=True)
class EndpointPatch:
    host: str | None = None
    port: int | None = None
    use_ssl: bool | None = None
    start_ssl: bool | None = None
    verify_ssl: bool | None = None
    user_name: str | None = None

    def apply(self, endpoint: EndpointSummary | None) -> EndpointSummary:
        if endpoint is None:
            if (
                not isinstance(self.host, str)
                or not isinstance(self.port, int)
                or isinstance(self.port, bool)
                or not isinstance(self.use_ssl, bool)
                or not isinstance(self.start_ssl, bool)
                or not isinstance(self.verify_ssl, bool)
                or not isinstance(self.user_name, str)
            ):
                raise ManagementError("A new endpoint requires host, port, TLS flags, and user name")
            return EndpointSummary(
                host=self.host,
                port=self.port,
                use_ssl=self.use_ssl,
                start_ssl=self.start_ssl,
                verify_ssl=self.verify_ssl,
                user_name=self.user_name,
            )
        return EndpointSummary(
            host=self.host if self.host is not None else endpoint.host,
            port=self.port if self.port is not None else endpoint.port,
            use_ssl=self.use_ssl if self.use_ssl is not None else endpoint.use_ssl,
            start_ssl=self.start_ssl if self.start_ssl is not None else endpoint.start_ssl,
            verify_ssl=self.verify_ssl if self.verify_ssl is not None else endpoint.verify_ssl,
            user_name=self.user_name if self.user_name is not None else endpoint.user_name,
        )


@dataclass(frozen=True)
class UpdateAccountCommand:
    name: str
    expected_revision: int
    new_name: str | None = None
    full_name: str | None = None
    email_address: str | None = None
    incoming: EndpointPatch | None = None
    outgoing: EndpointPatch | None = None
    remove_outgoing: bool = False
    save_to_sent: bool | None = None
    sent_folder_name: str | None = None
    update_sent_folder: bool = False


@dataclass(frozen=True)
class LegacyAccountSnapshot:
    name: str
    full_name: str
    email_address: str
    incoming: EndpointSummary
    incoming_secret_source: SecretSourceClass
    outgoing: EndpointSummary | None
    outgoing_secret_source: SecretSourceClass | None
    save_to_sent: bool
    sent_folder_name: str | None


@dataclass(frozen=True)
class LegacyPolicySnapshot:
    enable_attachment_download: bool
    allowed_recipients: tuple[str, ...]
    allowed_senders: tuple[str, ...]
    report_blocked_mutations: bool


@dataclass(frozen=True)
class LegacySourceSnapshot:
    accounts: tuple[LegacyAccountSnapshot, ...]
    unsupported_provider_names: tuple[str, ...]
    enable_attachment_download: bool
    allowed_recipients: tuple[str, ...]
    allowed_senders: tuple[str, ...]
    report_blocked_mutations: bool

    @property
    def policy(self) -> LegacyPolicySnapshot:
        return LegacyPolicySnapshot(
            enable_attachment_download=self.enable_attachment_download,
            allowed_recipients=self.allowed_recipients,
            allowed_senders=self.allowed_senders,
            report_blocked_mutations=self.report_blocked_mutations,
        )


ImportAction = Literal["create", "resume_credentials", "unchanged", "conflict"]


@dataclass(frozen=True)
class LegacyImportAccountPlan:
    name: str
    action: ImportAction
    source: LegacyAccountSnapshot
    expected_target_revision: int | None
    missing_credentials: tuple[BindingRole, ...] = ()


@dataclass(frozen=True)
class LegacyImportPlan:
    preview_token: str
    source_fingerprint: str
    target_revision: int
    target_policy_revision: int
    created_at: str
    accounts: tuple[LegacyImportAccountPlan, ...]
    source_policy: LegacyPolicySnapshot
    policy_action: Literal["update", "unchanged"]
    unsupported_provider_names: tuple[str, ...]

    @property
    def has_conflicts(self) -> bool:
        return any(account.action == "conflict" for account in self.accounts)


@dataclass(frozen=True)
class LegacyImportReport:
    plan: LegacyImportPlan
    created: tuple[str, ...]
    resumed: tuple[str, ...]


@dataclass(frozen=True)
class _StoredLegacyPreview:
    plan: LegacyImportPlan
    expires_at: float


class ManagedCatalogPort(Protocol):
    path: Path

    def lifecycle(self) -> Lifecycle: ...

    def catalog_revision(self) -> int: ...

    def doctor(self) -> DoctorReport: ...

    def index_health(self) -> IndexHealth: ...

    def activate(self, *, expected_revision: int) -> None: ...

    def policy(self) -> ManagedPolicy: ...

    def update_policy(
        self,
        *,
        expected_revision: int,
        enable_attachment_download: bool,
        allowed_recipients: tuple[str, ...],
        allowed_senders: tuple[str, ...],
        report_blocked_mutations: bool,
    ) -> int: ...

    def validate_ready(self, *, require_active: bool = True) -> None: ...

    def load_account(
        self,
        name: str,
        *,
        roles: tuple[BindingRole, ...] = ("incoming", "outgoing"),
        require_active_catalog: bool = False,
    ) -> EmailSettings: ...

    def add_account(
        self,
        *,
        name: str,
        full_name: str,
        email_address: str,
        incoming: EmailServer | EndpointSummary,
        outgoing: EmailServer | EndpointSummary | None,
        save_to_sent: bool = True,
        sent_folder_name: str | None = None,
        expected_revision: int | None = None,
    ) -> str: ...

    def set_secret(
        self,
        name: str,
        role: BindingRole,
        value: str,
        *,
        expected_revision: int | None = None,
    ) -> CredentialMutationResult: ...

    def list_accounts(self) -> list[AccountSummary]: ...

    def show_account(self, name: str) -> AccountDetails: ...

    def has_removed_account(self, name: str) -> bool: ...

    def update_account(
        self,
        name: str,
        *,
        expected_revision: int,
        new_name: str | None = None,
        full_name: str | None = None,
        email_address: str | None = None,
        incoming: EndpointSummary | None = None,
        outgoing: EndpointSummary | None = None,
        remove_outgoing: bool = False,
        save_to_sent: bool | None = None,
        sent_folder_name: str | None = None,
        update_sent_folder: bool = False,
    ) -> int: ...

    def disable_account(self, name: str, *, expected_revision: int) -> int: ...

    def enable_account(self, name: str, *, expected_revision: int) -> int: ...

    def soft_remove_account(self, name: str, *, expected_revision: int) -> AccountRemovalResult: ...

    def remove_secret(self, name: str, role: BindingRole, *, expected_revision: int) -> CredentialRemovalResult: ...

    def repair_secret(
        self,
        name: str,
        role: BindingRole,
        *,
        action: Literal["resume", "rollback"],
        expected_revision: int,
    ) -> CredentialRepairResult: ...

    def cleanup_credentials(
        self,
        *,
        limit: int,
        expected_revision: int | None = None,
    ) -> CredentialCleanupReport: ...


class ManagementBackend(Protocol):
    def read_bootstrap(self) -> BootstrapSnapshot: ...

    def initialize_catalog(self, path: Path) -> ManagedCatalogPort: ...

    def open_catalog(self, path: Path) -> ManagedCatalogPort: ...

    def write_selection(self, mode: ManagementMode, db_path: Path | None, *, expected_revision: int) -> None: ...

    def load_legacy_source(self) -> LegacySourceSnapshot: ...

    def resolve_legacy_secret(
        self,
        account_name: str,
        role: BindingRole,
        expected_account: LegacyAccountSnapshot,
    ) -> str: ...

    async def test_connection(self, catalog: ManagedCatalogPort, name: str, role: BindingRole) -> None: ...

    def index_health(self, catalog: ManagedCatalogPort) -> IndexHealth: ...


class _ConfiguredCatalogService:
    def __init__(self, backend: ManagementBackend) -> None:
        self._backend = backend

    def _catalog(self) -> ManagedCatalogPort:
        bootstrap = self._backend.read_bootstrap()
        if bootstrap.db_path is None:
            raise ManagementError(
                "No managed database is configured. Run `mcp-email-server config init --database PATH`."
            )
        return self._backend.open_catalog(bootstrap.db_path)


class CatalogLifecycleService(_ConfiguredCatalogService):
    """Own explicit catalog initialization, activation, and mode selection."""

    def initialize(self, database: Path) -> None:
        bootstrap = self._backend.read_bootstrap()
        if bootstrap.mode == "managed":
            raise ManagementError(
                "Cannot initialize a replacement catalog while managed mode is selected. "
                "Select legacy, restart, and retry."
            )
        catalog = self._backend.initialize_catalog(database)
        self._backend.write_selection(
            bootstrap.mode,
            catalog.path,
            expected_revision=bootstrap.revision,
        )

    def status(self) -> ManagementStatus:
        bootstrap = self._backend.read_bootstrap()
        report: DoctorReport | None = None
        catalog_problem: str | None = None
        if bootstrap.db_path is not None:
            try:
                report = self._backend.open_catalog(bootstrap.db_path).doctor()
            except ManagementError:
                catalog_problem = "selected_catalog_unavailable"
        running_mode = bootstrap.running_mode if bootstrap.running_mode is not None else bootstrap.mode
        running_db_path = bootstrap.running_db_path if bootstrap.running_mode is not None else bootstrap.db_path
        return ManagementStatus(
            mode=bootstrap.mode,
            selected_catalog=bootstrap.db_path.as_posix() if bootstrap.db_path is not None else None,
            bootstrap_revision=bootstrap.revision,
            restart_required=(running_mode, running_db_path) != (bootstrap.mode, bootstrap.db_path),
            report=report,
            catalog_problem=catalog_problem,
        )

    def doctor(self) -> DoctorReport:
        return self._catalog().doctor()

    def activate(self, *, expected_revision: int) -> None:
        self._catalog().activate(expected_revision=expected_revision)

    def select(
        self,
        mode: ManagementMode,
        *,
        expected_bootstrap_revision: int,
        expected_catalog_revision: int | None = None,
    ) -> None:
        bootstrap = self._backend.read_bootstrap()
        if bootstrap.revision != expected_bootstrap_revision:
            raise RevisionConflictError("bootstrap")
        if mode == "managed":
            if bootstrap.db_path is None:
                raise ManagementError("Selecting managed mode requires a configured database")
            if expected_catalog_revision is None:
                raise ManagementError("Selecting managed mode requires the expected catalog revision")
            catalog = self._backend.open_catalog(bootstrap.db_path)
            if catalog.catalog_revision() != expected_catalog_revision:
                raise RevisionConflictError("catalog")
            if catalog.lifecycle() != "ACTIVE":
                raise ManagementError("Managed catalog must be ACTIVE before selection")
            catalog.validate_ready(require_active=True)
        self._backend.write_selection(
            mode,
            bootstrap.db_path,
            expected_revision=expected_bootstrap_revision,
        )


def _validate_endpoint(endpoint: EmailServer | EndpointSummary, *, role: BindingRole) -> None:
    validate_controlled_string(
        endpoint.host,
        field_name=f"{role} host",
        maximum_bytes=APPLICATION_LIMITS.query_bytes,
    )
    validate_controlled_string(
        endpoint.user_name,
        field_name=f"{role} user name",
        maximum_bytes=APPLICATION_LIMITS.address_bytes,
    )


def _validate_secret(value: object, *, role: BindingRole) -> str:
    if not isinstance(value, str) or not value:
        raise ManagementError(f"{role.title()} credential must not be empty")
    if len(value.encode()) > APPLICATION_LIMITS.ui_json_body_bytes:
        raise ManagementError(f"{role.title()} credential exceeds the input limit")
    return value


def _unwrap_secret(value: object, *, role: BindingRole) -> str:
    if not isinstance(value, SecretStr):
        raise ManagementError(f"{role.title()} credential must be a protected secret")
    return _validate_secret(value.get_secret_value(), role=role)


class ManagedAccountService(_ConfiguredCatalogService):
    """Own managed account lifecycle commands and optimistic revisions."""

    def create(self, command: CreateAccountCommand) -> AccountCreationResult:
        if command.expected_catalog_revision < 1:
            raise ManagementError("Expected catalog revision must be positive")
        validate_controlled_string(
            command.name,
            field_name="account name",
            maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
        )
        validate_controlled_string(
            command.full_name,
            field_name="full name",
            maximum_bytes=APPLICATION_LIMITS.query_bytes,
        )
        validate_controlled_string(
            command.email_address,
            field_name="email address",
            maximum_bytes=APPLICATION_LIMITS.address_bytes,
        )
        if command.sent_folder_name is not None:
            validate_controlled_string(
                command.sent_folder_name,
                field_name="sent folder",
                maximum_bytes=APPLICATION_LIMITS.mailbox_bytes,
            )
        _validate_endpoint(command.incoming, role="incoming")
        incoming_secret = _unwrap_secret(command.incoming_secret, role="incoming")
        if (command.outgoing is None) != (command.outgoing_secret is None):
            raise ManagementError("Outgoing endpoint and credential must be provided together")
        outgoing_secret: str | None = None
        if command.outgoing is not None and command.outgoing_secret is not None:
            _validate_endpoint(command.outgoing, role="outgoing")
            outgoing_secret = _unwrap_secret(command.outgoing_secret, role="outgoing")
        catalog = self._catalog()
        catalog.add_account(
            name=command.name,
            full_name=command.full_name,
            email_address=command.email_address,
            incoming=command.incoming,
            outgoing=command.outgoing,
            save_to_sent=command.save_to_sent,
            sent_folder_name=command.sent_folder_name,
            expected_revision=command.expected_catalog_revision,
        )
        incoming = catalog.set_secret(
            command.name,
            "incoming",
            incoming_secret,
            expected_revision=1,
        )
        outgoing = (
            catalog.set_secret(
                command.name,
                "outgoing",
                outgoing_secret,
                expected_revision=incoming.revision,
            )
            if outgoing_secret is not None
            else None
        )
        return AccountCreationResult(incoming=incoming, outgoing=outgoing)

    def list(self) -> list[AccountSummary]:
        return self._catalog().list_accounts()

    def show(self, name: str) -> AccountDetails:
        return self._catalog().show_account(name)

    def update(self, command: UpdateAccountCommand) -> int:
        for field_name, value, limit in (
            ("account name", command.name, APPLICATION_LIMITS.account_name_bytes),
            ("new account name", command.new_name, APPLICATION_LIMITS.account_name_bytes),
            ("full name", command.full_name, APPLICATION_LIMITS.query_bytes),
            ("email address", command.email_address, APPLICATION_LIMITS.address_bytes),
            ("sent folder", command.sent_folder_name, APPLICATION_LIMITS.mailbox_bytes),
        ):
            if value is not None:
                validate_controlled_string(value, field_name=field_name, maximum_bytes=limit)
        catalog = self._catalog()
        current = catalog.show_account(command.name)
        incoming = command.incoming.apply(current.incoming) if command.incoming is not None else None
        outgoing = command.outgoing.apply(current.outgoing) if command.outgoing is not None else None
        if incoming is not None:
            _validate_endpoint(incoming, role="incoming")
        if outgoing is not None:
            _validate_endpoint(outgoing, role="outgoing")
        return catalog.update_account(
            command.name,
            expected_revision=command.expected_revision,
            new_name=command.new_name,
            full_name=command.full_name,
            email_address=command.email_address,
            incoming=incoming,
            outgoing=outgoing,
            remove_outgoing=command.remove_outgoing,
            save_to_sent=command.save_to_sent,
            sent_folder_name=command.sent_folder_name,
            update_sent_folder=command.update_sent_folder,
        )

    def disable(self, name: str, *, expected_revision: int) -> int:
        return self._catalog().disable_account(name, expected_revision=expected_revision)

    def enable(self, name: str, *, expected_revision: int) -> int:
        return self._catalog().enable_account(name, expected_revision=expected_revision)

    def soft_remove(self, name: str, *, expected_revision: int, confirmation: str) -> AccountRemovalResult:
        if confirmation != name:
            raise ManagementError("Soft removal confirmation must exactly match the account name")
        return self._catalog().soft_remove_account(name, expected_revision=expected_revision)


class CredentialManagementService(_ConfiguredCatalogService):
    """Own managed credential rotation, detachment, and bounded cleanup."""

    def set(
        self,
        name: str,
        role: BindingRole,
        value: str,
        *,
        expected_revision: int,
    ) -> CredentialMutationResult:
        validate_controlled_string(
            name,
            field_name="account name",
            maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
        )
        _validate_secret(value, role=role)
        return self._catalog().set_secret(name, role, value, expected_revision=expected_revision)

    def remove(self, name: str, role: BindingRole, *, expected_revision: int) -> CredentialRemovalResult:
        return self._catalog().remove_secret(name, role, expected_revision=expected_revision)

    def repair(
        self,
        name: str,
        role: BindingRole,
        *,
        action: Literal["resume", "rollback"],
        expected_revision: int,
    ) -> CredentialRepairResult:
        return self._catalog().repair_secret(
            name,
            role,
            action=action,
            expected_revision=expected_revision,
        )

    def cleanup(self, *, limit: int, expected_revision: int) -> CredentialCleanupReport:
        return self._catalog().cleanup_credentials(
            limit=limit,
            expected_revision=expected_revision,
        )


class LegacyImportService(_ConfiguredCatalogService):
    """Preview and explicitly apply stored legacy accounts without environment overlays."""

    _PREVIEW_TTL_SECONDS = 600.0

    def __init__(self, backend: ManagementBackend) -> None:
        super().__init__(backend)
        self._previews: dict[str, _StoredLegacyPreview] = {}

    @staticmethod
    def _source_fingerprint(source: LegacySourceSnapshot) -> str:
        canonical = json.dumps(asdict(source), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _configuration_matches(source: LegacyAccountSnapshot, destination: AccountDetails) -> bool:
        return (
            source.name == destination.name
            and source.full_name == destination.full_name
            and source.email_address == destination.email_address
            and source.incoming == destination.incoming
            and source.outgoing == destination.outgoing
            and source.save_to_sent == destination.save_to_sent
            and source.sent_folder_name == destination.sent_folder_name
        )

    def _build_plan(
        self,
        source: LegacySourceSnapshot,
        catalog: ManagedCatalogPort,
        *,
        preview_token: str,
        source_fingerprint: str,
        target_revision: int,
        created_at: str,
    ) -> LegacyImportPlan:
        existing = {account.name: account for account in catalog.list_accounts()}
        plans: list[LegacyImportAccountPlan] = []
        for account in source.accounts:
            if account.name not in existing:
                action: ImportAction = "conflict" if catalog.has_removed_account(account.name) else "create"
                plans.append(
                    LegacyImportAccountPlan(
                        name=account.name,
                        action=action,
                        source=account,
                        expected_target_revision=None,
                    )
                )
                continue
            destination = catalog.show_account(account.name)
            if not self._configuration_matches(account, destination):
                plans.append(
                    LegacyImportAccountPlan(
                        name=account.name,
                        action="conflict",
                        source=account,
                        expected_target_revision=destination.revision,
                    )
                )
                continue
            missing: list[BindingRole] = []
            if destination.incoming_binding != "ACTIVE":
                missing.append("incoming")
            if account.outgoing is not None and destination.outgoing_binding != "ACTIVE":
                missing.append("outgoing")
            plans.append(
                LegacyImportAccountPlan(
                    name=account.name,
                    action="resume_credentials" if missing else "unchanged",
                    source=account,
                    expected_target_revision=destination.revision,
                    missing_credentials=tuple(missing),
                )
            )
        policy = catalog.policy()
        policy_matches = (
            policy.enable_attachment_download == source.policy.enable_attachment_download
            and policy.allowed_recipients == source.policy.allowed_recipients
            and policy.allowed_senders == source.policy.allowed_senders
            and policy.report_blocked_mutations == source.policy.report_blocked_mutations
        )
        return LegacyImportPlan(
            preview_token=preview_token,
            source_fingerprint=source_fingerprint,
            target_revision=target_revision,
            target_policy_revision=policy.revision,
            created_at=created_at,
            accounts=tuple(plans),
            source_policy=source.policy,
            policy_action="unchanged" if policy_matches else "update",
            unsupported_provider_names=source.unsupported_provider_names,
        )

    def preview(self) -> LegacyImportPlan:
        source = self._backend.load_legacy_source()
        catalog = self._catalog()
        fingerprint = self._source_fingerprint(source)
        target_revision = catalog.catalog_revision()
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        plan = self._build_plan(
            source,
            catalog,
            preview_token=token,
            source_fingerprint=fingerprint,
            target_revision=target_revision,
            created_at=created_at,
        )
        self._assert_target_snapshot(catalog, plan, expected_catalog_revision=target_revision)
        self._previews[token] = _StoredLegacyPreview(
            plan=plan,
            expires_at=time.monotonic() + self._PREVIEW_TTL_SECONDS,
        )
        return plan

    @classmethod
    def _assert_account_target(
        cls,
        catalog: ManagedCatalogPort,
        item: LegacyImportAccountPlan,
        *,
        expected_revision: int | None = None,
    ) -> None:
        target_revision = item.expected_target_revision if expected_revision is None else expected_revision
        summaries = {account.name: account for account in catalog.list_accounts()}
        if target_revision is None:
            if item.name in summaries or catalog.has_removed_account(item.name):
                raise ManagementError("Legacy import preview is stale; preview and retry")
            return
        if item.name not in summaries:
            raise ManagementError("Legacy import preview is stale; preview and retry")
        destination = catalog.show_account(item.name)
        if destination.revision != target_revision:
            raise ManagementError("Legacy import preview is stale; preview and retry")

    @classmethod
    def _assert_target_snapshot(
        cls,
        catalog: ManagedCatalogPort,
        plan: LegacyImportPlan,
        *,
        expected_catalog_revision: int,
    ) -> None:
        if catalog.catalog_revision() != expected_catalog_revision:
            raise ManagementError("Legacy import preview is stale; preview and retry")
        policy = catalog.policy()
        if policy.revision != plan.target_policy_revision:
            raise ManagementError("Legacy import preview is stale; preview and retry")
        for item in plan.accounts:
            cls._assert_account_target(catalog, item)
        if catalog.catalog_revision() != expected_catalog_revision:
            raise ManagementError("Legacy import preview is stale; preview and retry")

    def _assert_source_snapshot(self, plan: LegacyImportPlan) -> LegacySourceSnapshot:
        source = self._backend.load_legacy_source()
        if self._source_fingerprint(source) != plan.source_fingerprint:
            raise ManagementError("Legacy import preview is stale; preview and retry")
        return source

    def apply(  # noqa: C901 - explicit per-account cross-store outcomes
        self,
        *,
        preview_token: str,
        expected_revision: int,
        confirmation: str,
    ) -> LegacyImportReport:
        if confirmation != "IMPORT":
            raise ManagementError("Legacy import confirmation must be exactly IMPORT")
        stored = self._previews.pop(preview_token, None)
        if stored is None or stored.expires_at < time.monotonic():
            raise ManagementError("Legacy import preview is missing or expired; preview and retry")
        plan = stored.plan
        if expected_revision != plan.target_revision:
            raise ManagementError("Legacy import preview is stale; preview and retry")
        self._assert_source_snapshot(plan)
        catalog = self._catalog()
        self._assert_target_snapshot(catalog, plan, expected_catalog_revision=expected_revision)
        if catalog.lifecycle() != "STAGING":
            raise ManagementError("Legacy import can be applied only to a STAGING managed catalog")
        if plan.has_conflicts:
            conflicts = ", ".join(account.name for account in plan.accounts if account.action == "conflict")
            raise ManagementError(f"Legacy import has destination conflicts: {conflicts}")

        created: list[str] = []
        resumed: list[str] = []
        accounts = ManagedAccountService(self._backend)
        credentials = CredentialManagementService(self._backend)
        expected_catalog_revision = plan.target_revision
        for item in plan.accounts:
            account = item.source
            if item.action == "create":
                self._assert_account_target(catalog, item)
                if catalog.catalog_revision() != expected_catalog_revision:
                    raise ManagementError("Legacy import preview is stale; preview and retry")
                incoming_secret = self._backend.resolve_legacy_secret(account.name, "incoming", account)
                outgoing_secret: str | None = None
                if account.outgoing is not None:
                    self._assert_account_target(catalog, item)
                    if catalog.catalog_revision() != expected_catalog_revision:
                        raise ManagementError("Legacy import preview is stale; preview and retry")
                    outgoing_secret = self._backend.resolve_legacy_secret(account.name, "outgoing", account)
                self._assert_source_snapshot(plan)
                self._assert_account_target(catalog, item)
                if catalog.catalog_revision() != expected_catalog_revision:
                    raise ManagementError("Legacy import preview is stale; preview and retry")
                accounts.create(
                    CreateAccountCommand(
                        expected_catalog_revision=expected_catalog_revision,
                        name=account.name,
                        full_name=account.full_name,
                        email_address=account.email_address,
                        incoming=account.incoming,
                        incoming_secret=SecretStr(incoming_secret),
                        outgoing=account.outgoing,
                        outgoing_secret=SecretStr(outgoing_secret) if outgoing_secret is not None else None,
                        save_to_sent=account.save_to_sent,
                        sent_folder_name=account.sent_folder_name,
                    )
                )
                expected_catalog_revision += 1
                created.append(account.name)
            elif item.action == "resume_credentials":
                if item.expected_target_revision is None:
                    raise ManagementError("Legacy import preview is invalid; preview and retry")
                expected_account_revision = item.expected_target_revision
                for role in item.missing_credentials:
                    self._assert_account_target(catalog, item, expected_revision=expected_account_revision)
                    if catalog.catalog_revision() != expected_catalog_revision:
                        raise ManagementError("Legacy import preview is stale; preview and retry")
                    secret = self._backend.resolve_legacy_secret(account.name, role, account)
                    self._assert_source_snapshot(plan)
                    self._assert_account_target(catalog, item, expected_revision=expected_account_revision)
                    if catalog.catalog_revision() != expected_catalog_revision:
                        raise ManagementError("Legacy import preview is stale; preview and retry")
                    credentials.set(
                        account.name,
                        role,
                        secret,
                        expected_revision=expected_account_revision,
                    )
                    expected_account_revision += 1
                resumed.append(account.name)
        if plan.policy_action == "update":
            self._assert_source_snapshot(plan)
            policy = catalog.policy()
            if policy.revision != expected_catalog_revision:
                raise ManagementError("Legacy import preview is stale; preview and retry")
            catalog.update_policy(
                expected_revision=expected_catalog_revision,
                enable_attachment_download=plan.source_policy.enable_attachment_download,
                allowed_recipients=plan.source_policy.allowed_recipients,
                allowed_senders=plan.source_policy.allowed_senders,
                report_blocked_mutations=plan.source_policy.report_blocked_mutations,
            )
        return LegacyImportReport(plan=plan, created=tuple(created), resumed=tuple(resumed))


class PolicyManagementService(_ConfiguredCatalogService):
    """Read and revision-update bounded catalog policy."""

    def get(self) -> ManagedPolicy:
        return self._catalog().policy()

    def update(self, policy: ManagedPolicy) -> ManagedPolicy:
        if len(policy.allowed_recipients) > APPLICATION_LIMITS.recipients:
            raise ManagementError("Managed recipient policy has too many entries")
        if len(policy.allowed_senders) > APPLICATION_LIMITS.recipients:
            raise ManagementError("Managed sender policy has too many entries")
        raw_recipients = tuple(
            validate_controlled_string(
                item,
                field_name="allowed recipient",
                maximum_bytes=APPLICATION_LIMITS.address_bytes,
                allow_empty=True,
            )
            for item in policy.allowed_recipients
        )
        raw_senders = tuple(
            validate_controlled_string(
                item,
                field_name="allowed sender",
                maximum_bytes=APPLICATION_LIMITS.address_bytes,
                allow_empty=True,
            )
            for item in policy.allowed_senders
        )
        recipients = tuple(normalize_address_list(raw_recipients))
        senders = tuple(normalize_pattern_list(raw_senders))
        revision = self._catalog().update_policy(
            expected_revision=policy.revision,
            enable_attachment_download=policy.enable_attachment_download,
            allowed_recipients=recipients,
            allowed_senders=senders,
            report_blocked_mutations=policy.report_blocked_mutations,
        )
        return ManagedPolicy(
            revision=revision,
            enable_attachment_download=policy.enable_attachment_download,
            allowed_recipients=recipients,
            allowed_senders=senders,
            report_blocked_mutations=policy.report_blocked_mutations,
        )


class ConnectivityValidationService(_ConfiguredCatalogService):
    """Validate one endpoint without holding catalog transactions."""

    async def execute(self, name: str, role: BindingRole) -> ConnectivityResult:
        validate_controlled_string(
            name,
            field_name="account_name",
            maximum_bytes=APPLICATION_LIMITS.account_name_bytes,
        )
        try:
            async with asyncio.timeout(APPLICATION_LIMITS.provider_timeout_seconds):
                await self._backend.test_connection(self._catalog(), name, role)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ManagementError):
            return ConnectivityResult(role=role, status="failed", message="Connection test failed")
        return ConnectivityResult(role=role, status="ok", message="Connection succeeded")


class IndexHealthService(_ConfiguredCatalogService):
    """Return one bounded non-secret operational projection health summary."""

    def get(self) -> IndexHealth:
        return self._backend.index_health(self._catalog())


@dataclass(frozen=True)
class ManagementServices:
    lifecycle: CatalogLifecycleService
    accounts: ManagedAccountService
    credentials: CredentialManagementService
    policy: PolicyManagementService
    connectivity: ConnectivityValidationService
    legacy_import: LegacyImportService
    index_health: IndexHealthService

    @classmethod
    def compose(cls, backend: ManagementBackend) -> ManagementServices:
        return cls(
            lifecycle=CatalogLifecycleService(backend),
            accounts=ManagedAccountService(backend),
            credentials=CredentialManagementService(backend),
            policy=PolicyManagementService(backend),
            connectivity=ConnectivityValidationService(backend),
            legacy_import=LegacyImportService(backend),
            index_health=IndexHealthService(backend),
        )
