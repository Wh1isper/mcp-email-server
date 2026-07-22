from __future__ import annotations

from typing import Protocol

from mcp_email_server.config import AccountAttributes


class EffectiveAccountSource(Protocol):
    def list_effective_accounts(self) -> list[AccountAttributes]: ...


class EffectiveAccountQueryService:
    """Return the selected authority's masked effective accounts."""

    def __init__(self, source: EffectiveAccountSource) -> None:
        self._source = source

    def execute(self) -> list[AccountAttributes]:
        return self._source.list_effective_accounts()
