from datetime import datetime

from mcp.server.fastmcp import FastMCP
from pydantic import Field

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
    account_name: str = Field(description="The name of the email account."),
    page: int = Field(default=1, description="The page number to retrieve (starting from 1)."),
    page_size: int = Field(default=10, description="The number of emails to retrieve per page."),
    before: datetime | None = Field(default=None, description="Retrieve emails before this datetime (UTC)."),
    since: datetime | None = Field(default=None, description="Retrieve emails since this datetime (UTC)."),
    subject: str | None = Field(default=None, description="Filter emails by subject."),
    body: str | None = Field(default=None, description="Filter emails by body."),
    text: str | None = Field(default=None, description="Filter emails by text."),
    from_address: str | None = Field(default=None, description="Filter emails by sender address."),
    to_address: str | None = Field(default=None, description="Filter emails by recipient address."),
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
    account_name: str = Field(description="The name of the email account to send from."),
    recipients: list[str] = Field(description="A list of recipient email addresses."),
    subject: str = Field(description="The subject of the email."),
    body: str = Field(description="The body of the email."),
    cc: list[str] | None = Field(default=None, description="A list of CC email addresses."),
    bcc: list[str] | None = Field(default=None, description="A list of BCC email addresses."),
) -> None:
    handler = dispatch_handler(account_name)
    await handler.send_email(recipients, subject, body, cc, bcc)
    return
