from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import SecretStr

from mcp_email_server.config import EmailServer, EmailSettings, Settings

BindingRole = Literal["incoming", "outgoing"]
Lifecycle = Literal["STAGING", "ACTIVE"]
ManagementMode = Literal["legacy", "managed"]


class ManagementError(RuntimeError):
    """A management workflow failed without exposing sensitive internals."""


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
class CredentialCleanupReport:
    examined: int
    cleaned: int
    remaining: int


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
    account_count: int
    enabled_account_count: int
    pending_bindings: int
    cleanup_required_bindings: int
    problems: tuple[str, ...]


@dataclass(frozen=True)
class BootstrapSnapshot:
    mode: ManagementMode
    db_path: Path | None


@dataclass(frozen=True)
class ManagementStatus:
    mode: ManagementMode
    report: DoctorReport | None


@dataclass(frozen=True)
class CreateAccountCommand:
    name: str
    full_name: str
    email_address: str
    incoming: EmailServer
    incoming_secret: str
    outgoing: EmailServer | None = None
    outgoing_secret: str | None = None
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
    outgoing: EndpointSummary | None
    save_to_sent: bool
    sent_folder_name: str | None


@dataclass(frozen=True)
class LegacySourceSnapshot:
    accounts: tuple[LegacyAccountSnapshot, ...]
    unsupported_provider_names: tuple[str, ...]
    enable_attachment_download: bool
    allowed_recipients: tuple[str, ...]
    allowed_senders: tuple[str, ...]
    report_blocked_mutations: bool


ImportAction = Literal["create", "resume_credentials", "unchanged", "conflict"]


@dataclass(frozen=True)
class LegacyImportAccountPlan:
    name: str
    action: ImportAction
    missing_credentials: tuple[BindingRole, ...] = ()


@dataclass(frozen=True)
class LegacyImportPlan:
    accounts: tuple[LegacyImportAccountPlan, ...]
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


class ManagedCatalogPort(Protocol):
    path: Path

    def lifecycle(self) -> Lifecycle: ...

    def doctor(self) -> DoctorReport: ...

    def activate(self) -> None: ...

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

    def load_settings(self, *, require_active: bool = True) -> Settings: ...

    def load_account(self, name: str, *, require_active_catalog: bool = False) -> EmailSettings: ...

    def add_account(
        self,
        *,
        name: str,
        full_name: str,
        email_address: str,
        incoming: EmailServer,
        outgoing: EmailServer | None,
        save_to_sent: bool = True,
        sent_folder_name: str | None = None,
    ) -> str: ...

    def set_secret(self, name: str, role: BindingRole, value: str) -> None: ...

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

    def soft_remove_account(self, name: str, *, expected_revision: int) -> int: ...

    def remove_secret(self, name: str, role: BindingRole, *, expected_revision: int) -> bool: ...

    def cleanup_credentials(self, *, limit: int) -> CredentialCleanupReport: ...


class ManagementBackend(Protocol):
    def read_bootstrap(self) -> BootstrapSnapshot: ...

    def initialize_catalog(self, path: Path) -> ManagedCatalogPort: ...

    def open_catalog(self, path: Path) -> ManagedCatalogPort: ...

    def write_selection(self, mode: ManagementMode, db_path: Path | None) -> None: ...

    def load_legacy_source(self) -> LegacySourceSnapshot: ...

    def resolve_legacy_secret(
        self,
        account_name: str,
        role: BindingRole,
        expected_account: LegacyAccountSnapshot,
    ) -> str: ...

    async def test_connection(self, catalog: ManagedCatalogPort, name: str, role: BindingRole) -> None: ...


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
        self._backend.write_selection(bootstrap.mode, catalog.path)

    def status(self) -> ManagementStatus:
        bootstrap = self._backend.read_bootstrap()
        report = self._backend.open_catalog(bootstrap.db_path).doctor() if bootstrap.db_path is not None else None
        return ManagementStatus(mode=bootstrap.mode, report=report)

    def doctor(self) -> DoctorReport:
        return self._catalog().doctor()

    def activate(self) -> None:
        self._catalog().activate()

    def select(self, mode: ManagementMode) -> None:
        bootstrap = self._backend.read_bootstrap()
        if mode == "managed":
            if bootstrap.db_path is None:
                raise ManagementError("Selecting managed mode requires a configured database")
            catalog = self._backend.open_catalog(bootstrap.db_path)
            if catalog.lifecycle() != "ACTIVE":
                raise ManagementError("Managed catalog must be ACTIVE before selection")
            catalog.load_settings(require_active=True)
        self._backend.write_selection(mode, bootstrap.db_path)


class ManagedAccountService(_ConfiguredCatalogService):
    """Own managed account lifecycle commands and optimistic revisions."""

    def create(self, command: CreateAccountCommand) -> None:
        if (command.outgoing is None) != (command.outgoing_secret is None):
            raise ManagementError("Outgoing endpoint and credential must be provided together")
        catalog = self._catalog()
        catalog.add_account(
            name=command.name,
            full_name=command.full_name,
            email_address=command.email_address,
            incoming=command.incoming,
            outgoing=command.outgoing,
            save_to_sent=command.save_to_sent,
            sent_folder_name=command.sent_folder_name,
        )
        catalog.set_secret(command.name, "incoming", command.incoming_secret)
        if command.outgoing_secret is not None:
            catalog.set_secret(command.name, "outgoing", command.outgoing_secret)

    def list(self) -> list[AccountSummary]:
        return self._catalog().list_accounts()

    def show(self, name: str) -> AccountDetails:
        return self._catalog().show_account(name)

    def update(self, command: UpdateAccountCommand) -> int:
        catalog = self._catalog()
        current = catalog.show_account(command.name)
        incoming = command.incoming.apply(current.incoming) if command.incoming is not None else None
        outgoing = command.outgoing.apply(current.outgoing) if command.outgoing is not None else None
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

    def soft_remove(self, name: str, *, expected_revision: int, confirmation: str) -> int:
        if confirmation != name:
            raise ManagementError("Soft removal confirmation must exactly match the account name")
        return self._catalog().soft_remove_account(name, expected_revision=expected_revision)


class CredentialManagementService(_ConfiguredCatalogService):
    """Own managed credential rotation, detachment, and bounded cleanup."""

    def set(self, name: str, role: BindingRole, value: str) -> None:
        self._catalog().set_secret(name, role, value)

    def remove(self, name: str, role: BindingRole, *, expected_revision: int) -> bool:
        return self._catalog().remove_secret(name, role, expected_revision=expected_revision)

    def cleanup(self, *, limit: int) -> CredentialCleanupReport:
        return self._catalog().cleanup_credentials(limit=limit)


class LegacyImportService(_ConfiguredCatalogService):
    """Preview and explicitly apply stored legacy accounts without environment overlays."""

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

    def _build_plan(self, source: LegacySourceSnapshot, catalog: ManagedCatalogPort) -> LegacyImportPlan:
        existing = {account.name: account for account in catalog.list_accounts()}
        plans: list[LegacyImportAccountPlan] = []
        for account in source.accounts:
            if account.name not in existing:
                action: ImportAction = "conflict" if catalog.has_removed_account(account.name) else "create"
                plans.append(LegacyImportAccountPlan(name=account.name, action=action))
                continue
            destination = catalog.show_account(account.name)
            if not self._configuration_matches(account, destination):
                plans.append(LegacyImportAccountPlan(name=account.name, action="conflict"))
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
                    missing_credentials=tuple(missing),
                )
            )
        policy = catalog.policy()
        policy_matches = (
            policy.enable_attachment_download == source.enable_attachment_download
            and policy.allowed_recipients == source.allowed_recipients
            and policy.allowed_senders == source.allowed_senders
            and policy.report_blocked_mutations == source.report_blocked_mutations
        )
        return LegacyImportPlan(
            accounts=tuple(plans),
            policy_action="unchanged" if policy_matches else "update",
            unsupported_provider_names=source.unsupported_provider_names,
        )

    def preview(self) -> LegacyImportPlan:
        return self._build_plan(self._backend.load_legacy_source(), self._catalog())

    @staticmethod
    def _server(endpoint: EndpointSummary, secret: str) -> EmailServer:
        return EmailServer(
            host=endpoint.host,
            port=endpoint.port,
            user_name=endpoint.user_name,
            password=SecretStr(secret),
            use_ssl=endpoint.use_ssl,
            start_ssl=endpoint.start_ssl,
            verify_ssl=endpoint.verify_ssl,
        )

    def apply(self, *, confirmation: str) -> LegacyImportReport:
        if confirmation != "IMPORT":
            raise ManagementError("Legacy import confirmation must be exactly IMPORT")
        source = self._backend.load_legacy_source()
        catalog = self._catalog()
        if catalog.lifecycle() != "STAGING":
            raise ManagementError("Legacy import can be applied only to a STAGING managed catalog")
        plan = self._build_plan(source, catalog)
        if plan.has_conflicts:
            conflicts = ", ".join(account.name for account in plan.accounts if account.action == "conflict")
            raise ManagementError(f"Legacy import has destination conflicts: {conflicts}")
        source_by_name = {account.name: account for account in source.accounts}
        created: list[str] = []
        resumed: list[str] = []
        accounts = ManagedAccountService(self._backend)
        credentials = CredentialManagementService(self._backend)
        for item in plan.accounts:
            account = source_by_name[item.name]
            if item.action == "create":
                incoming_secret = self._backend.resolve_legacy_secret(account.name, "incoming", account)
                outgoing_secret = (
                    self._backend.resolve_legacy_secret(account.name, "outgoing", account)
                    if account.outgoing is not None
                    else None
                )
                accounts.create(
                    CreateAccountCommand(
                        name=account.name,
                        full_name=account.full_name,
                        email_address=account.email_address,
                        incoming=self._server(account.incoming, incoming_secret),
                        incoming_secret=incoming_secret,
                        outgoing=self._server(account.outgoing, outgoing_secret)
                        if account.outgoing is not None and outgoing_secret is not None
                        else None,
                        outgoing_secret=outgoing_secret,
                        save_to_sent=account.save_to_sent,
                        sent_folder_name=account.sent_folder_name,
                    )
                )
                created.append(account.name)
            elif item.action == "resume_credentials":
                resolved = {
                    role: self._backend.resolve_legacy_secret(account.name, role, account)
                    for role in item.missing_credentials
                }
                for role in item.missing_credentials:
                    credentials.set(account.name, role, resolved[role])
                resumed.append(account.name)
        if plan.policy_action == "update":
            policy = catalog.policy()
            catalog.update_policy(
                expected_revision=policy.revision,
                enable_attachment_download=source.enable_attachment_download,
                allowed_recipients=source.allowed_recipients,
                allowed_senders=source.allowed_senders,
                report_blocked_mutations=source.report_blocked_mutations,
            )
        return LegacyImportReport(plan=plan, created=tuple(created), resumed=tuple(resumed))


class ConnectivityValidationService(_ConfiguredCatalogService):
    """Validate one endpoint without holding catalog transactions."""

    async def execute(self, name: str, role: BindingRole) -> None:
        await self._backend.test_connection(self._catalog(), name, role)


@dataclass(frozen=True)
class ManagementServices:
    lifecycle: CatalogLifecycleService
    accounts: ManagedAccountService
    credentials: CredentialManagementService
    connectivity: ConnectivityValidationService
    legacy_import: LegacyImportService

    @classmethod
    def compose(cls, backend: ManagementBackend) -> ManagementServices:
        return cls(
            lifecycle=CatalogLifecycleService(backend),
            accounts=ManagedAccountService(backend),
            credentials=CredentialManagementService(backend),
            connectivity=ConnectivityValidationService(backend),
            legacy_import=LegacyImportService(backend),
        )
