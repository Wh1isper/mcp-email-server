from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mcp_email_server.config import AccountAttributes


@dataclass(frozen=True)
class EffectiveConfiguration:
    accounts: tuple[AccountAttributes, ...]
    allowed_recipients: tuple[str, ...]
    allowed_senders: tuple[str, ...]


class EffectiveAccountSource(Protocol):
    def list_effective_accounts(self) -> list[AccountAttributes]: ...

    def effective_configuration(self) -> EffectiveConfiguration: ...


class EffectiveAccountQueryService:
    """Return the selected authority's masked effective accounts."""

    def __init__(self, source: EffectiveAccountSource) -> None:
        self._source = source

    def execute(self) -> list[AccountAttributes]:
        return self._source.list_effective_accounts()

    def get(self, account_name: str) -> AccountAttributes | None:
        return next((account for account in self.execute() if account.account_name == account_name), None)


class EffectiveConfigurationQueryService:
    """Return selected-authority policies without exposing secret values."""

    def __init__(self, source: EffectiveAccountSource) -> None:
        self._source = source

    def execute(self) -> EffectiveConfiguration:
        return self._source.effective_configuration()
