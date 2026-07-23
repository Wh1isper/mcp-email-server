from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import TypeAdapter

from mcp_email_server.application.limits import APPLICATION_LIMITS, validate_serialized_result
from mcp_email_server.config import AccountAttributes


@dataclass(frozen=True)
class EffectiveConfiguration:
    accounts: tuple[AccountAttributes, ...]
    allowed_recipients: tuple[str, ...]
    allowed_senders: tuple[str, ...]


_ACCOUNT_LIST_ADAPTER = TypeAdapter(list[AccountAttributes])
_EFFECTIVE_CONFIGURATION_ADAPTER = TypeAdapter(EffectiveConfiguration)


class EffectiveConfigurationLimitError(ValueError):
    """The selected authority exceeds bounded discovery-result limits."""


class EffectiveAccountSource(Protocol):
    def list_effective_accounts(self) -> list[AccountAttributes]: ...

    def effective_configuration(self) -> EffectiveConfiguration: ...


class EffectiveAccountQueryService:
    """Return the selected authority's masked effective accounts."""

    def __init__(self, source: EffectiveAccountSource) -> None:
        self._source = source

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
