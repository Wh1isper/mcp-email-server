from __future__ import annotations

from typing import TYPE_CHECKING

from mcp_email_server.config import EmailSettings, ProviderSettings, get_settings
from mcp_email_server.emails.classic import ClassicEmailHandler

if TYPE_CHECKING:
    from mcp_email_server.emails import EmailHandler


def dispatch_handler(account_name: str) -> EmailHandler:
    settings = get_settings()
    account = settings.get_account(account_name)
    if isinstance(account, ProviderSettings):
        raise NotImplementedError
    if isinstance(account, EmailSettings):
        return ClassicEmailHandler(account)

    account_names = [a.account_name for a in settings.get_accounts()]
    raise ValueError(f"Account {account_name} not found, available accounts: {account_names}")
