from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, TypeAdapter

from mcp_email_server.application.limits import APPLICATION_LIMITS, validate_serialized_result
from mcp_email_server.config import AccountAttributes, EmailSettings, ProviderSettings


@dataclass(frozen=True)
class EffectiveConfiguration:
    accounts: tuple[AccountAttributes, ...]
    allowed_recipients: tuple[str, ...]
    allowed_senders: tuple[str, ...]


class AvailableAccount(BaseModel):
    """Stable non-secret capabilities for one account exposed to agents."""

    account_name: str = Field(max_length=APPLICATION_LIMITS.account_name_bytes)
    account_type: Literal["email", "provider", "unknown"]
    description: str = Field(max_length=APPLICATION_LIMITS.account_description_bytes)
    email_address: str | None = Field(default=None, max_length=APPLICATION_LIMITS.address_bytes)
    can_receive: bool
    can_send: bool


_ACCOUNT_LIST_ADAPTER = TypeAdapter(list[AccountAttributes])
_AVAILABLE_ACCOUNT_LIST_ADAPTER = TypeAdapter(list[AvailableAccount])
_EFFECTIVE_CONFIGURATION_ADAPTER = TypeAdapter(EffectiveConfiguration)


class EffectiveConfigurationLimitError(ValueError):
    """The selected authority exceeds bounded discovery-result limits."""


class EffectiveAccountSource(Protocol):
    def list_effective_accounts(self) -> list[AccountAttributes]: ...

    def effective_configuration(self) -> EffectiveConfiguration: ...


class EffectiveAccountQueryService:
    """Return selected-authority accounts through internal and public projections."""

    def __init__(self, source: EffectiveAccountSource) -> None:
        self._source = source

    @staticmethod
    def _available_account(account: AccountAttributes) -> AvailableAccount:
        description = account.description
        if len(description.encode("utf-8")) > APPLICATION_LIMITS.account_description_bytes:
            raise EffectiveConfigurationLimitError("limit_exceeded: account description exceeds application limits")
        if isinstance(account, EmailSettings):
            return AvailableAccount(
                account_name=account.account_name,
                account_type="email",
                description=description,
                email_address=account.email_address,
                can_receive=True,
                can_send=account.can_send,
            )
        if isinstance(account, ProviderSettings):
            return AvailableAccount(
                account_name=account.account_name,
                account_type="provider",
                description=description,
                can_receive=False,
                can_send=False,
            )
        return AvailableAccount(
            account_name=account.account_name,
            account_type="unknown",
            description=description,
            can_receive=False,
            can_send=False,
        )

    def execute(self) -> list[AccountAttributes]:
        accounts = self._source.list_effective_accounts()
        if len(accounts) > APPLICATION_LIMITS.configured_accounts:
            raise EffectiveConfigurationLimitError(
                "limit_exceeded: effective account result exceeds application limits"
            )
        try:
            validate_serialized_result(_ACCOUNT_LIST_ADAPTER.dump_json(accounts))
        except ValueError:
            raise EffectiveConfigurationLimitError(
                "limit_exceeded: effective account result exceeds application limits"
            ) from None
        return accounts

    def get(self, account_name: str) -> AccountAttributes | None:
        return next((account for account in self.execute() if account.account_name == account_name), None)

    def discover(self) -> list[AvailableAccount]:
        accounts = [self._available_account(account) for account in self.execute()]
        try:
            validate_serialized_result(_AVAILABLE_ACCOUNT_LIST_ADAPTER.dump_json(accounts))
        except ValueError:
            raise EffectiveConfigurationLimitError(
                "limit_exceeded: account discovery result exceeds application limits"
            ) from None
        return accounts

    def discover_one(self, account_name: str) -> AvailableAccount | None:
        account = self.get(account_name)
        return self._available_account(account) if account is not None else None


class EffectiveConfigurationQueryService:
    """Return selected-authority policies without exposing secret values."""

    def __init__(self, source: EffectiveAccountSource) -> None:
        self._source = source

    def execute(self) -> EffectiveConfiguration:
        configuration = self._source.effective_configuration()
        if len(configuration.accounts) > APPLICATION_LIMITS.configured_accounts or (
            len(configuration.allowed_recipients) > APPLICATION_LIMITS.policy_entries
            or len(configuration.allowed_senders) > APPLICATION_LIMITS.policy_entries
        ):
            raise EffectiveConfigurationLimitError(
                "limit_exceeded: effective configuration result exceeds application limits"
            )
        try:
            validate_serialized_result(_EFFECTIVE_CONFIGURATION_ADAPTER.dump_json(configuration))
        except ValueError:
            raise EffectiveConfigurationLimitError(
                "limit_exceeded: effective configuration result exceeds application limits"
            ) from None
        return configuration
