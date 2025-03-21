from datetime import datetime

from mcp.server.fastmcp import FastMCP

from mcp_email_server.config import (
    AccountAttributes,
    EmailSettings,
    ProviderSettings,
    get_settings,
)
from mcp_email_server.emails.dispatcher import dispatch_handler
from mcp_email_server.emails.models import EmailPageResponse

mcp = FastMCP("email")


@mcp.resource("email://{account_name}")
async def get_account(account_name: str) -> EmailSettings | ProviderSettings | None:
    settings = get_settings()
    return settings.get_account(account_name, masked=True)


@mcp.tool()
async def list_available_accounts() -> list[AccountAttributes]:
    settings = get_settings()
    return [account.masked() for account in settings.get_accounts()]


@mcp.tool()
async def add_email_account(email: EmailSettings) -> None:
    settings = get_settings()
    settings.add_email(email)
    settings.store()


@mcp.tool(description="Paginate emails, page start at 1, before and since as UTC datetime.")
async def page_email(
    account_name: str,
    page: int = 1,
    page_size: int = 10,
    before: datetime | None = None,
    since: datetime | None = None,
    subject: str | None = None,
    body: str | None = None,
    text: str | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
) -> EmailPageResponse:
    handler = dispatch_handler(account_name)

    return await handler.get_emails(
        page=page,
        page_size=page_size,
        before=before,
        since=since,
        subject=subject,
        body=body,
        text=text,
        from_address=from_address,
        to_address=to_address,
    )


@mcp.tool(
    description="Send an email using the specified account. Recipient should be a list of email addresses.",
)
async def send_email(
    account_name: str,
    recipients: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> None:
    handler = dispatch_handler(account_name)
    await handler.send_email(recipients, subject, body, cc, bcc)
    return


if __name__ == "__main__":
    import asyncio

    asyncio.run(
        send_email(**{
            "account_name": "jizhongsheng957@gmail.com",
            "recipients": ["jizhongsheng957@gmail.com"],
            "subject": "问候",
            "body": "你好",
            "cc": ["9573586@qq.com"],
            "bcc": ["jzs9573586@qq.com"],
        })
    )
